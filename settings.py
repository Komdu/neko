"""WebUI-настройки «Нэко»: settings.json + генерация esp32 secrets.h + env-файлы.

Плюс: скан свободных IP в подсети (если 192.168.0.200 занят) и запуск OTA-прошивки.
Никаких секретов в этом файле — только чтение/запись gitignored-файлов.
"""
import hmac
import json
import os
import re
import secrets as _secrets
import socket
import subprocess
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(ROOT, "settings.json")
SECRETS_H = os.path.join(ROOT, "esp32_speaker", "src", "secrets.h")
FLASH_LOG = os.path.join(ROOT, "webui_flash.log")

ENV_FILES = {
    "ha_env.sh": ("HA_URL", "HA_TOKEN"),
    "music_env.sh": ("YANDEX_MUSIC_TOKEN",),
    "miio_env.sh": (
        "MIIO_YEELIGHT_IP", "MIIO_YEELIGHT_TOKEN", "MIIO_YEELIGHT_MODEL",
        "MIIO_PURIFIER_IP", "MIIO_PURIFIER_TOKEN", "MIIO_PURIFIER_MODEL",
        "MIIO_FRYER_IP", "MIIO_FRYER_TOKEN", "MIIO_FRYER_MODEL",
    ),
}
SECRET_KEYS = ("HA_TOKEN", "YANDEX_MUSIC_TOKEN", "MIIO_YEELIGHT_TOKEN",
               "MIIO_PURIFIER_TOKEN", "MIIO_FRYER_TOKEN",
               "WIFI_PASSWORD", "OTA_PASSWORD", "AUTH_TOKEN")
CLEAR = "__CLEAR__"


