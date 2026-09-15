import os

from miio.device import Device
from miio.exceptions import DeviceException
from miio.integrations.airpurifier.zhimi.airpurifier_miot import AirPurifierMiot
from miio.miot_device import MiotDevice

DEVICES = {
    "люстра": {"ip": os.getenv("MIIO_YEELIGHT_IP", ""), "token": os.getenv("MIIO_YEELIGHT_TOKEN", ""),
               "model": "yeelink.light.ceiling20", "kind": "light"},
    "очиститель": {"ip": os.getenv("MIIO_PURIFIER_IP", ""), "token": os.getenv("MIIO_PURIFIER_TOKEN", ""),
                   "model": "xiaomi.airp.cpa4", "kind": "purifier"},
    "аэрогриль": {"ip": os.getenv("MIIO_FRYER_IP", ""), "token": os.getenv("MIIO_FRYER_TOKEN", ""),
                  "model": "xiaomi.fryer.maf14", "kind": "fryer"},
}

ALIASES = {
    "люстра": "люстра", "свет": "люстра", "люстры": "люстра", "люстру": "люстра",
    "очиститель": "очиститель", "воздух": "очиститель", "очиститель воздуха": "очиститель",
    "аэрогриль": "аэрогриль", "фритер": "аэрогриль", "субок": "аэрогриль",
}


class MiioError(RuntimeError):
    pass


def _resolve(name: str) -> str:
    n = ALIASES.get(name.lower(), name.lower())
    if n not in DEVICES:
        raise MiioError(f"не знаю устройство «{name}»: доступны люстра, очиститель, аэрогриль")
    return n


def _client(kind: str, ip: str, token: str, model: str):
    try:
        if kind == "purifier":
            return AirPurifierMiot(ip, token, model=model)
        if kind == "light":
            return Device(ip, token)
        return MiotDevice(ip, token, model=model)
    except DeviceException:
        raise MiioError("устройство не на связи — включи и попробуй ещё раз")


def device_status(name: str) -> dict:
    n = _resolve(name)
    d = DEVICES[n]
    if not d["ip"] or not d["token"]:
        raise MiioError("устройство не настроено (нет токена)")
    c = _client(d["kind"], d["ip"], d["token"], d["model"])
    try:
        if d["kind"] == "purifier":
            s = c.status()
            return {"device": n, "power": "on" if s.power else "off",
                    "fan_level": s.fan_level, "mode": str(s.mode),
                    "filter_days_left": s.filter_life_remaining,
                    "aqi": s.aqi, "pm25": s.pm10_density}
        if d["kind"] == "light":
            vals = c.send("get_prop", ["power", "bright", "cct", "color_mode", "correlated_temp"])
            return {"device": n, "power": vals[0], "brightness": vals[1],
                    "cct": vals[2], "color_mode": vals[3], "correlated_temp": vals[4]}
        props = c.get_properties([])
        return {"device": n, "reachable": bool(props), "props": props[:20]}
    except (DeviceException, OSError, TimeoutError) as e:
        raise MiioError(f"устройство «{n}» не на связи: {e}")


def device_power(name: str, on: bool) -> dict:
    n = _resolve(name)
    d = DEVICES[n]
    c = _client(d["kind"], d["ip"], d["token"], d["model"])
    try:
        if d["kind"] == "purifier":
            c.set_property("power", bool(on))
            return {"device": n, "power": "on" if on else "off"}
        if d["kind"] == "light":
            c.send("set_power", ["on" if on else "off", "smooth", 1000])
            return {"device": n, "power": "on" if on else "off"}
        raise MiioError("аэрогриль пока нельзя включить через этот контур")
    except (DeviceException, OSError, TimeoutError) as e:
        raise MiioError(f"устройство «{n}» не на связи: {e}")


