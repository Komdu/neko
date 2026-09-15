#!/usr/bin/env bash
# Запуск умной колонки (ESP32): VAD -> Whisper -> Qwen (LM Studio) -> Silero -> TCP
set -e

cd "$(dirname "$0")"

# HA-креды (токен в ha_env.sh, chmod 600)
if [ -f ha_env.sh ]; then
    # shellcheck disable=SC1091
    source ha_env.sh
fi

# LAN-токены Xiaomi (miio_env.sh, chmod 600)
if [ -f miio_env.sh ]; then
    # shellcheck disable=SC1091
    source miio_env.sh
fi

# Yandex Music (music_env.sh, chmod 600)
if [ -f music_env.sh ]; then
    # shellcheck disable=SC1091
    source music_env.sh
fi

echo "Загрузка моделей (torch/whisper/silero)... ~20-30 сек, ждите"

HSA_OVERRIDE_GFX_VERSION=10.3.0 \
TMPDIR=/mnt/hdd/neko/tmp \
/mnt/hdd/neko/venv/bin/python -u /mnt/hdd/neko/assistant.py