#include <Arduino.h>
#include <WiFi.h>
#include <driver/i2s.h>

#ifdef ENABLE_OTA
#include <ArduinoOTA.h>
#endif

#include "secrets.h"

const char* ssid = WIFI_SSID;
const char* password = WIFI_PASSWORD;

const int TCP_PORT = 4211;
const int SAMPLE_RATE_SPK = 48000;  // динамик (TTS-ответы)
const int SAMPLE_RATE_MIC = 16000;  // микрофон (whisper)

IPAddress staticIP(192, 168, 0, 200);
IPAddress gateway(192, 168, 0, 1);
IPAddress subnet(255, 255, 255, 0);

// ---------- Динамик: MAX98357A (I2S0 TX) ----------
#define I2S_SPK_BCK 26
#define I2S_SPK_LRC 25
#define I2S_SPK_DIN 22

// ---------- Микрофон: INMP441 (I2S1 RX) ----------
// INMP441: VDD=3.3V, GND=GND, L/R=GND (левый канал), SD -> I2S1_SD
#define I2S_MIC_SCK 18
#define I2S_MIC_WS  19
#define I2S_MIC_SD  34

const size_t CHUNK_SAMPLES = 700;     // сэмплов моно за раз (как PACKET_SIZE/2 у радио)
const size_t CHUNK_BYTES = CHUNK_SAMPLES * 2;
const size_t JITTER_CAPACITY = 11200;
const size_t MIC_READ_SAMPLES = 256;
const float VOLUME = 0.7;             // аттенюация динамика (1.0 = полная)

// ---------- Кнопки и управляющий канал ----------
const int CTRL_PORT = 4212;           // отдельный TCP: команды/события кнопок
const int VOL_LEVELS = 10;            // уровней громкости (0..10)
#define BTN_VOL_UP   27
#define BTN_VOL_DOWN 4
#define BTN_MAIN     33

volatile float spkGain = VOLUME;              // текущий коэффициент громкости
volatile int volLevel = VOL_LEVELS;           // 0..VOL_LEVELS
volatile bool micMuted = false;               // мьют микрофона (кнопка MAIN в idle)
volatile unsigned long discardUntil = 0;      // до этого времени выкидываем входящее аудио
volatile unsigned long lastAudioMs = 0;       // когда в последний раз приходил звук с ПК

WiFiServer server(TCP_PORT);
WiFiClient client;
bool clientConnected = false;
SemaphoreHandle_t connLock;

WiFiServer ctrlServer(CTRL_PORT);
WiFiClient ctrlClient;
bool ctrlConnected = false;

static uint8_t jitterBuf[JITTER_CAPACITY];
static int16_t stereoBuf[CHUNK_SAMPLES * 2];
static int32_t micRaw[MIC_READ_SAMPLES];
static int16_t micBuf[MIC_READ_SAMPLES];
volatile size_t jitterFill = 0;  // текущий объём буфера на воспроизведение