def light_set(name: str, on: bool | None = None, brightness: int | None = None,
              color_temp: int | None = None) -> dict:
    n = _resolve(name)
    d = DEVICES[n]
    if d["kind"] != "light":
        raise MiioError(f"«{n}» — не свет, это {d['kind']}")
    c = _client(d["kind"], d["ip"], d["token"], d["model"])
    try:
        if color_temp is not None:
            c.send("set_cct", [max(2700, min(6500, int(color_temp)))])
        if brightness is not None:
            c.send("set_bright", [max(1, min(100, int(brightness)))])
        if on is not None:
            c.send("set_power", ["on" if on else "off", "smooth", 1000])
        return device_status(n)
    except (DeviceException, OSError, TimeoutError) as e:
        raise MiioError(f"устройство «{n}» не на связи: {e}")


def purifier_set(name: str, on: bool | None = None, level: int | None = None) -> dict:
    n = _resolve(name)
    d = DEVICES[n]
    if d["kind"] != "purifier":
        raise MiioError(f"«{n}» — не очиститель, это {d['kind']}")
    c = _client(d["kind"], d["ip"], d["token"], d["model"])
    try:
        if level is not None:
            c.set_fan_level(max(0, min(3, int(level))))
        if on is not None:
            c.set_property("power", bool(on))
        return device_status(n)
    except (DeviceException, OSError, TimeoutError) as e:
        raise MiioError(f"устройство «{n}» не на связи: {e}")


TOOLS = [
    {"type": "function",
     "function": {"name": "mi_device_status",
                  "description": "Узнать статус устройство по имени: люстра, очиститель воздуха, аэрогриль. "
                                 "Управляет локально по LAN (Xiaomi miio). Если устройство выключено/не на связи — так и ответит.",
                  "parameters": {"type": "object",
                                 "properties": {"name": {"type": "string", "description": "имя устройства"}},
                                 "required": ["name"]}}},
    {"type": "function",
     "function": {"name": "mi_power",
                  "description": "Включить или выключить устройство по имени (люстра, очиститель воздуха). Аэрогриль пока не умеет.",
                  "parameters": {"type": "object",
                                 "properties": {"name": {"type": "string"}, "on": {"type": "boolean"}},
                                 "required": ["name", "on"]}}},
    {"type": "function",
     "function": {"name": "mi_light_set",
                  "description": "Настроить люстру: включить/выключить, яркость 1-100, цветовая температура 2700-6500К.",
                  "parameters": {"type": "object",
                                 "properties": {"name": {"type": "string"},
                                                "on": {"type": "boolean", "description": "необязательно"},
                                                "brightness": {"type": "integer", "description": "1-100, необязательно"},
                                                "color_temp": {"type": "integer", "description": "2700-6500K, необязательно"}},
                                 "required": ["name"]}}},
    {"type": "function",
     "function": {"name": "mi_purifier_set",
                  "description": "Настроить очиститель воздуха: включить/выключить и уровень вентилятора 0-3 (0 выключен).",
                  "parameters": {"type": "object",
                                 "properties": {"name": {"type": "string"},
                                                "on": {"type": "boolean", "description": "необязательно"},
                                                "level": {"type": "integer", "description": "0-3, необязательно"}},
                                 "required": ["name"]}}},
]

TOOL_NAMES = frozenset(t["function"]["name"] for t in TOOLS)


def run_tool(name: str, args: dict):
    args = args or {}
    if name == "mi_device_status":
        return device_status(str(args["name"]))
    if name == "mi_power":
        return device_power(str(args["name"]), bool(args["on"]))
    if name == "mi_light_set":
        return light_set(str(args["name"]),
                         args.get("on"),
                         args.get("brightness"),
                         args.get("color_temp"))
    if name == "mi_purifier_set":
        return purifier_set(str(args["name"]), args.get("on"), args.get("level"))
    raise MiioError(f"неизвестный инструмент {name}")


if __name__ == "__main__":
    import json
    for name in ("очиститель", "люстра", "аэрогриль"):
        try:
            print(name, "->", json.dumps(device_status(name), ensure_ascii=False))
        except MiioError as e:
            print(name, "->", e)