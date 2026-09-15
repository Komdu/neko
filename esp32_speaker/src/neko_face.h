#pragma once
// Мордочки Нэко для OLED SSD1306 128x64 (I2C), эмоции по команде с ПК "EMOTION <имя>".
#include <Arduino.h>
#include <Adafruit_SSD1306.h>

#define NEKO_EYE_Y 27
#define NEKO_EYE_R 8
#define NEKO_EYE_LX 38
#define NEKO_EYE_RX 90

static void _arc(Adafruit_SSD1306& d, int cx, int cy, int r, double a0, double a1) {
  double a = a0;
  int px = cx + (int)(r * cos(a)), py = cy + (int)(r * sin(a));
  for (; a <= a1; a += 0.12) {
    int x = cx + (int)(r * cos(a)), y = cy + (int)(r * sin(a));
    d.drawLine(px, py, x, y, SSD1306_WHITE);
    px = x; py = y;
  }
}

// Глаза
static void _eyeOpen(Adafruit_SSD1306& d, int cx, int cy) {
  d.fillCircle(cx, cy, NEKO_EYE_R, SSD1306_WHITE);
  d.fillCircle(cx, cy, 3, SSD1306_BLACK);
}

static void _eyeLook(Adafruit_SSD1306& d, int cx, int cy, int px, int py) {
  d.fillCircle(cx, cy, NEKO_EYE_R, SSD1306_WHITE);
  d.fillCircle(cx + px, cy + py, 3, SSD1306_BLACK);
}

static void _eyeBig(Adafruit_SSD1306& d, int cx, int cy) {
  d.fillCircle(cx, cy, 9, SSD1306_WHITE);
  d.fillCircle(cx, cy, 2, SSD1306_BLACK);
}

static void _eyeDome(Adafruit_SSD1306& d, int cx, int cy) {  // ^^ закрытый радостный
  _arc(d, cx, cy + 4, NEKO_EYE_R, M_PI, 2 * M_PI);
}

static void _eyeLine(Adafruit_SSD1306& d, int cx, int cy) {  // ровная линия (подмиг)
  d.drawLine(cx - NEKO_EYE_R, cy, cx + NEKO_EYE_R, cy, SSD1306_WHITE);
}

static void _brow(Adafruit_SSD1306& d, int cx, int y, int slant) {
  // slant>0: внешний край выше (грусть); slant<0: внутренний выше (злость)
  d.drawLine(cx - NEKO_EYE_R, y - slant, cx + NEKO_EYE_R, y + slant, SSD1306_WHITE);
}

static void _heart(Adafruit_SSD1306& d, int cx, int cy, int s) {
  d.fillCircle(cx - s / 3, cy - s / 4, s / 4, SSD1306_WHITE);
  d.fillCircle(cx + s / 3, cy - s / 4, s / 4, SSD1306_WHITE);
  d.fillTriangle(cx - s / 2, cy, cx + s / 2, cy, cx, cy + s / 2, SSD1306_WHITE);
}

static void _tear(Adafruit_SSD1306& d, int cx, int cy) {
  d.fillRect(cx - 1, cy, 2, 5, SSD1306_WHITE);
  d.fillCircle(cx, cy + 6, 2, SSD1306_WHITE);
}

// Рты
static void _mouthSmile(Adafruit_SSD1306& d, int r) { _arc(d, 64, 42, r, 0, M_PI); }
static void _mouthFrown(Adafruit_SSD1306& d, int r) { _arc(d, 64, 48, r, M_PI, 2 * M_PI); }
static void _mouthLine(Adafruit_SSD1306& d, int y) { d.drawLine(52, y, 76, y, SSD1306_WHITE); }
static void _mouthO(Adafruit_SSD1306& d) { d.fillCircle(64, 48, 4, SSD1306_WHITE); }
static void _mouthOpen(Adafruit_SSD1306& d) { d.fillCircle(64, 48, 5, SSD1306_WHITE); }

// ---------- Сборка мордочки ----------
static void drawEmotion(Adafruit_SSD1306& d, const String& name) {
  d.clearDisplay();
  if (name == "neutral") {
    _eyeOpen(d, NEKO_EYE_LX, NEKO_EYE_Y);
    _eyeOpen(d, NEKO_EYE_RX, NEKO_EYE_Y);
    _mouthLine(d, 47);
  } else if (name == "happy") {
    _eyeOpen(d, NEKO_EYE_LX, NEKO_EYE_Y);
    _eyeOpen(d, NEKO_EYE_RX, NEKO_EYE_Y);
    _mouthSmile(d, 11);
  } else if (name == "sad") {
    _eyeOpen(d, NEKO_EYE_LX, NEKO_EYE_Y);
    _eyeOpen(d, NEKO_EYE_RX, NEKO_EYE_Y);
    _brow(d, NEKO_EYE_LX, 16, 2);
    _brow(d, NEKO_EYE_RX, 16, -2);
    _mouthFrown(d, 11);
  } else if (name == "angry") {
    _eyeOpen(d, NEKO_EYE_LX, NEKO_EYE_Y);
    _eyeOpen(d, NEKO_EYE_RX, NEKO_EYE_Y);
    _brow(d, NEKO_EYE_LX, 15, -3);
    _brow(d, NEKO_EYE_RX, 15, 3);
    _mouthFrown(d, 11);
  } else if (name == "surprised") {
    _eyeBig(d, NEKO_EYE_LX, NEKO_EYE_Y);
    _eyeBig(d, NEKO_EYE_RX, NEKO_EYE_Y);
    _mouthO(d);
  } else if (name == "thinking") {
    _eyeLook(d, NEKO_EYE_LX, NEKO_EYE_Y, -2, -2);
    _eyeLook(d, NEKO_EYE_RX, NEKO_EYE_Y, -2, -2);
    _mouthLine(d, 49);
    d.fillCircle(78, 7, 1, SSD1306_WHITE);
    d.fillCircle(86, 5, 1, SSD1306_WHITE);
    d.fillCircle(94, 7, 1, SSD1306_WHITE);
  } else if (name == "love") {
    _heart(d, NEKO_EYE_LX, NEKO_EYE_Y, 12);
    _heart(d, NEKO_EYE_RX, NEKO_EYE_Y, 12);
    _mouthSmile(d, 11);
  } else if (name == "cry") {
    _eyeLook(d, NEKO_EYE_LX, NEKO_EYE_Y, 0, -1);
    _eyeLook(d, NEKO_EYE_RX, NEKO_EYE_Y, 0, -1);
    _tear(d, NEKO_EYE_LX - 7, 36);
    _tear(d, NEKO_EYE_RX + 7, 36);
    _mouthFrown(d, 11);
  } else if (name == "lol") {
    _eyeDome(d, NEKO_EYE_LX, NEKO_EYE_Y);
    _eyeDome(d, NEKO_EYE_RX, NEKO_EYE_Y);
    _mouthOpen(d);
    _mouthSmile(d, 12);
  } else if (name == "wink") {
    _eyeDome(d, NEKO_EYE_LX, NEKO_EYE_Y);
    _eyeOpen(d, NEKO_EYE_RX, NEKO_EYE_Y);
    _mouthSmile(d, 11);
  } else {  // idle / NONE / неизвестная
    _eyeDome(d, NEKO_EYE_LX, NEKO_EYE_Y);
    _eyeDome(d, NEKO_EYE_RX, NEKO_EYE_Y);
    _mouthSmile(d, 9);
  }
  d.display();
}