// ---------- Приём TTS-аудио с ПК -> I2S0 (динамик) ----------
static void streamSpeakerTask(void* param) {
  unsigned long lastData = 0;

  while (true) {
    if (!clientConnected) {
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }

    xSemaphoreTake(connLock, portMAX_DELAY);
    if (!client.connected()) {
      clientConnected = false;
      client.stop();
      xSemaphoreGive(connLock);
      i2s_zero_dma_buffer(I2S_NUM_0);
      Serial.println("[SPK] клиент отключился");
      continue;
    }

    size_t avail = (size_t)client.available();
    if (avail > 0) {
      // окно выброса: после stop/pause со стороны ПК сбрасываем хвост, не добивая jitter
      if (millis() < discardUntil) {
        uint8_t junk[256];
        client.read(junk, sizeof(junk));
      } else {
        size_t room = JITTER_CAPACITY - jitterFill;
        if (room > 0) {
          size_t want = room < avail ? room : avail;
          int r = client.read(jitterBuf + jitterFill, want);
          if (r > 0) {
            jitterFill += r;
            lastData = millis();
            lastAudioMs = millis();
          } else if (r < 0) {
            clientConnected = false;
            client.stop();
            xSemaphoreGive(connLock);
            i2s_zero_dma_buffer(I2S_NUM_0);
            continue;
          }
        }
      }
    }
    xSemaphoreGive(connLock);

    // моно -> стерео: иначе MAX98357A усредняет с тишиной и играет на -6дБ
    float gain = spkGain;
    while (jitterFill >= CHUNK_BYTES) {
      const int16_t* src = (const int16_t*)jitterBuf;
      int16_t* dst = stereoBuf;
      for (size_t i = 0; i < CHUNK_SAMPLES; i++) {
        int16_t v = (int16_t)(src[i] * gain);
        dst[2 * i] = v;
        dst[2 * i + 1] = v;
      }
      size_t written = 0;
      i2s_write(I2S_NUM_0, stereoBuf, CHUNK_BYTES * 2, &written, portMAX_DELAY);
      memmove(jitterBuf, jitterBuf + CHUNK_BYTES, jitterFill - CHUNK_BYTES);
      jitterFill -= CHUNK_BYTES;
    }

    // поток закончился (тишина на приёме) — сброс хвоста
    if (jitterFill > 0 && millis() - lastData > 500) {
      jitterFill = 0;
      i2s_zero_dma_buffer(I2S_NUM_0);
    }

    vTaskDelay(pdMS_TO_TICKS(2));
  }
}

// ---------- Микрофон I2S1 -> TCP (на ПК) ----------
static void streamMicTask(void* param) {
  while (true) {
    if (!clientConnected) {
      vTaskDelay(pdMS_TO_TICKS(20));
      continue;
    }

    size_t bytesRead = 0;
    esp_err_t err = i2s_read(I2S_NUM_1, micRaw, sizeof(micRaw), &bytesRead, pdMS_TO_TICKS(100));
    if (err != ESP_OK || bytesRead == 0) continue;

    size_t n = bytesRead / sizeof(int32_t);
    size_t out = 0;
    for (size_t i = 0; i < n; i += 2) {  // L-канал (чётные слова), R — тишина
      micBuf[out++] = (int16_t)(micRaw[i] >> 14);
    }

    xSemaphoreTake(connLock, portMAX_DELAY);
    // во время воспроизведения микрофонный аплинк паузим:
    // WiFi полудуплекс, он бы душил даунлинк и ронял sendal'ы по таймауту
    if (clientConnected && client.connected() && jitterFill == 0 && !micMuted) {
      size_t w = client.write((const uint8_t*)micBuf, out * sizeof(int16_t));
      if (w == 0) {
        clientConnected = false;
        client.stop();
        i2s_zero_dma_buffer(I2S_NUM_0);
      }
    }
    xSemaphoreGive(connLock);
  }
}

void setupI2S0() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX),
    .sample_rate = SAMPLE_RATE_SPK,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 256,
  };
  i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
  i2s_pin_config_t pins = {
    .bck_io_num = I2S_SPK_BCK,
    .ws_io_num = I2S_SPK_LRC,
    .data_out_num = I2S_SPK_DIN,
    .data_in_num = I2S_PIN_NO_CHANGE,
  };
  i2s_set_pin(I2S_NUM_0, &pins);
}

void setupI2S1() {
  i2s_config_t cfg = {
    .mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
    .sample_rate = SAMPLE_RATE_MIC,
    .bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT,
    .channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT,
    .communication_format = I2S_COMM_FORMAT_STAND_I2S,
    .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
    .dma_buf_count = 8,
    .dma_buf_len = 256,
  };
  i2s_driver_install(I2S_NUM_1, &cfg, 0, NULL);
  i2s_pin_config_t pins = {
    .bck_io_num = I2S_MIC_SCK,
    .ws_io_num = I2S_MIC_WS,
    .data_out_num = I2S_PIN_NO_CHANGE,
    .data_in_num = I2S_MIC_SD,
  };
  i2s_set_pin(I2S_NUM_1, &pins);
}

