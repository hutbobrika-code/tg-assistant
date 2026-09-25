import html
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import log

EXA = "https://mcp.exa.ai/mcp"  # публичный mcp-сервер exa, ключ не нужен
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

TOOLS = [
    {
        "name": "web_search",
        "description": "Поиск в интернете. Нужен для всего, что могло измениться: цены, курсы, "
                       "новости, характеристики техники, расписания, свежие версии. Возвращает "
                       "страницы с выдержками.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string",
                                     "description": "подробный запрос, можно на любом языке"}},
            "required": ["query"],
        },
    },
    {
        "name": "open_page",
        "description": "Прочитать страницу по ссылке: текст и ссылки на картинки со страницы.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
]


class Exa:
    def __init__(self):
        self.session = None
        self.lock = threading.Lock()
        self.ids = 0

    def call(self, tool, args):
        for attempt in range(2):
            try:
                with self.lock:
                    if not self.session:
                        self.session = self.rpc("initialize", {
                            "protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "tg-assistant", "version": "1"}})[0]
                res = self.rpc("tools/call", {"name": tool, "arguments": args})[1]
                if "error" in res:
                    raise RuntimeError(res["error"].get("message", res["error"]))
                return "\n".join(c.get("text", "") for c in res["result"].get("content", []))
            except (OSError, ValueError, KeyError, RuntimeError) as e:
                self.session = None  # сессия могла протухнуть, пробуем заново
                if attempt:
                    raise RuntimeError(f"поиск не ответил: {e}") from None
        return ""

    def rpc(self, method, params):
        self.ids += 1
        headers = {"Content-Type": "application/json", "User-Agent": "tg-assistant/1.0",
                   "Accept": "application/json, text/event-stream"}
        if self.session and method != "initialize":
            headers["Mcp-Session-Id"] = self.session
        body = {"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params}
        req = urllib.request.Request(EXA, json.dumps(body).encode(), headers)
        with urllib.request.urlopen(req, timeout=90) as r:
            session = r.headers.get("Mcp-Session-Id") or self.session
            raw = r.read().decode("utf-8", "replace")
        data = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
        return session, json.loads(data[-1] if data else raw)


exa = Exa()


def run(name, args, seen):
    # выполняет инструмент модели. seen - куда складываем запросы и страницы для отчёта
    try:
        if name == "web_search":
            query = str(args.get("query", "")).strip()
            seen.append(("search", query))
            text = exa.call("web_search_exa", {"query": query, "numResults": 6})
            for url in re.findall(r"^URL: (\S+)", text, re.M):
                seen.append(("source", url))
            return text[:14000] or "ничего не нашлось"
        if name == "open_page":
            url = str(args.get("url", "")).strip()
            seen.append(("page", url))
            text = exa.call("web_fetch_exa", {"urls": [url], "maxCharacters": 12000})
            images = page_images(url)
            if images:
                text += "\n\nКартинки со страницы:\n" + "\n".join(images)
            return text or "страница пустая"
        return f"нет такого инструмента: {name}"
    except RuntimeError as e:
        log(e)
        return f"ошибка: {e}"


def page_images(url):
    # og:image и т.п. - главная картинка страницы, её можно показать в ответе
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=10) as r:
            if "html" not in r.headers.get("Content-Type", ""):
                return []
            page = r.read(400_000).decode("utf-8", "replace")
    except (OSError, ValueError):
        return []
    found = re.findall(r'<meta[^>]+(?:property|name)="(?:og:image|twitter:image)(?::src)?"'
                       r'[^>]+content="([^"]+)"', page)
    found += re.findall(r'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="og:image"', page)
    out = []
    for src in found:
        src = urllib.parse.urljoin(url, html.unescape(src))
        if src.startswith("http") and src not in out:
            out.append(src)
    return out[:4]


def image_ok(url):
    # телега сама скачивает картинку по ссылке, и если не сможет - не отправит всё сообщение
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": "bytes=0-2047"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.headers.get("Content-Type", "").startswith("image/")
    except (OSError, ValueError):
        return False


def domain(url):
    return urllib.parse.urlparse(url).netloc.removeprefix("www.")
