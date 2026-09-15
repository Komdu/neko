#!/usr/bin/env bash
# OTA-прошивка ESP32 по сети (нужен рабочий WiFi и запущенная боевая прошивка).
# Устройство: 192.168.0.200, порт 3232, пароль: neko-ota (см. platformio.ini).
# Если WiFi/плата недоступны — только UART: pio run -e esp32dev -t upload
set -e
cd "$(dirname "$0")/esp32_speaker"
# Пароль OTA берём из gitignored secrets.h (не светить в репо); дефолт на всякий случай
OTA_PASSWORD="$(sed -n 's/^#define OTA_PASSWORD "\(.*\)"/\1/p' src/secrets.h | head -1)"
[ -n "$OTA_PASSWORD" ] || OTA_PASSWORD="neko-ota"
export OTA_PASSWORD
echo "OTA-прошивка на 192.168.0.200 ... (колонка перезагрузится туда же)"
pio run -e esp32dev_ota -t upload