// ---------- Зонд усилителя: тоны на I2S0 без WiFi --------------
// Сборка: pio run -e amp_probe -t upload
// Запускать ТОЛЬКО после проверки модуля омметром (без мОм на выходах)!
#ifdef AMP_PROBE
void ampTone(float freq, int seconds) {
  const size_t CHUNK = 1024;
  int16_t st[CHUNK * 2];
  size_t base = 0;
  size_t leftTotal = (size_t)seconds * SAMPLE_RATE_SPK;
  while (leftTotal > 0) {
    size_t n = leftTotal < CHUNK ? leftTotal : CHUNK;
    for (size_t i = 0; i < n; i++) {
      float t = (float)(base + i) / SAMPLE_RATE_SPK;
      int16_t v = (int16_t)(sinf(2.0f * M_PI * freq * t) * 0.12f * 32767.0f);
      st[2 * i] = v;
      st[2 * i + 1] = v;
    }
    size_t written = 0;
    i2s_write(I2S_NUM_0, st, n * 4, &written, portMAX_DELAY);
    base += n;
    leftTotal -= n;
  }
}

void runAmpProbe() {
  setupI2S0();
  i2s_start(I2S_NUM_0);
  Serial.println();
  Serial.println("=== AMP PROBE: I2S0 TX 16bit ===");
  Serial.println("Ждите: 440, 1000, 3000, 100 Гц по 1.2с с паузами, цикл повторяется");
  const float freqs[] = {440.0f, 1000.0f, 3000.0f, 100.0f};
  while (true) {
    for (size_t i = 0; i < 4; i++) {
      Serial.printf("Тон %d Гц ...\n", (int)freqs[i]);
      ampTone(freqs[i], 1);
      delay(300);
    }
    Serial.println("Цикл завершён. Все 4 тона слышны - усилитель и проводка живы.");
    delay(2000);
  }
}
#endif

// ---------- Зонд микрофона: прямой вывод на Serial -------------
// Сборка: pio run -e mic_probe -t upload
#ifdef MIC_PROBE
void runMicProbe() {
  setupI2S1();
  i2s_start(I2S_NUM_1);
  Serial.println();
  Serial.println("=== MIC PROBE: I2S1 RX 32bit RIGHT_LEFT ===");
  Serial.println("Стучите/говорите рядом с микрофоном - уровень должен прыгать");
  int32_t raw[128];
  unsigned long lastReport = 0;
  float rmsL = 0, rmsR = 0;
  while (true) {
    size_t br = 0;
    esp_err_t e = i2s_read(I2S_NUM_1, raw, sizeof(raw), &br, pdMS_TO_TICKS(100));
    if (e != ESP_OK || br == 0) continue;
    size_t n = br / sizeof(int32_t);
    long sumL = 0, sumR = 0;
    size_t cL = 0, cR = 0;
    for (size_t i = 0; i < n; i++) {
      int32_t v = raw[i] >> 14;
      if (i % 2 == 0) { sumL += (long)v * v; cL++; }
      else            { sumR += (long)v * v; cR++; }
    }
    rmsL = cL ? sqrtf((float)sumL / cL) : 0;
    rmsR = cR ? sqrtf((float)sumR / cR) : 0;
    if (millis() - lastReport >= 300) {
      lastReport = millis();
      Serial.printf("rmsL=%7.1f rmsR=%7.1f  (16к, сэмплов=%u)\n",
                    rmsL, rmsR, (unsigned)n);
    }
  }
}
#endif

// ---------- Управляющий TCP-канал (:4212): команды с ПК + события кнопок ----------
void ctrlSend(const char* text) {
  if (ctrlConnected && ctrlClient && ctrlClient.connected()) {
    ctrlClient.print(text);
    ctrlClient.print("\n");
  }
}

