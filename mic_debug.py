import os
import re
import socket
import sys
import time

import numpy as np

HOST = os.getenv("ESP32_HOST", "192.168.0.200")
PORT = int(os.getenv("ESP32_PORT", "4211"))
SR = 16000

WINDOW = SR // 10  # 0.1 сек

_BAR = 40  # ширина полосы уровня


def auth(sock):
    tok = os.getenv("NEKO_AUTH_TOKEN")
    if not tok:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "esp32_speaker", "src", "secrets.h")
        try:
            for line in open(path):
                m = re.match(r'\s*#define\s+AUTH_TOKEN\s+"([^"]+)"', line)
                if m:
                    tok = m.group(1)
                    break
        except OSError:
            pass
    if tok:
        sock.sendall(f"AUTH {tok}\n".encode())
        if b"AUTH OK" not in sock.recv(64):
            raise SystemExit("auth: сервер отклонил токен")


def connect():
    while True:
        try:
            sock = socket.create_connection((HOST, PORT), timeout=10)
            sock.settimeout(1)
            auth(sock)
            print(f"[NET] подключён к {HOST}:{PORT}, микрофон льётся 16к/s16/mono")
            print("Ctrl+C — выход\n")
            return sock
        except OSError as e:
            print(f"[NET] {e}, повтор через 3с...")
            time.sleep(3)


def main():
    dump_path = sys.argv[1] if len(sys.argv) > 1 else None
    dump = []

    sock = connect()
    buf = b""
    acc = np.zeros(0, dtype=np.int16)
    total = 0
    last_report = 0.0

    try:
        while True:
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                print("\n[NET] поток закончился, переподключение...")
                sock.close()
                sock = connect()
                buf = b""
                continue

            buf += data
            if len(buf) >= 2:
                n = len(buf) - len(buf) % 2
                acc = np.concatenate([acc, np.frombuffer(buf[:n], dtype="<i2")])
                buf = buf[n:]

            if dump_path is not None:
                dump.append(acc.copy())
                acc = np.zeros(0, dtype=np.int16)

            # окно 0.1с
            while len(acc) >= WINDOW:
                win = acc[:WINDOW]
                acc = acc[WINDOW:]
                total += WINDOW
                rms = float(np.sqrt(np.mean(win.astype(np.float32) ** 2))) + 1e-9
                peak = float(np.max(np.abs(win.astype(np.float32))))
                dbfs = 20 * np.log10(rms / 32768)
                dbpeak = 20 * np.log10(peak / 32768 + 1e-9)

                frac = min(1.0, max(0.0, (dbfs + 60) / 60))
                bar = "#" * int(frac * _BAR) + "." * (_BAR - int(frac * _BAR))
                now = time.time()
                ts = f"{total / SR:6.2f}s"
                level = f"{dbfs:5.1f}dBFS pk{dbpeak:5.1f}"

                state = ""
                if dbfs > -30:
                    state = " ГОВОРИТ"
                elif dbfs > -55 and dbfs <= -30:
                    state = " фон"
                else:
                    state = " тишина"
                if now - last_report >= 0.05:
                    sys.stdout.write(f"\r[{ts}] |{bar}| {level}  {state}     ")
                    sys.stdout.flush()
                    last_report = now
    except KeyboardInterrupt:
        print("\n\n[СТОП]")
    finally:
        sock.close()
        if dump_path is not None and dump:
            arr = np.concatenate(dump)
            import soundfile as sf

            sf.write(dump_path, arr.astype(np.float32) / 32768.0, SR)
            print(f"Сохранено: {dump_path} ({len(arr) / SR:.1f}с)")
        elif dump_path is not None:
            print("Нет данных для сохранения")


if __name__ == "__main__":
    main()