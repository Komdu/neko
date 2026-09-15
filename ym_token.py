import os
import re
import time

from yandex_music import Client
from yandex_music.exceptions import DeviceAuthError

PATH = "/mnt/hdd/neko/music_env.sh"


def show(code):
    print("=" * 60)
    print("1. Открой в браузере:", code.verification_url)
    print("2. Введи там код:", code.user_code)
    print("    (появится выбор устройства — подтверди)")
    print("=" * 60)
    print("Жду подтверждения входа...")


def main():
    cl = Client()
    token = None
    for attempt in range(1, 4):
        try:
            token = cl.device_auth(on_code=show, timeout=300)
            break
        except DeviceAuthError as e:
            print(f"попытка {attempt} не удалась ({e}); пробую ещё раз...")
            time.sleep(2)
    if not token:
        raise SystemExit("токен не получен после 3 попыток")
    with open(PATH) as f:
        content = f.read()
    content = re.sub(
        r'(export\s+YANDEX_MUSIC_TOKEN=").*?(")',
        r"\g<1>" + token.access_token + r"\g<2>",
        content,
        count=1,
    )
    with open(PATH, "w") as f:
        f.write(content)
    os.chmod(PATH, 0o600)
    print("OK: токен сохранён в", PATH)
    print("firstName:", getattr(token, "access_token", "")[:0] or token.access_token[:8] + "...")


if __name__ == "__main__":
    main()