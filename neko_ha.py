import json
import os
import re

import requests

HA_URL = os.getenv("HA_URL").rstrip("/") if os.getenv("HA_URL") else ""
HA_TOKEN = os.getenv("HA_TOKEN", "")

_ATTR_KEYS = (
    "friendly_name", "unit_of_measurement", "battery_level", "charging",
    "cleaning_mode", "fan_speed", "current_position", "status", "humidity",
    "temperature", "preset_mode", "color_temp", "brightness", "rssi",
)


class HAError(RuntimeError):
    pass


def _headers():
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


def ha_enabled() -> bool:
    return bool(HA_TOKEN)


def get_state(entity_id: str) -> dict:
    if not ha_enabled():
        raise HAError("HA не подключён: нет HA_TOKEN")
    r = requests.get(f"{HA_URL}/api/states/{entity_id}", headers=_headers(), timeout=15)
    if r.status_code == 404:
        raise HAError(f"сущность {entity_id} не найдена в Home Assistant")
    if r.status_code != 200:
        raise HAError(f"HA ответил {r.status_code}: {r.text[:200]}")
    s = r.json()
    attrs = {k: s.get("attributes", {}).get(k) for k in _ATTR_KEYS
             if k in s.get("attributes", {})}
    return {"entity_id": s["entity_id"], "state": s["state"], **attrs}


def list_states(include_unavailable: bool = False) -> list[dict]:
    if not ha_enabled():
        raise HAError("HA не подключён: нет HA_TOKEN")
    r = requests.get(f"{HA_URL}/api/states", headers=_headers(), timeout=15)
    r.raise_for_status()
    out = []
    for s in r.json():
        if not include_unavailable and s.get("state") in ("unavailable", "unknown"):
            continue
        out.append({"entity_id": s["entity_id"],
                    "state": s["state"],
                    "friendly_name": s.get("attributes", {}).get("friendly_name", "")})
    return out


def find_entity(fragment: str) -> list[dict]:
    """Поиск сущности по обрывку имени/юзер-friendly названия («виталя»)."""
    f = fragment.lower().replace("ё", "е")
    hits = []
    for s in list_states(include_unavailable=True):
        hay = f"{s['entity_id']} {s['friendly_name']}".lower().replace("ё", "е")
        if f in hay:
            hits.append(s)
    return hits


def call_service(domain: str, service: str, entity_id: str | None = None,
                 **data) -> dict:
    if not ha_enabled():
        raise HAError("HA не подключён: нет HA_TOKEN")
    payload = dict(data)
    if entity_id:
        payload["entity_id"] = entity_id
    r = requests.post(f"{HA_URL}/api/services/{domain}/{service}",
                      headers=_headers(), json=payload, timeout=20)
    if r.status_code != 200:
        detail = r.text[:200] if r.text else r.status_code
        raise HAError(f"HA: {domain}.{service} не удался: {detail}")
    return {"ok": True, "domain": domain, "service": service, "entity_id": entity_id}


def _normalize(value):
    if isinstance(value, str) and re.fullmatch(r"\d+", value):
        return int(value)
    return value


TOOLS = [
    {"type": "function",
     "function": {"name": "ha_get_state",
                  "description": "Узнать текущее состояние сущности Home Assistant "
                                 "(свет, пылесос, погода, заряд устройства и т.п.). "
                                 "Указывай ТОЧНЫЙ entity_id; если не уверен — сначала "
                                 "найди сущность через ha_find_entity",
                  "parameters": {"type": "object",
                                 "properties": {"entity_id": {"type": "string",
                                                              "description": "entity_id в HA, напр. sun.sun или vacuum.koridor_vitalia"}},
                                 "required": ["entity_id"]}}},
    {"type": "function",
     "function": {"name": "ha_find_entity",
                  "description": "Найти сущность в HA по обрывку названия, если точный "
                                 "entity_id неизвестен («виталя», «список покупок» и т.п.)",
                  "parameters": {"type": "object",
                                 "properties": {"fragment": {"type": "string",
                                                              "description": "подстрока имени или entity_id"}},
                                 "required": ["fragment"]}}},
    {"type": "function",
     "function": {"name": "ha_call_service",
                  "description": "Вызвать сервис Home Assistant: включить/выключить свет, "
                                 "запустить/поставить на паузу/отправить домой пылесос, "
                                 "добавить в список покупок и т.п.",
                  "parameters": {"type": "object",
                                 "properties": {
                                     "domain": {"type": "string", "description": "домен сервиса, напр. light, vacuum, todo"},
                                     "service": {"type": "string", "description": "название сервиса, напр. turn_on, fold_to_base"},
                                     "entity_id": {"type": "string", "description": "целевая сущность (необязательно)"},
                                     "data": {"type": "object", "description": "доп. аргументы сервиса (необязательно)"}},
                                 "required": ["domain", "service"]}}},
    {"type": "function",
     "function": {"name": "ha_list_entities",
                  "description": "Перечислить доступные сущности HA (для ориентира)", "parameters": {"type": "object", "properties": {}}}},
]

def run_tool(name: str, args: dict):
    args = args or {}
    if name == "ha_get_state":
        return get_state(str(args["entity_id"]))
    if name == "ha_find_entity":
        return find_entity(str(args["fragment"]))
    if name == "ha_call_service":
        data = {k: _normalize(v) for k, v in (args.get("data") or {}).items()}
        return call_service(str(args["domain"]), str(args["service"]),
                            args.get("entity_id"), **data)
    if name == "ha_list_entities":
        return list_states()
    raise HAError(f"неизвестный инструмент {name}")


TOOL_NAMES = frozenset(t["function"]["name"] for t in TOOLS)


if __name__ == "__main__":
    print(json.dumps(run_tool("ha_find_entity", {"fragment": "виталя"}), ensure_ascii=False, indent=1))
    print(json.dumps(run_tool("ha_get_state", {"entity_id": "weather.forecast_home"}), ensure_ascii=False, indent=1))