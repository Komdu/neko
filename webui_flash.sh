#!/usr/bin/env bash
# Перепрошивка ESP32 по OTA из WebUI «Настройки». Лог идёт в stdout (webui_flash.log).
cd "$(dirname "$0")/esp32_speaker"

if [ ! -f src/secrets.h ]; then
    echo "нет src/secrets.h — сначала сохрани настройки"
    exit 1
fi

OTA_PASSWORD="$(sed -n 's/^#define OTA_PASSWORD "\(.*\)"/\1/p' src/secrets.h | head -1)"
[ -n "$OTA_PASSWORD" ] || OTA_PASSWORD="neko-ota"
export OTA_PASSWORD

echo ">>> OTA-прошивка (пароль из src/secrets.h)"
pio run -e esp32dev_ota -t upload
echo ">>> EXIT=$?"