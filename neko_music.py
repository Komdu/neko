import os
import re
import socket
import subprocess
import sys
import threading
import time

from yt_dlp import YoutubeDL

SAMPLE_RATE = 48000
DEFAULT_QUERY = "популярное"
_ym = None  # лениво инициализированный клиент yandex-music

PLAY_KIND = re.compile(r"музык\w*|песн\w*|трек\w*|композиц\w*", re.I)


def _read_auth_token():
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
PLAY_ACTION = re.compile(r"включ\w*|постав[^.]*|запуст\w*|сыграй|врубай", re.I)
STOP_KIND = re.compile(r"(?:выключ\w*|останов\w*|прекрат\w*)\s+(?:музык\w*|песн\w*|трек\w*)", re.I)
STOP_ALONE = re.compile(r"\b(?:стоп|хватит|заткнись|хватит музыки)\b", re.I)


def parse_intent(text: str):
    t = str(text).lower().replace("ё", "е")
    if STOP_ALONE.search(t) or STOP_KIND.search(t):
        return ("stop", None)
    k = PLAY_KIND.search(t)
    if not k or not PLAY_ACTION.search(t):
        return None
    q = t[k.end():]
    q = re.sub(r"^(?:какую[- ]нибудь|что[- ]нибудь|немного|сейчас|нам|пожалуйста)[ .,]+", "", q)
    q = q.strip(" ,.?—!…:;")
    return ("play", q or None)


def ym_client():
    global _ym
    token = os.getenv("YANDEX_MUSIC_TOKEN", "").strip()
    if not token:
        return None
    if _ym is None:
        from yandex_music import Client as YMClient
        _ym = YMClient(token).init()
    return _ym


def _pick_download(info_list):
    best = None
    for i in info_list:
        if i.codec != "mp3":
            continue
        rate = getattr(i, "bitrate_in_kbps", 0) or 0
        if best is None or rate > getattr(best, "bitrate_in_kbps", 0) or 0:
            best = i
    if best is None:
        best = info_list[0]
    return best


def ym_resolve(query: str) -> dict:
    cl = ym_client()
    if cl is None:
        raise RuntimeError("не настроен Yandex Music: зайди по ссылке из music_env.sh и вставь токен")
    res = cl.search(query, page=0, type_="all")
    results = res.tracks.results if res and res.tracks else None
    if not results:
        raise RuntimeError(f"по запросу «{query}» ничего не нашлось")
    track = results[0]
    title = f"{track.artists[0].name} — {track.title}" if track.artists else track.title
    info = _pick_download(track.get_download_info())
    url = info.get_direct_link() if hasattr(info, "get_direct_link") else info.direct_link
    if not url:
        raise RuntimeError("не удалось получить ссылку на трек")
    return {"title": title, "url": url}


def resolve(query: str) -> dict:
    query = query or DEFAULT_QUERY
    if query.startswith("http"):
        opts = {"quiet": True, "format": "bestaudio/best", "noplaylist": True}
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(query, download=False)
    if ym_client() is not None:
        return ym_resolve(query)
    opts = {"quiet": True, "format": "bestaudio/best", "noplaylist": True}
    try:
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info("ytsearch1:" + query, download=False)
    except Exception as e:
        raise RuntimeError(f"не удалось найти трек: {e}")


def _ffmpeg(url: str, volume: float):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", url,
           "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    if volume != 1.0:
        cmd[6:6] = ["-af", f"volume={volume}"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class MusicPlayer:
    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._title = None
        self._stop = threading.Event()
        self._paused = threading.Event()

    @property
    def playing(self) -> bool:
        with self._lock:
            return self._proc is not None

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    @property
    def title(self):
        with self._lock:
            return self._title

    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def stop(self):
        self._stop.set()
        with self._lock:
            ff = self._proc
            self._proc = None
            self._title = None
        if ff is not None:
            try:
                ff.terminate()
            except Exception:
                pass

    def play(self, query, sink, volume=0.85):
        if self.playing:
            self.stop()
        self._stop.clear()
        t = threading.Thread(target=self._run, args=(query, sink, volume), daemon=True)
        t.start()

    def _run(self, query, sink, volume):
        try:
            info = resolve(query)
        except Exception as e:
            print(f"[MUSIC] ошибка: {e}")
            return
        title = info.get("title") or query
        url = info.get("url")
        if not url:
            print(f"[MUSIC] не удалось получить стрим: {title}")
            return
        with self._lock:
            self._title = title
        print(f"[MUSIC] играет: {title}")
        ff = _ffmpeg(url, volume)
        with self._lock:
            self._proc = ff
        try:
            while not self._stop.is_set():
                if self._paused.is_set():
                    time.sleep(0.1)
                    continue
                chunk = ff.stdout.read(16384)
                if not chunk:
                    break
                sink(chunk)
        except OSError as e:
            print(f"[MUSIC] обрыв: {e!r}")
        finally:
            self.stop()
            print("[MUSIC] поток завершён")


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: neko_music.py 'запрос или URL' [--seconds N] [--volume 0.8]")
    query = sys.argv[1]
    volume = float(sys.argv[sys.argv.index("--volume") + 1]) if "--volume" in sys.argv else 0.85

    host = os.getenv("ESP32_HOST", "192.168.0.200")
    port = int(os.getenv("ESP32_PORT", "4211"))

    sock = socket.create_connection((host, port), timeout=15)
    token = os.getenv("NEKO_AUTH_TOKEN") or _read_auth_token()
    if token:
        sock.sendall(f"AUTH {token}\n".encode())
        resp = sock.recv(64).decode(errors="ignore").strip()
        if "AUTH OK" not in resp:
            raise SystemExit(f"[MUSIC] сервер отклонил авторизацию: {resp!r}")
    print(f"[MUSIC] колонка {host}:{port} подключена, Ctrl+C — стоп")
    player = MusicPlayer()
    player.play(query, sock.sendall, volume=volume)
    try:
        while player.playing:
            time.sleep(0.5)
    except KeyboardInterrupt:
        player.stop()
    sock.close()


if __name__ == "__main__":
    main()