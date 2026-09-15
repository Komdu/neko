import difflib
import io
import json
import os
import queue
import re
import socket
import struct
import threading
import time

import numpy as np
import requests
import soundfile as sf
import torch
import torchaudio
import whisper
from num2words import num2words

import neko_ha
import neko_miio
import neko_music
import neko_web
import settings as neko_settings

player = neko_music.MusicPlayer()

_NS = neko_settings.load()
_ASS = _NS.get("assistant") or {}
_ESP = _NS.get("esp32") or {}


def _cfg(key, default):
    v = _ASS.get(key)
    if v is None or v == "":
        return os.getenv(key, default)
    return v


def _read_auth_token():
    """Токен берём из env или из esp32_speaker/src/secrets.h (единый источник)."""
    tok = os.getenv("NEKO_AUTH_TOKEN")
    if tok:
        return tok
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "esp32_speaker", "src", "secrets.h")
    try:
        with open(path) as f:
            for line in f:
                m = re.match(r'\s*#define\s+AUTH_TOKEN\s+"([^"]+)"', line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None

ESP32_HOST = os.getenv("ESP32_HOST") or _ESP.get("static_ip") or "192.168.0.200"
ESP32_PORT = int(_cfg("esp32_port", 4211))
ESP32_CTRL_PORT = int(_cfg("esp32_ctrl_port", 4212))
AUTH_TOKEN = os.getenv("NEKO_AUTH_TOKEN") or _read_auth_token()
RECONNECT_DELAY = 3

speech_stop = threading.Event()  # прерывание речи кнопкой MAIN (short во время говорения)
mic_on = True                    # ожидаемое состояние микрофона (для кнопки MAIN в idle)
ctrl = None                      # CtrlClient, создаётся в main()

USE_WAKE_WORD = str(_cfg("use_wake_word", os.getenv("USE_WAKE_WORD", "1"))) == "1"
WAKE_WORDS = str(_cfg("wake_words", "неко,нэко,neko")).lower().split(",")
WAKE_RATIO = float(_cfg("wake_ratio", 0.72))  # порог нечёткого совпадения (0..1)

LLM_URL = str(_cfg("llm_url", "http://localhost:1234/v1/chat/completions"))
LLM_MODEL = str(_cfg("llm_model", "qwen2.5-7b-instruct"))
VOLUME = float(_cfg("volume", 0.85))  # громкость музыки/фиала по умолчанию

SAMPLE_RATE_MIC = 16000
SAMPLE_RATE_SPK = 48000
TTS_MODEL_ID = "v5_5_ru"
TTS_SPEAKER = "kseniya"

SYSTEM_PROMPT = (
    "Ты — умная колонка для дома, её зовут «Нэко». Отвечай кратко и по делу, 1-3 предложения, "
    "обычной разговорной речью, без смайликов и форматирования. Всегда говори по-русски.\n"
    "Ты подключена к Home Assistant и можешь пользоваться инструментами ha_*. "
    "Перед утверждением о состоянии устройства (включено/выключено, заряд, погода) "
    "ОБЯЗАТЕЛЬНО сначала получи реальные данные через ha_get_state или найди сущность "
    "через ha_find_entity по имени («виталя», «уборка», «список покупок»). "
    "Никогда не выдумывай состояния. Если сущность недоступна — честно скажи, что устройство "
    "не на связи.\n"
    "Известные сущности дома: weather.forecast_home (погода), sun.sun с sensor.sun_* (солнце/рассвет), "
    "vacuum.koridor_vitalia (пылесос «Виталя»), sensor.koridor_vitalia_uroven_zariada (заряд Витали), "
    "todo.shopping_list (список покупок), "
    "light.yeelink_ceiling20_a1a2_light (люстра, есть и ambient_light), "
    "fan.xiaomi_cpa4_f77b_air_purifier (очиститель воздуха) с его sensor.xiaomi_cpa4_f77b_pm25_density, "
    "аэрогриль xiaomi_maf14_7c71_* (кнопки start_cook/pause/cancel_cooking, select.mode, "
    "number.target_temperature/target_time). Если запрос про устройство, которого нет в списке или "
    "не уверен в названии — найди через ha_find_entity.\n"
    "Для управления вызывай ha_call_service: свет — light.turn_on/turn_off, пылесос — "
    "vacuum.start/vacuum.pause/vacuum.return_to_base, список покупок — todo.add_item.\n"
    "Часть устройств (люстра, очиститель воздуха) управляется ЛОКАЛЬНО по LAN через инструменты "
    "mi_* (Xiaomi miio) — имя указывай по-русски: «люстра», «очиститель». По LAN девайсы отвечают "
    "только когда реально включены в розетку; иначе инструмент вернёт «не на связи» — так и скажи.\n"
    "Внешние факты (погода не из HA, новости, рецепты, историю, незнакомые слова) — сначала "
    "поищи через web_search, вернёт ссылки с заголовками и сниппетами. Не выдумывай."
)

CHIME_FILE = str(_cfg("chime_file", "chime.wav"))
CHIME_SLEEP = float(_cfg("chime_sleep", 0.4))  # пауза после чимы (эхо динамика)
MAX_LISTEN_S = float(_cfg("max_listen_s", 5))  # сколько ждём команду после чимы

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
print(f"Device: {DEVICE}")

print("Loading Whisper small...")
stt_model = whisper.load_model("small", device=DEVICE)

print(f"Loading Silero {TTS_MODEL_ID}/{TTS_SPEAKER}...")
tts_model, _ = torch.hub.load(
    repo_or_dir="snakers4/silero-models",
    model="silero_tts",
    language="ru",
    model_id=TTS_MODEL_ID,
    speaker=TTS_MODEL_ID,
)

print("Loading Silero VAD...")
vad_model, _vad_utils = torch.hub.load("snakers4/silero-vad", "silero_vad", trust_repo=True)
(get_speech_timestamps, _s, _r, VADIterator, _c) = _vad_utils

# чима-звук после wake-word («туньдунь») -> готовый PCM для динамика
chime_pcm = b""
if os.path.exists(CHIME_FILE):
    _ch, _sr = sf.read(CHIME_FILE, dtype="float32", always_2d=False)
    if _ch.ndim > 1:
        _ch = _ch.mean(axis=1)
    if _sr != SAMPLE_RATE_SPK:
        _n = int(round(len(_ch) * SAMPLE_RATE_SPK / _sr))
        _ch = np.interp(np.linspace(0, len(_ch), _n), np.arange(len(_ch)), _ch)
    _pk = float(np.abs(_ch).max())
    if _pk > 1e-4:
        _ch = _ch * (0.9 / _pk)  # туньдунь изначально тихий (-12дБ) — выравниваем до голоса
    chime_pcm = np.clip(_ch, -1, 1)
    chime_pcm = (chime_pcm * 32767).astype("<i2").tobytes()
    print(f"[CHIME] загружен {CHIME_FILE}: {len(chime_pcm)/2/SAMPLE_RATE_SPK:.2f}s, peak->0.9")
else:
    print(f"[CHIME] {CHIME_FILE} не найден — wake-word без звука")


# ---------- распознавание речи ----------
STT_PROMPT = (
    "Разговорная русская речь, обращение к голосовой колонке. "
    "Колонку зовут «Нэко» — это её имя, пиши его как «неко» или «нэко». "
    "Числа пиши словами, а не цифрами: «двадцать пять», «сорок два». "
    "Никаких субтитров, заголовков и переводов."
)


def recognize(audio: np.ndarray) -> str:
    t0 = time.time()
    audio = audio.copy()
    peak = np.abs(audio).max()
    if peak > 1e-4:
        audio = audio * (0.98 / peak)  # нормировка громкости: тихий/кричащий -> whisper
    result = stt_model.transcribe(
        audio,
        language="ru",
        fp16=False,
        temperature=0.0,
        initial_prompt=STT_PROMPT,
        condition_on_previous_text=False,
        without_timestamps=True,
    )
    print(f"[STT {time.time()-t0:.2f}s] {result['text'].strip()!r}")
    return result["text"].strip()


# ---------- LLM ----------
def _run_tool(name: str, args: dict):
    for mod in (neko_ha, neko_miio, neko_web):
        if name in mod.TOOL_NAMES:
            return mod.run_tool(name, args)
    raise RuntimeError(f"нет такого инструмента {name}")


MAX_TOOL_ROUNDS = 5


def ask(prompt: str) -> str:
    """LLM-запрос с поддержкой tool-use. При ошибке кидает RuntimeError с причиной."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}]
    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 512,
    }
    tools_all = neko_ha.TOOLS + neko_miio.TOOLS + neko_web.TOOLS
    payload["tools"] = tools_all
    payload["tool_choice"] = "auto"

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            r = requests.post(LLM_URL, json=payload, timeout=120)
        except requests.exceptions.ConnectionError as e:
            raise RuntimeError(f"LM Studio недоступен: {e}") from e
        if r.status_code != 200:
            body = r.text[:300]
            raise RuntimeError(f"LM Studio {r.status_code}: {body}")
        resp = r.json()
        if "choices" not in resp:
            err = resp.get("error") or resp.get("detail") or resp
            raise RuntimeError(f"LM Studio ответил без choices: {str(err)[:200]}")
        msg = resp["choices"][0]["message"]
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return msg["content"].strip()
        messages.append(msg)
        for call in tool_calls:
            fn = call.get("function", {})
            name, args = fn.get("name", ""), fn.get("arguments", "{}")
            try:
                result = _run_tool(name, json.loads(args))
            except Exception as e:
                result = {"error": str(e)}
            print(f"[HA {name}] {args} -> {result}{'...' if len(str(result)) > 160 else ''}")
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": json.dumps(result, ensure_ascii=False)[:2000]})
        payload["messages"] = messages
        if "tool_choice" in payload:
            payload.pop("tool_choice")  # после первого вызова модели пусть сервер сам решает
    return "Что-то пошло не так, попробуйте ещё раз"


# ---------- синтез ----------
def _num_to_words(n_str: str) -> str:
    n_str = n_str.replace(",", ".")
    try:
        v = float(n_str)
    except ValueError:
        return n_str
    if v.is_integer():
        return num2words(int(v), lang="ru")
    return num2words(v, lang="ru")


def normalize_numbers(text: str) -> str:
    """Silero не дружит с цифрами: 42 читает как «четыре два».
    Переводим числа в слова, пока ответ не превратился в PCM."""
    text = re.sub(r"(\d+(?:[.,]\d+)?)\s*%", lambda m: _num_to_words(m.group(1)) + " процентов", text)
    text = re.sub(r"(\d+(?:[.,]\d+)?)\s*(?:₽|руб\.?|рублей|рубля)", lambda m: _num_to_words(m.group(1)) + " рублей", text)
    text = re.sub(r"\d+(?:[.,]\d+)?", lambda m: _num_to_words(m.group(0)), text)
    return text


def synthesize(text: str) -> bytes:
    text = normalize_numbers(text)
    print(f"[TTS text] {text}")
    audio = tts_model.apply_tts(text=text, speaker=TTS_SPEAKER, sample_rate=SAMPLE_RATE_SPK)
    if isinstance(audio, list):
        audio = torch.cat(audio)
    pcm = (audio.cpu().numpy() * 32767).astype("<i2").tobytes()
    return pcm


# ---------- VAD-захват фразы из потока микро ------
class UtteranceCapture:
    def __init__(self, threshold=0.5, min_silence_ms=600, speech_pad_ms=200):
        self.iterator = VADIterator(
            vad_model,
            threshold=threshold,
            sampling_rate=SAMPLE_RATE_MIC,
            min_silence_duration_ms=min_silence_ms,
            speech_pad_ms=speech_pad_ms,
        )
        self.in_speech = False
        self.buf = []
        self.stream = []  # непотреблённые сэмплы для выравнивания по 512
        self.chunk = np.zeros(0, dtype=np.float32)

    def push(self, frame: np.ndarray):
        self.stream.append(frame)
        total = np.concatenate(self.stream) if self.stream else frame

        # ресемпл 16к (иногда приходит 44.1/48к не бывает — ESP32 шлёт 16к)
        # подаём VAD кусками по 512 сэмплов (32мс)
        while len(total) >= 512:
            batch = total[:512].astype(np.float32)
            total = total[512:]
            event = self.iterator(batch)
            if self.in_speech:
                self.buf.append(batch)
            if event is not None:
                if "start" in event:
                    self.in_speech = True
                    self.buf = [batch]
                if "end" in event:
                    self.in_speech = False
                    seg = np.concatenate(self.buf)
                    self.buf = []
                    if len(seg) >= SAMPLE_RATE_MIC * 0.4:
                        return seg
        self.stream = [total] if len(total) else []
        return None


def _handshake(sock, tag):
    """Авторизация на ESP32: «AUTH <токен>» -> ждём «AUTH OK»."""
    token = AUTH_TOKEN
    if not token:
        print(f"{tag} токен не задан — подключаюсь без авторизации")
        return True
    try:
        sock.sendall(f"AUTH {token}\n".encode())
        resp = sock.recv(64).decode(errors="ignore").strip()
    except OSError as e:
        print(f"{tag} auth ошибка: {e!r}")
        return False
    if "AUTH OK" not in resp:
        print(f"{tag} сервер отклонил авторизацию: {resp!r}")
        return False
    print(f"{tag} авторизован")
    return True


class NetStream:
    """TCP-связь с ESP32: отдельный поток всегда читает микрофон,
    во время речи ответа сбрасывает (чтобы не слышать собственный голос)."""

    def __init__(self):
        self.q = queue.Queue(maxsize=32)
        self.busy = False
        self.sock = None
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def set_busy(self, busy: bool):
        self.busy = busy

    def send(self, data: bytes):
        # отправляем под блокировкой: recv-поток (close) не закроет fd во время sendall
        with self._lock:
            sock = self.sock
            if sock is None:
                return
            try:
                sock.sendall(data)
            except OSError as e:
                print(f"[NET] send ошибка: {e!r}, пересоздаю соединение")
                if self.sock is sock:
                    self.sock = None
                try:
                    sock.close()
                except OSError:
                    pass

    def recv_frame(self, timeout=0.1):
        try:
            data = self.q.get(timeout=timeout)
        except queue.Empty:
            return None
        return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0

    def _run(self):
        while True:
            print(f"[NET] подключение к {ESP32_HOST}:{ESP32_PORT}...")
            try:
                sock = socket.create_connection((ESP32_HOST, ESP32_PORT), timeout=10)
                sock.settimeout(3)
                if not _handshake(sock, "[NET]"):
                    sock.close()
                    time.sleep(RECONNECT_DELAY)
                    continue
                with self._lock:
                    self.sock = sock
                print("[NET] подключён")
            except Exception as e:
                print(f"[NET] ошибка: {e}; повтор через {RECONNECT_DELAY}с")
                time.sleep(RECONNECT_DELAY)
                continue

            try:
                while True:
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not data:
                        break
                    if not self.busy:
                        try:
                            self.q.put_nowait(data)
                        except queue.Full:
                            try:
                                self.q.get_nowait()
                            except queue.Empty:
                                pass
                            try:
                                self.q.put_nowait(data)
                            except queue.Full:
                                pass
            finally:
                with self._lock:
                    self.sock = None
                sock.close()
                print("[NET] соединение потеряно, переподключение...")
                time.sleep(RECONNECT_DELAY)


class CtrlClient:
    """Управляющий TCP-канал (:4212) с ESP32: события кнопок к нам, команды от нас.
    Протокол — строки через \n: приходит «EV BTN SHORT 1», «EV MUTE 0», «EV VOL»;
    уходит «FLUSH», «MUTE 0/1», «VOL n»."""

    def __init__(self):
        self.q = queue.Queue()
        self._sock = None
        self._lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def send(self, line: str):
        with self._lock:
            sock = self._sock
            if sock is None:
                print(f"[CTRL] не подключён — команда потеряна: {line}")
                return
            try:
                sock.sendall((line + "\n").encode())
            except OSError as e:
                print(f"[CTRL] send ошибка: {e!r}, пересоздаю соединение")
                if self._sock is sock:
                    self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass

    def _run(self):
        buf = b""
        while True:
            print(f"[CTRL] подключение к {ESP32_HOST}:{ESP32_CTRL_PORT}...")
            try:
                sock = socket.create_connection((ESP32_HOST, ESP32_CTRL_PORT), timeout=10)
                sock.settimeout(3)
                if not _handshake(sock, "[CTRL]"):
                    sock.close()
                    time.sleep(RECONNECT_DELAY)
                    continue
                with self._lock:
                    self._sock = sock
                print("[CTRL] управляющий канал подключён")
            except Exception as e:
                print(f"[CTRL] ошибка: {e}; повтор через {RECONNECT_DELAY}с")
                time.sleep(RECONNECT_DELAY)
                continue
            buf = b""
            try:
                while True:
                    try:
                        data = sock.recv(512)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not data:
                        break
                    buf += data
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        self.q.put(line.decode(errors="replace").strip())
            finally:
                with self._lock:
                    self._sock = None
                sock.close()
                print("[CTRL] соединение потеряно, переподключение...")
                time.sleep(RECONNECT_DELAY)


def _norm_word(s: str) -> str:
    return s.lower().replace("ё", "е").strip(" ,.!?;:\"'«»()")


def _word_match(token: str):
    """Точное или нечёткое совпадение токена с wake-word (whisper коверкает «неко»)."""
    t = _norm_word(token)
    best, best_r = None, 0.0
    for wake in WAKE_WORDS:
        w = _norm_word(wake)
        r = 1.0 if t == w else difflib.SequenceMatcher(None, t, w).ratio()
        if r > best_r:
            best, best_r = wake, r
    if best is not None and best_r >= WAKE_RATIO:
        return best, best_r
    return None, 0.0


def _strip_wake_word(text: str):
    m, matched, match_ratio = None, None, 0.0
    for wm in re.finditer(r"\S+", text):
        wake, ratio = _word_match(wm.group())
        if wake is not None:
            m, matched, match_ratio = wm, wake, ratio
            break
    if m is None:
        return None
    print(f"[WUW матч] {m.group()!r} ~ {matched!r} ({match_ratio:.2f})")
    rest = (text[: m.start()] + text[m.end() :]).strip(" ,.;:!?\"«»")
    rest = re.sub(r"\s*,\s*,+", ",", rest).strip()
    return rest


def _send_pcm(net: NetStream, pcm: bytes):
    """Отправка PCM чанками: между чанками проверяем кнопку «стоп» (speech_stop)."""
    speech_stop.clear()
    for off in range(0, len(pcm), 16384):
        if speech_stop.is_set():
            print("[TTS] речь прервана кнопкой")
            break
        net.send(pcm[off:off + 16384])


def speak(net: NetStream, text: str):
    print(f"[TTS text] {text!r}")
    pcm = synthesize(text)
    print(f"[TTS] {len(pcm)/2/SAMPLE_RATE_SPK:.2f}s аудио")
    _send_pcm(net, pcm)


def process_command(command: str, net: NetStream):
    try:
        answer = ask(command)
    except Exception as e:
        print(f"[ERR] LLM: {e!r}")
        answer = "Мозги перегружены: LM Studio не отвечает или модель не загружена."
    print(f"[LLM] {answer!r}")
    pcm = synthesize(answer)
    print(f"[TTS] {len(pcm)/2/SAMPLE_RATE_SPK:.2f}s аудио")
    _send_pcm(net, pcm)


def handle_music(command: str, net: NetStream) -> bool:
    """Возвращает True, если команда — про музыку (включить/стоп) и обработана."""
    intent = neko_music.parse_intent(command)
    if intent is None:
        return False
    act, query = intent
    if act == "stop":
        if player.playing:
            player.stop()
            speak(net, "останавливаю музыку")
        else:
            speak(net, "музыка сейчас и так не играет")
        return True
    if player.playing:
        player.stop()
    speak(net, "сейчас поставлю")
    player.play(query, net.send, volume=VOLUME)
    print(f"[MUSIC] ставлю: {query or 'по умолчанию'}")
    return True


def handle_ctrl(net: NetStream, ev: str):
    """События кнопок с ESP32 (EV BTN SHORT/LONG <playing>, EV MUTE 0/1, EV VOL).
    Решение «пауза/стоп/мьют» принимаем здесь: музыку отличаем по player, речь — по playing."""
    global mic_on
    parts = ev.split()
    if len(parts) >= 4 and parts[:2] == ["EV", "BTN"]:
        press, playing = parts[2], parts[3] == "1"
        if player.playing:  # музыка (в т.ч. пауза): short = пауза/игра, long = стоп
            if press == "LONG":
                print("[CTRL] стоп музыки")
                player.stop()
                ctrl.send("FLUSH")
            elif player.paused:
                print("[CTRL] продолжить музыку")
                player.resume()
            else:
                print("[CTRL] пауза музыки")
                player.pause()
                ctrl.send("FLUSH")
            return
        if playing:  # звучала речь — ESP32 уже сбросил аудио; заставляем ПК прекратить посыл
            print("[CTRL] остановка речи")
            speech_stop.set()
            ctrl.send("FLUSH")
            return
        if press == "SHORT":  # idle: переключаем микрофон
            mic_on = not mic_on
            ctrl.send("MUTE 1" if mic_on else "MUTE 0")
            speak(net, "микрофон включён" if mic_on else "микрофон выключен")
        return
    if len(parts) >= 3 and parts[:2] == ["EV", "MUTE"]:
        mic_on = parts[2] == "1"


def play_chime(net: NetStream) -> float:
    """Проигрывает чиму и возвращает паузу, на которую надо ждать (эхо в микрофон)."""
    if not chime_pcm:
        return 0.0
    chime_dur = len(chime_pcm) / 2 / SAMPLE_RATE_SPK
    net.send(chime_pcm)
    return chime_dur + CHIME_SLEEP


def warn_llm_model():
    """Предупреждает, если LLM-модель в LM Studio не загружена (channel error)."""
    try:
        base = re.match(r"(https?://[^/]+)", LLM_URL)
        r = requests.get((base.group(1) if base else "http://localhost:1234") + "/api/v0/models",
                         timeout=5)
        for md in r.json().get("data", []):
            if md.get("id") == LLM_MODEL and md.get("state") == "not-loaded":
                print(f"[LLM] ВНИМАНИЕ: {LLM_MODEL} в LM Studio НЕ загружена — "
                      "выбери модель в окне LM Studio, иначе ответов не будет.")
    except Exception:
        pass


def main():
    global ctrl
    warn_llm_model()
    net = NetStream()
    net.start()
    ctrl = CtrlClient()
    ctrl.start()
    cap = UtteranceCapture()
    state = "idle"  # idle: ждём wake-word; listening: ждём команду после чимы
    wait_deadline = 0.0

    print("[VAD] жду wake-word «Neko»..." if USE_WAKE_WORD else "[VAD] слушаю...")

    while True:
        while True:  # обработка кнопок с ESP32 (не блокирует приём микрофона)
            try:
                ev = ctrl.q.get_nowait()
            except queue.Empty:
                break
            handle_ctrl(net, ev)

        frame = net.recv_frame()
        if frame is None:
            if state == "listening" and time.monotonic() > wait_deadline:
                print(f"[CLEAR] никто не заговорил за {MAX_LISTEN_S}с — жду «Neko»")
                state = "idle"
            continue

        seg = cap.push(frame)
        if seg is None:
            if state == "listening" and time.monotonic() > wait_deadline:
                print(f"[CLEAR] никто не заговорил за {MAX_LISTEN_S}с — жду «Neko»")
                state = "idle"
            continue

        net.set_busy(True)
        try:
            text = recognize(seg)
            if not text:
                print("[STT] пусто, пропускаю")
                continue

            if state == "idle":
                command = _strip_wake_word(text) if USE_WAKE_WORD else text
                if command is None:
                    print(f"[WUW] {text!r} — wake-word не услышан, игнор")
                    continue
                print(f"[WUW] «{text}» -> Neko!" if USE_WAKE_WORD else f"[CMD] {text!r}")
                if command.strip():
                    chime_wait = play_chime(net)
                    if chime_wait:
                        time.sleep(chime_wait)
                    if not handle_music(command.strip(), net):
                        process_command(command.strip(), net)
                else:
                    chime_wait = play_chime(net)
                    if chime_wait:
                        time.sleep(chime_wait)
                    if USE_WAKE_WORD:
                        state = "listening"
                        wait_deadline = time.monotonic() + MAX_LISTEN_S
                        print(f"[VAD] слушаю команду ({int(MAX_LISTEN_S)}с)...")
                    else:
                        process_command("", net)
            else:  # listening: команда после чимы
                if not handle_music(text, net):
                    process_command(text, net)
                state = "idle"
                print("[VAD] жду «Neko»...")
        except Exception as e:
            print(f"[ERR] {e!r}")
        finally:
            net.set_busy(False)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()