void changeVol(int delta) {
  int l = volLevel + delta;
  if (l < 0) l = 0;
  if (l > VOL_LEVELS) l = VOL_LEVELS;
  volLevel = l;
  spkGain = VOLUME * l / (float)VOL_LEVELS;
  ctrlSend("EV VOL");
}

bool audioPlayingNow() {
  return jitterFill > 0 || (millis() - lastAudioMs) < 700;
}

// Hands-токена: клиент должен прислать "AUTH <токен>", иначе соединение закрывается
static bool authClient(WiFiClient& c, const char* tag) {
  if (!AUTH_TOKEN[0]) return true;  // пустой токен => авторизация отключена
  const unsigned long AUTH_TIMEOUT = 4000;
  c.setTimeout(1000);
  unsigned long t0 = millis();
  String line;
  while (millis() - t0 < AUTH_TIMEOUT) {
    if (c.available()) {
      line = c.readStringUntil('\n');
      break;
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
  line.trim();
  String want = String("AUTH ") + AUTH_TOKEN;
  if (line != want) return false;
  c.print("AUTH OK\n");
  Serial.printf("%s авторизация успешна (%s)\n", tag, c.remoteIP().toString().c_str());
  return true;
}

static void ctrlTask(void* param) {
  while (true) {
    if (!ctrlConnected && WiFi.status() == WL_CONNECTED) {
      WiFiClient c = ctrlServer.available();
      if (c) {
        if (authClient(c, "[CTRL]")) {
          ctrlClient = c;
          ctrlConnected = true;
          ctrlClient.setNoDelay(true);
          Serial.println("[CTRL] клиент авторизован");
        } else {
          c.stop();
          Serial.println("[CTRL] неверный токен, соединение закрыто");
        }
      }
    }

    if (ctrlConnected && !ctrlClient.connected()) {
      ctrlClient.stop();
      ctrlConnected = false;
      Serial.println("[CTRL] клиент отключён");
    }

    while (ctrlConnected && ctrlClient.available()) {
      String line = ctrlClient.readStringUntil('\n');
      line.trim();
      if (line == "FLUSH") {
        discardUntil = millis() + 900;
        jitterFill = 0;
        i2s_zero_dma_buffer(I2S_NUM_0);
        Serial.println("[CTRL] FLUSH");
      } else if (line == "MUTE 1") {
        micMuted = true;
        ctrlSend("EV MUTE 1");
        Serial.println("[CTRL] микрофон выключен");
      } else if (line == "MUTE 0") {
        micMuted = false;
        ctrlSend("EV MUTE 0");
        Serial.println("[CTRL] микрофон включён");
      } else if (line.startsWith("VOL ")) {
        int l = line.substring(4).toInt();
        if (l >= 0 && l <= VOL_LEVELS) {
          volLevel = l;
          spkGain = VOLUME * l / (float)VOL_LEVELS;
          ctrlSend("EV VOL");
        }
      }
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

// ---------- Кнопки: громкость +/-, главная (short=пауза/стоп/мьют, long=стоп) ----------
static void buttonsTask(void* param) {
  bool volUpWasLow = false, volDownWasLow = false, mainWasLow = false;
  unsigned long downStart = 0;
  bool longDone = false;
  const unsigned long MAIN_LONG_MS = 800;

  while (true) {
    bool vup = digitalRead(BTN_VOL_UP) == LOW;
    if (vup) {
      if (!volUpWasLow) { volUpWasLow = true; changeVol(+1); }
    } else {
      volUpWasLow = false;
    }
    bool vdn = digitalRead(BTN_VOL_DOWN) == LOW;
    if (vdn) {
      if (!volDownWasLow) { volDownWasLow = true; changeVol(-1); }
    } else {
      volDownWasLow = false;
    }

    bool main = digitalRead(BTN_MAIN) == LOW;
    if (main && !mainWasLow) {
      mainWasLow = true;
      downStart = millis();
      longDone = false;
    }
    if (mainWasLow && !longDone && millis() - downStart >= MAIN_LONG_MS) {
      longDone = true;
      ctrlSend(audioPlayingNow() ? "EV BTN LONG 1" : "EV BTN LONG 0");
      Serial.println("[BTN] длинное нажатие");
    }
    if (!main && mainWasLow) {
      if (!longDone) {
        ctrlSend(audioPlayingNow() ? "EV BTN SHORT 1" : "EV BTN SHORT 0");
        Serial.println("[BTN] короткое нажатие");
      }
      mainWasLow = false;
    }

    vTaskDelay(pdMS_TO_TICKS(10));
  }
}

void setup() {
  Serial.begin(115200);
  Serial.println("\n=== ESP32 Smart Speaker ===");

#ifdef MIC_PROBE
  runMicProbe();  // бесконечный зонд, дальше не идём
#endif

#ifdef AMP_PROBE
  runAmpProbe();  // бесконечный зонд, дальше не идём
#endif

  setupI2S0();
  setupI2S1();

  pinMode(BTN_VOL_UP, INPUT_PULLUP);
  pinMode(BTN_VOL_DOWN, INPUT_PULLUP);
  pinMode(BTN_MAIN, INPUT_PULLUP);

  connLock = xSemaphoreCreateMutex();

  WiFi.begin(ssid, password);
  WiFi.config(staticIP, gateway, subnet);
  WiFi.setSleep(false);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);
  Serial.print("Подключение к WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("IP адрес: ");
  Serial.println(WiFi.localIP());

  server.begin();
  Serial.printf("TCP сервер на порту %d\n", TCP_PORT);

  ctrlServer.begin();
  Serial.printf("Управляющий TCP сервер на порту %d\n", CTRL_PORT);

#ifdef ENABLE_OTA
  ArduinoOTA.setHostname("neko");
#ifdef OTA_PASSWORD
  ArduinoOTA.setPassword(OTA_PASSWORD);
#endif
  ArduinoOTA.onStart([]() {
    Serial.println("[OTA] начало обновления...");
    i2s_zero_dma_buffer(I2S_NUM_0);
  });
  ArduinoOTA.onProgress([](unsigned int p, unsigned int t) {
    Serial.printf("[OTA] %d%%\r", p * 100 / t);
  });
  ArduinoOTA.onEnd([]() { Serial.println("\n[OTA] готово, перезагрузка"); });
  ArduinoOTA.onError([](ota_error_t e) { Serial.printf("[OTA] ошибка: %d\n", e); });
  ArduinoOTA.begin();
  Serial.println("[OTA] готов, UDP/TCP порт 3232");
#endif

  xTaskCreatePinnedToCore(streamSpeakerTask, "speaker", 4096, NULL, 1, NULL, 0);
  xTaskCreatePinnedToCore(streamMicTask, "mic", 4096, NULL, 1, NULL, 1);
  xTaskCreatePinnedToCore(ctrlTask, "ctrl", 4096, NULL, 1, NULL, 1);
  xTaskCreatePinnedToCore(buttonsTask, "buttons", 2048, NULL, 1, NULL, 1);
}

void loop() {
#ifdef ENABLE_OTA
  ArduinoOTA.handle();
#endif
  if (!clientConnected && WiFi.status() == WL_CONNECTED) {
    WiFiClient c = server.available();
    if (c) {
      if (!authClient(c, "[NET]")) {
        c.stop();
        Serial.printf("[NET] неверный токен, соединение закрыто (%s)\n",
                      c.remoteIP().toString().c_str());
        return;
      }
      xSemaphoreTake(connLock, portMAX_DELAY);
      client = c;
      clientConnected = true;
      client.setTimeout(1000);  // не висеть на блокирующих ops дольше 1с
      client.setNoDelay(true);  // никаких нагл-задержек: важна задержка микрофона
      xSemaphoreGive(connLock);
      i2s_zero_dma_buffer(I2S_NUM_0);
      Serial.printf("[NET] клиент подключён: %s\n", client.remoteIP().toString().c_str());
    }
  }
  delay(10);
}