# ---------------------------------------------------------------- env files
def parse_env_file(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                m = re.match(r"\s*export\s+([A-Z0-9_]+)=(.*)", line)
                if m:
                    key, val = m.group(1), m.group(2).strip()
                    if val.startswith('"') and val.endswith('"'):
                        val = val[1:-1]
                    elif val.startswith("'") and val.endswith("'"):
                        val = val[1:-1]
                    out[key] = val
    except OSError:
        pass
    return out


def write_env_file(path, values, header=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    if header:
        lines.append(header)
    for k, v in values.items():
        lines.append(f'export {k}="{v}"')
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_env_all():
    out = {}
    for name in ENV_FILES:
        out.update(parse_env_file(os.path.join(ROOT, name)))
    return out


# ------------------------------------------------------------------ secrets.h
def parse_secrets(path=SECRETS_H):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                m = re.match(r'\s*#define\s+([A-Z0-9_]+)\s+"([^"]*)"', line)
                if m:
                    out[m.group(1)] = m.group(2)
    except OSError:
        pass
    return out


def write_secrets(d):
    template = (
        "#pragma once\n"
        "// Локальные секреты (не коммитить). Генерируются WebUI: страница «Настройки».\n"
        '#define WIFI_SSID "{WIFI_SSID}"\n'
        '#define WIFI_PASSWORD "{WIFI_PASSWORD}"\n'
        '#define OTA_PASSWORD "{OTA_PASSWORD}"\n'
        '#define AUTH_TOKEN "{AUTH_TOKEN}"\n'
        '#define STATIC_IP "{STATIC_IP}"\n'
    )
    with open(SECRETS_H, "w") as f:
        f.write(template.format(**d))


# ------------------------------------------------------------------ settings.json
def default_settings():
    sec = parse_secrets()
    env = read_env_all()
    return {
        "esp32": {
            "ssid": sec.get("WIFI_SSID", ""),
            "wifi_password": sec.get("WIFI_PASSWORD", ""),
            "ota_password": sec.get("OTA_PASSWORD", ""),
            "auth_token": sec.get("AUTH_TOKEN", ""),
            "static_ip": sec.get("STATIC_IP", "192.168.0.200"),
        },
        "assistant": {
            "llm_url": os.getenv("LLM_URL", "http://localhost:1234/v1/chat/completions"),
            "llm_model": os.getenv("LLM_MODEL", "qwen2.5-7b-instruct"),
            "wake_words": os.getenv("WAKE_WORDS", "неко,нэко,neko"),
            "wake_ratio": float(os.getenv("WAKE_RATIO", "0.72")),
            "max_listen_s": float(os.getenv("MAX_LISTEN_S", "5")),
            "volume": float(os.getenv("VOLUME", "0.85")),
            "esp32_port": int(os.getenv("ESP32_PORT", "4211")),
            "esp32_ctrl_port": int(os.getenv("ESP32_CTRL_PORT", "4212")),
        },
        "env": env,
    }


def load():
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict) and "esp32" in data:
            return data
    except (OSError, ValueError):
        pass
    d = default_settings()
    return d


def _merge_env(existing, incoming):
    """incoming: {KEY: value|CLEAR}. CLEAR удаляет ключ."""
    out = dict(existing)
    for k, v in incoming.items():
        if v == CLEAR:
            out.pop(k, None)
        else:
            out[k] = v
    return out


def save(payload):
    cur = load()
    notes = []

    # ---- esp32 -> secrets.h
    esp32 = payload.get("esp32") or {}
    sec = parse_secrets()
    for k in ("WIFI_SSID", "WIFI_PASSWORD", "OTA_PASSWORD", "AUTH_TOKEN", "STATIC_IP"):
        src = esp32.get({
            "WIFI_SSID": "ssid", "WIFI_PASSWORD": "wifi_password",
            "OTA_PASSWORD": "ota_password", "AUTH_TOKEN": "auth_token",
            "STATIC_IP": "static_ip",
        }[k])
        if src is not None and src != CLEAR and src != "":
            sec[k] = str(src)
    static_ip = sec.get("STATIC_IP", "192.168.0.200")
    write_secrets(sec)
    notes.append("secrets.h обновлён (SSID/пароль/токены/IP)")

    # ---- env -> *_env.sh
    env_in = payload.get("env") or {}
    env_cur = cur.get("env") or {}
    env_new = _merge_env(env_cur, env_in)
    for fname, keys in ENV_FILES.items():
        values = {k: env_new[k] for k in keys if k in env_new}
        path = os.path.join(ROOT, fname)
        header = None
        if fname == "music_env.sh":
            header = ('# Токен Yandex Music (разовый вход). НЕ коммитить, chmod 600.\n'
                      '# Получить: зайди по ссылке в браузере, авторизуйся, разреши доступ,\n'
                      '# в поле address вставь GET-параметр access_token=…')
        write_env_file(path, values, header=header)
    if env_in:
        notes.append("env-файлы (ha/music/miio) обновлены")

    # ---- assistant + webui_password -> settings.json
    cur.update(payload)
    cur["esp32"] = {
        "ssid": sec.get("WIFI_SSID", ""),
        "wifi_password": sec.get("WIFI_PASSWORD", ""),
        "ota_password": sec.get("OTA_PASSWORD", ""),
        "auth_token": sec.get("AUTH_TOKEN", ""),
        "static_ip": static_ip,
    }
    cur["env"] = env_new
    with open(SETTINGS_PATH, "w") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass
    notes.append("settings.json сохранён")
    return notes


# ------------------------------------------------------------------ password
def webui_password():
    p = os.getenv("WEBUI_PASSWORD")
    if p:
        return p
    try:
        data = json.load(open(SETTINGS_PATH))
        return data.get("webui_password") or ""
    except (OSError, ValueError):
        return ""


def check_password(p):
    real = webui_password()
    if not real or not p:
        return False
    return hmac.compare_digest(real, p)


def set_password(p):
    if webui_password():
        return False, "пароль уже задан (сменить — через env WEBUI_PASSWORD и перезапуск)"
    p = (p or "").strip()
    if len(p) < 4:
        return False, "пароль слишком короткий"
    data = load()
    data["webui_password"] = p
    with open(SETTINGS_PATH, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass
    return True, "пароль задан"


def gen_token(nbytes=16):
    return _secrets.token_hex(nbytes)


# ------------------------------------------------------------------ masked view
def masked_view(payload):
    out = {
        "password_set": bool(webui_password()),
        "assistant": payload.get("assistant", {}),
    }
    esp = dict(payload.get("esp32", {}))
    for k in ("wifi_password", "ota_password", "auth_token"):
        esp["has_" + k] = bool(esp.get(k))
        if esp.get(k):
            esp[k] = "••••••"
    out["esp32"] = esp

    env_list = []
    env = payload.get("env") or {}
    for key, val in env.items():
        env_list.append({
            "key": key,
            "value": ("••••••" if key in SECRET_KEYS else val),
            "set": bool(val),
            "secret": key in SECRET_KEYS,
        })
    env_list.sort(key=lambda e: e["key"])
    out["env"] = env_list
    return out


# ------------------------------------------------------------------ IP check (только текущий адрес)
def host_busy(ip, timeout=0.4):
    probe = subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                           capture_output=True, timeout=2)
    if probe.returncode == 0:
        return True
    for port in (80, 443):
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            pass
    return False


# ------------------------------------------------------------------ OTA flash
_flash = {"proc": None, "started": 0.0}


def flash_start():
    if _flash["proc"] is not None and _flash["proc"].poll() is None:
        return False, "прошивка уже идёт"
    log = open(FLASH_LOG, "a")
    log.write(f"\n===== flash start {time.strftime('%H:%M:%S')} =====\n")
    log.flush()
    proc = subprocess.Popen(
        ["bash", os.path.join(ROOT, "webui_flash.sh")],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    _flash["proc"] = proc
    _flash["started"] = time.time()
    return True, "прошивка запущена"


def flash_status(lines=80):
    proc = _flash["proc"]
    running = proc is not None and proc.poll() is None
    exitcode = None if running else (proc.returncode if proc else None)
    tail = ""
    try:
        with open(FLASH_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 30000))
            tail = f.read().decode(errors="replace")
    except OSError:
        pass
    return {"running": running, "exit": exitcode, "log": tail[-4000:]}