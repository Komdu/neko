# ESP32 Smart Speaker (форк `esp32_radio`)

Голосовой ассистент «умная колонка»: ESP32 + MAX98357A (+ динамик) + INMP441 (микрофон).
ПК делает Whisper (STT) → Qwen 7B (LLM в LM Studio) → Silero (TTS).

## Аппаратная часть

```
ESP32 GPIO26 ──→ MAX98357A BCLK     ESP32 GPIO18 ──→ INMP441 SCK
ESP32 GPIO25 ──→ MAX98357A LRC      ESP32 GPIO19 ──→ INMP441 WS
ESP32 GPIO22 ──→ MAX98357A DIN      ESP32 GPIO34 ──→ INMP441 SD
ESP32 5V      ──→ MAX98357A VIN     ESP32 3.3V    ──→ INMP441 VDD
динамик ───────→ MAX98357A OUT       ESP32 GND     ──→ INMP441 GND
                                    INMP441 L/R   ──→ GND (левый канал)
```

Два независимых I2S-контроллера:
- `I2S0` TX — динамик, **48 кГц**, моно→стерео дубль.
- `I2S1` RX — микрофон, **16 кГц**, 32-бит фрейм (INMP441 24-бит >> 14).

> Не путай: MAX98357A — усилитель (выход), INMP441 — микрофон (вход). Оба I2S,
> но тактовые частоты у них разные, поэтому отдельные линии SCK/WS.

## Схема работы

```
ESP32 (TCP сервер, порт 4211)          ПК
┌────────────────────────┐      ┌─────────────────────────────┐
│ микрофон → I2S1 16к    │ ───► │ assistant.py: VAD → Whisper │
│ TTS-аудио → I2S0 48к   │ ◄─── │ Qwen (LM Studio) → Silero  │
└────────────────────────┘      └─────────────────────────────┘
```

Одно TCP-соединение двухнаправленное: ESP32 постоянно шлёт микрофон (16к, s16, моно),
а читает из того же сокета TTS-аудио и играет его (48к, s16, моно).

## Прошивка

```
cd esp32_speaker
../.venv/bin/pio run -t upload
```

WiFi/статический IP в `src/main.cpp` (по умолчанию `192.168.0.200`, ssid/password те же).

## ПК — обработчик (`assistant.py`)

Запуск (нужен venv с ROCm-torch/whisper из `/mnt/hdd/neko/venv`):

```
HSA_OVERRIDE_GFX_VERSION=10.3.0 \
  /mnt/hdd/neko/venv/bin/python /mnt/hdd/neko/assistant.py
```

Параметры через env: `ESP32_HOST` (default `192.168.0.200`), `ESP32_PORT` (`4211`).

Что делает: слушает микрофон, Silero VAD выхватывает фразу (после >600мс тишины),
Whisper small распознаёт, Qwen через LM Studio (`localhost:1234`) отвечает,
Silero v5_5_ru/kseniya синтезирует 48к/16-бит и шлёт по сокету — ESP32 играет.

Веб-интерфейс (браузерный вариант) — сервер `/mnt/hdd/neko/server.py`, см. там.