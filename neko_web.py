"""Инструмент web_search для Нэко: обычная поисковая выдача (DuckDuckGo), без ключей."""
import html as _html
import json
import re

import requests

DDG_URL = "https://html.duckduckgo.com/html/"
DDG_LITE = "https://lite.duckduckgo.com/lite/"
UA_BROWSER = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126 Safari/537.36")


class WebError(RuntimeError):
    pass


def _real_url(u: str) -> str:
    """DDG оборачивает ссылки в //duckduckgo.com/l/?uddg=... — достаём настоящий URL."""
    if "uddg=" in u:
        from urllib.parse import parse_qs, urlparse
        vals = parse_qs(urlparse(u).query).get("uddg")
        if vals:
            return vals[0]
    return u


def _parse_html(text: str, limit: int) -> list[dict]:
    out = []
    # каждая строка результата: <a class="result__a" href="...">Title</a> ... <a class="result__snippet">
    for m in re.finditer(
            r'<a[^>]+rel="nofollow"[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
            r'(?:(?!result__a).)*?'
            r'class="result__snippet"[^>]*>(.*?)</a>',
            text, re.S):
        url = _html.unescape(m.group(1))
        title = re.sub("<[^>]+>", "", m.group(2)).strip()
        snippet = re.sub("<[^>]+>", "", m.group(3)).strip()
        out.append({
            "title": _html.unescape(title)[:200],
            "url": _real_url(url)[:300],
            "snippet": _html.unescape(snippet)[:500],
        })
        if len(out) >= limit:
            break
    if out:
        return out
    # облегчённый парсер: только ссылки
    for m in re.finditer(
            r'<a[^>]+rel="nofollow"[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            text, re.S):
        url = _html.unescape(m.group(1))
        title = re.sub("<[^>]+>", "", m.group(2)).strip()
        out.append({"title": _html.unescape(title)[:200], "url": _real_url(url)[:300],
                    "snippet": ""})
        if len(out) >= limit:
            break
    return out


def _parse_lite(text: str, limit: int) -> list[dict]:
    out = []
    for m in re.finditer(
            r'<a[^>]+class="result-link"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.S):
        url = _html.unescape(m.group(1))
        title = re.sub("<[^>]+>", "", m.group(2)).strip()
        out.append({"title": _html.unescape(title)[:200], "url": _real_url(url)[:300],
                    "snippet": ""})
        if len(out) >= limit:
            break
    return out


def web_search(query: str, limit: int = 6) -> dict:
    q = query.strip()
    if not q:
        raise WebError("пустой запрос")
    out = {"query": q, "results": [], "error": None}
    try:
        r = requests.get(DDG_URL, params={"q": q},
                         headers={"User-Agent": UA_BROWSER,
                                  "Referer": "https://duckduckgo.com/"}, timeout=10)
        if r.status_code in (200, 202):
            out["results"] = _parse_html(r.text, limit)
        if not out["results"]:
            r = requests.get(DDG_LITE, params={"q": q},
                             headers={"User-Agent": UA_BROWSER}, timeout=10)
            r.raise_for_status()
            out["results"] = _parse_lite(r.text, limit)
    except Exception as e:
        out["error"] = f"{e!r}"
    return out


TOOLS = [
    {"type": "function",
     "function": {"name": "web_search",
                  "description": "Поиск в интернете по запросу. Возвращает список "
                                 "результатов: заголовок, ссылка, сниппет. Используй для "
                                 "фактов, новостей, рецептов, неизвестных слов. "
                                 "Не выдумывай данные — сначала поищи.",
                  "parameters": {"type": "object",
                                 "properties": {"query": {"type": "string",
                                                          "description": "что искать, например «какая сегодня погода в Москве»"}},
                                 "required": ["query"]}}},
]


def run_tool(name: str, args: dict):
    args = args or {}
    if name == "web_search":
        return web_search(str(args["query"]))
    raise WebError(f"неизвестный инструмент {name}")


TOOL_NAMES = frozenset(t["function"]["name"] for t in TOOLS)


if __name__ == "__main__":
    print(json.dumps(web_search("погода в Москве сегодня"), ensure_ascii=False, indent=1))