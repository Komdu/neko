"""Realtime-превью микрофона ESP32: осциллограмма + уровень + прослушивание (Qt6 + sounddevice).

Запуск:
  /mnt/hdd/neko/venv/bin/python mic_preview.py
Опции: ESP32_HOST / ESP32_PORT.
Выходное устройство выбирается в комбо (по умолчанию — Fifine/голова, иначе system default).
"""

import collections
import os
import socket
import threading
import time

import numpy as np
import sounddevice as sd
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

HOST = os.getenv("ESP32_HOST", "192.168.0.200")
PORT = int(os.getenv("ESP32_PORT", "4211"))
SR = 16000

WAVE_WINDOW_S = 0.7     # сколько аудио видно на экране
FALL_DB_S = 8.0         # скорость затухания индикатора, дБ/с
PLAY_BUF_MAX = 2 * SR * 2   # максимум буфера прослушивания, 2с

BG = QColor("#0a0e14")
GRID = QColor("#1e2a38")
WAVE_C = QColor("#39d98a")
RED = QColor("#ff6b6b")
YELLOW = QColor("#ffd166")
TEXT_C = QColor("#9fb3c8")


class WaveformWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(820, 280)
        self.samples = np.zeros(0, dtype=np.float32)
        self.status_msg = "инициализация..."
        self.status_color = YELLOW
        self.extra = ""

    def set_data(self, samples, msg, color, extra):
        self.samples = samples
        self.status_msg = msg
        self.status_color = color
        self.extra = extra
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), BG)

        w, h = self.width(), self.height()
        mid = h * 0.5

        pen = QPen(GRID, 1, Qt.DashLine)
        p.setPen(pen)
        p.drawLine(0, int(mid), w, int(mid))

        if len(self.samples) > 1:
            npts = min(w, len(self.samples))
            tail = self.samples[-npts:] if len(self.samples) > npts else self.samples
            if len(tail) > npts:
                idx = np.linspace(0, len(tail) - 1, npts).astype(int)
                tail = tail[idx]
            ys = mid - tail.astype(np.float64) / 32768.0 * mid
            xs = np.linspace(0, w, len(tail))
            poly = QPolygonF([QPointF(float(xs[i]), float(ys[i])) for i in range(len(tail))])
            pen = QPen(WAVE_C, 1.5)
            p.setPen(pen)
            p.drawPolyline(poly)

        p.setFont(QFont("Sans", 11, QFont.Bold))
        p.setPen(self.status_color)
        p.drawText(10, 24, f"{self.status_msg}  {self.extra}")
        p.setPen(TEXT_C)
        p.setFont(QFont("Sans", 9))
        p.drawText(10, h - 12, f"{HOST}:{PORT}  ·  16kHz/s16/mono")


class MeterWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(90, 280)
        self.level_db = -99.0
        self.peak_db = -99.0

    def set_level(self, db, peak):
        self.level_db = db
        self.peak_db = peak
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), BG)

        mw, mh = self.width(), self.height()
        x0, y0, x1, y1 = 6, 6, mw - 6, mh - 6

        for db in range(-60, 1, 10):
            y = y1 - (db + 70) / 70.0 * (y1 - y0)
            p.setPen(QPen(GRID, 1, Qt.DashLine if db else Qt.SolidLine))
            p.drawLine(x0, int(y), x1, int(y))

        frac = max(0.0, min(1.0, (self.level_db + 70) / 70.0))
        bh = max(1, int((y1 - y0) * frac))
        p.fillRect(x0, y1 - bh, x1 - x0, bh, WAVE_C)

        ty = y1 - (-30 + 70) / 70.0 * (y1 - y0)
        p.setPen(QPen(RED, 2))
        p.drawLine(x0, int(ty), x1, int(ty))

        p.setFont(QFont("Sans", 8))
        p.setPen(TEXT_C)
        for i, t in enumerate(["ГОВОРИТ", "фон", "тишина"]):
            p.drawText(x0 + 3, int(y1 - 20 - i * 15), t)
        p.setPen(YELLOW)
        p.setFont(QFont("Sans", 8, QFont.Bold))
        p.drawText(x0 + 3, y0 + 14, f"pk {self.peak_db:.1f}")


class PreviewApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"ESP32 Mic Preview — {HOST}:{PORT}")

        self.stop = threading.Event()
        self.sock = None
        self.state = "ищу колонку..."
        self.bytes_total = 0
        self.last_rx_t = 0.0
        self._t0 = time.time()

        self.wave = collections.deque(maxlen=int(SR * WAVE_WINDOW_S))
        self.wave_lock = threading.Lock()
        self.level_db = -99.0
        self.peak_db = -99.0

        self.rec_samples = None
        self.rec_path = "mic_record.wav"

        self.playing = False
        self.sd_stream = None
        self.sd_buf = bytearray()
        self.sd_lock = threading.Lock()
        self.dev_index = 0

        self._build_ui()
        threading.Thread(target=self._recv_loop, daemon=True).start()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._on_tick)
        self.timer.start(50)
        self._t0 = time.time()

    # ---------- UI ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        top = QHBoxLayout()
        self.wave_w = WaveformWidget()
        self.meter_w = MeterWidget()
        top.addWidget(self.wave_w, 1)
        top.addWidget(self.meter_w, 0)
        root.addLayout(top, 1)

        bar = QHBoxLayout()
        self.rec_btn = QPushButton("● Запись")
        self.rec_btn.setFixedWidth(110)
        self.rec_btn.clicked.connect(self._toggle_rec)
        bar.addWidget(self.rec_btn)

        self.play_btn = QPushButton("🔇 Слушать: ВЫКЛ")
        self.play_btn.setFixedWidth(140)
        self.play_btn.clicked.connect(self._toggle_play)
        bar.addWidget(self.play_btn)

        bar.addWidget(QLabel("Вывод:"))
        self.dev_combo = QComboBox()
        self.dev_combo.setMinimumWidth(240)
        self._fill_devices()
        self.dev_combo.currentIndexChanged.connect(self._on_dev_changed)
        bar.addWidget(self.dev_combo, 1)

        self.status_label = QLabel(self.state)
        bar.addWidget(self.status_label)
        root.addLayout(bar)

        self.resize(1040, 340)

    def _fill_devices(self):
        prefer = ("fifine", "vc_out", "pulse", "default")
        try:
            devs = sd.query_devices()
        except Exception as e:
            self.state = f"sounddevice: {e}"
            return
        chosen = None
        seen = set()
        for i, d in enumerate(devs):
            if d.get("max_output_channels", 0) <= 0:
                continue
            ha = sd.query_hostapis(d["hostapi"])["name"]
            label = f"{i} · {d['name']} [{ha}]"
            seen.add(i)
            self.dev_combo.addItem(label, i)
            nl = f"{d['name']}".lower()
            if chosen is None and any(p in nl for p in prefer):
                chosen = i
        if chosen is not None:
            idx = self.dev_combo.findData(chosen)
            if idx >= 0:
                self.dev_combo.setCurrentIndex(idx)
                self.dev_index = chosen

    def _on_dev_changed(self):
        self.dev_index = self.dev_combo.currentData()
        if self.playing:
            self._stop_play()
            self._start_play(self.dev_index)

    # ---------- сеть ----------
    def _connect(self):
        self.sock = socket.create_connection((HOST, PORT), timeout=10)
        self.sock.settimeout(1)
        self.state = f"колонка {HOST}:{PORT}"

    def _recv_loop(self):
        buf = b""
        while not self.stop.is_set():
            if self.sock is None:
                self.state = "✖ ищу колонку (останови run.sh, если он запущен)"
                try:
                    self._connect()
                except OSError:
                    time.sleep(3)
                    continue
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                self.state = "соединение потеряно"
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
                time.sleep(3)
                continue
            if not data:
                self.state = "поток закончился"
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None
                continue

            buf += data
            n = len(buf) - len(buf) % 2
            if n == 0:
                continue
            raw = buf[:n]
            buf = buf[n:]

            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            now = time.time()

            with self.wave_lock:
                self.wave.extend(samples.tolist())
            self.bytes_total += len(raw)
            self.last_rx_t = now

            if self.rec_samples is not None:
                self.rec_samples.append(samples)
            if self.playing:
                self._feed_play(raw)

            db = 20 * np.log10((float(np.sqrt((samples ** 2).mean())) + 1e-9) / 32768)
            self.level_db = max(db, self.level_db - FALL_DB_S * 0.05)
            self.peak_db = max(db, self.peak_db - FALL_DB_S * 0.05 * 0.25)

    # ---------- прослушивание ----------
    def _start_play(self, index):
        self.sd_buf = bytearray()

        def cb(outdata, frames, _ti, _st):
            need = frames * 2
            with self.sd_lock:
                b = self.sd_buf
                if len(b) >= need:
                    outdata[:] = np.frombuffer(bytes(b[:need]), dtype="<i2").reshape(frames, 1)
                    del b[:need]
                else:
                    nsamp = len(b) // 2
                    if nsamp:
                        arr = np.frombuffer(bytes(b[: nsamp * 2]), dtype="<i2")
                        outdata[:nsamp] = arr.reshape(-1, 1)
                    outdata[nsamp:] = 0
                    b.clear()

        try:
            self.sd_stream = sd.OutputStream(
                samplerate=SR, channels=1, dtype="int16",
                device=None if index is None else index,
                callback=cb, latency="low", blocksize=1024,
            )
            self.sd_stream.start()
            self.playing = True
            self.play_btn.setText("⚡ Слушать: ВКЛ")
        except Exception as e:
            self.state = f"вывод не запустился: {e}"
            self.playing = False

    def _stop_play(self):
        self.playing = False
        try:
            if self.sd_stream:
                self.sd_stream.stop()
                self.sd_stream.close()
        except Exception:
            pass
        self.sd_stream = None
        self.play_btn.setText("🔇 Слушать: ВЫКЛ")

    def _toggle_play(self):
        if self.playing:
            self._stop_play()
        else:
            self._start_play(self.dev_index)

    def _feed_play(self, raw):
        with self.sd_lock:
            self.sd_buf += raw
            over = len(self.sd_buf) - PLAY_BUF_MAX
            if over > 0:
                del self.sd_buf[:over]

    # ---------- запись ----------
    def _toggle_rec(self):
        if self.rec_samples is None:
            self.rec_samples = []
            self.rec_btn.setText("⏹ Стоп (пишу...)")
        else:
            try:
                samples = np.concatenate(self.rec_samples)
            except ValueError:
                samples = np.zeros(0, dtype=np.float32)
            import soundfile as sf

            sf.write(self.rec_path, samples.astype(np.float32) / 32768.0, SR)
            self.rec_samples = None
            self.rec_btn.setText("● Запись")
            self.state = f"сохранено: {self.rec_path} ({len(samples) / SR:.1f}с)"

    # ---------- тик ----------
    def _on_tick(self):
        with self.wave_lock:
            data = np.asarray(list(self.wave), dtype=np.float32)
        self.meter_w.set_level(self.level_db, self.peak_db)

        now = time.time()
        if self.sock is not None:
            if now - self.last_rx_t <= 1.5:
                color = WAVE_C
                msg = "● ДАННЫЕ ИДУТ"
            else:
                color = YELLOW
                msg = "● подключена, но тишина"
            rate = self.bytes_total / max(0.1, now - self._t0)
            extra = f"{self.bytes_total // 1024} KiB · {rate / 1024:.1f} KiB/s"
        else:
            color = RED
            msg = self.state
            extra = ""
        self.wave_w.set_data(data, msg, color, extra)
        self.status_label.setText(self.state)

    # ---------- выход ----------
    def closeEvent(self, _e):
        self.stop.set()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self._stop_play()


def main():
    import sys

    app = QApplication(sys.argv)
    win = PreviewApp()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()