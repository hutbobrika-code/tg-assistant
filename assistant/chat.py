import random
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import log, models, now, web
from .config import ROOT
from .llm import LLMError, human_error, stream
from .markdown import (
    drop_images,
    duration,
    plain,
    prepare,
    split_long,
    thinking_block,
    thinking_title,
    web_images,
    without_followups,
)
from .telegram import TelegramError

PROMPT = (ROOT / "prompt.md").read_text(encoding="utf-8")
FILE_TURNS = 4  # файлы отдаём модели только из последних сообщений, старые заменяем пометкой
TIME_LIMIT = 20 * 60  # дольше этого ответ не ждём
BUTTONS_LIMIT = 300


class Draft:
    # черновик, который телега показывает пока модель думает и пишет.
    # живёт 30 секунд, поэтому обновляем его и тогда, когда модель молчит
    def __init__(self, tg, chat):
        self.tg = tg
        self.chat = chat
        self.id = random.randint(1, 2**31 - 1)
        self.md = None
        self.sent = None
        self.sent_at = 0.0
        self.lock = threading.Lock()
        self.closed = threading.Event()
        threading.Thread(target=self.keepalive, daemon=True).start()

    def show(self, md):
        self.md = md
        if time.monotonic() - self.sent_at >= 0.9:
            self.push()

    def push(self):
        with self.lock:
            md = self.md
            if md is None or self.closed.is_set():
                return
            try:
                self.tg.draft(self.chat, self.id, md)
            except TelegramError as e:
                # недописанный markdown иногда не парсится, это не страшно
                log(f"черновик: {e}")
            self.sent, self.sent_at = md, time.monotonic()

    def keepalive(self):
        while not self.closed.wait(1):
            idle = time.monotonic() - self.sent_at
            if (self.md != self.sent and idle >= 0.9) or idle >= 20:
                self.push()

    def close(self):
        self.closed.set()


class Reply:
    # всё, что накопилось за один ответ
    def __init__(self):
        self.thinking = ""
        self.text = ""
        self.status = ""  # что модель делает прямо сейчас: ищет, читает
        self.seen = []  # запросы и страницы из интернета


class Responder:
    def __init__(self, tg, store):
        self.tg = tg
        self.store = store
        self.files = {}  # file_id -> байты, кэш на смену
        self.running = {}  # id черновика -> Event остановки

    def stop(self, draft_id):
        event = self.running.get(draft_id)
        if event:
            event.set()

    def answer(self, chat_id, reply_to, user=None, asked=None):
        conf = self.store.chat(chat_id)
        model = models.get(conf["model"])
        effort = models.effort_for(model, conf["effort"])
        messages = self.context(chat_id, model)
        r = Reply()
        tools = (lambda name, args: web.run(name, args, r.seen)) if conf.get("web", True) else None

        draft = Draft(self.tg, chat_id)
        stop = threading.Event()
        timer = threading.Timer(TIME_LIMIT, stop.set)
        timer.start()
        self.running[draft.id] = stop
        draft.show("<tg-thinking>Думаю…</tg-thinking>")

        error = None
        started = time.monotonic()
        try:
            for kind, piece in stream(model, system_prompt(user), messages, effort, stop, tools):
                if kind == "thinking":
                    r.thinking += piece
                elif kind == "text":
                    r.text += piece
                    r.status = ""
                else:
                    on_tool(r, *piece)
                draft.show(preview(r))
        except LLMError as e:
            error = e
            log(f"модель: {e}")
        finally:
            timer.cancel()
            draft.close()
            self.running.pop(draft.id, None)
        seconds = time.monotonic() - started

        if not r.text.strip():
            if stop.is_set():
                reason = "остановлено" if seconds < TIME_LIMIT else "слишком долго думала"
            else:
                reason = human_error(error) if error else "пустой ответ"
            return self.tg.send(chat_id, f"⚠️ Модель не ответила: {plain(reason)}",
                                reply_to=reply_to, markup=self.keyboard([], retry=True))

        md, images, followups = prepare(r.text)
        mode = f" · {models.EFFORTS[effort]}" if effort else ""
        stopped = stop.is_set() and seconds < TIME_LIMIT
        parts = [
            f"> 💬 {plain(asked)}" if asked else "",
            thinking_block(r.thinking, seconds),
            web_block(r.seen),
            md,
            "_⏹ Остановлено_" if stopped else "",
            f"_⚠️ Ответ оборвался: {plain(human_error(error))}_" if error else "",
            f"<footer>{model.name}{mode} · {duration(seconds)}</footer>",
        ]
        full = "\n\n".join(p for p in parts if p)
        first, last = self.deliver(chat_id, full, images, reply_to, self.keyboard(followups),
                                   r.text)
        self.store.remember(chat_id, {"role": "assistant", "text": without_followups(r.text),
                                      "msg": first, "last": last})
        self.store.save()

    def deliver(self, chat_id, full, images, reply_to, keyboard, raw):
        # картинки из интернета телега качает сама; если не сможет - не отправит всё сообщение
        urls = web_images(full)
        if urls:
            with ThreadPoolExecutor(4) as pool:
                checks = list(pool.map(web.image_ok, urls))
            bad = [u for u, ok in zip(urls, checks, strict=True) if not ok]
            if bad:
                full = drop_images(full, bad)

        for attempt in (full, drop_images(full)):
            try:
                return self.send_parts(chat_id, attempt, images, reply_to, keyboard)
            except TelegramError as e:
                log(f"rich не отправился: {e}")
            if not urls:
                break

        # совсем крайний случай: отправляем обычным текстом, чтобы ответ всё равно дошёл
        first = last = None
        for i in range(0, len(raw), 4000):
            is_last = i + 4000 >= len(raw)
            msg = self.tg.send(chat_id, raw[i:i + 4000], reply_to=reply_to if i == 0 else None,
                               markup=keyboard if is_last else None, html=False)
            first, last = first or msg["message_id"], msg["message_id"]
        return first, last

    def send_parts(self, chat_id, full, images, reply_to, keyboard):
        chunks = split_long(full)
        first = last = None
        for i, chunk in enumerate(chunks):
            used = [img for img in images if f"id={img[0]})" in chunk]
            is_last = i == len(chunks) - 1
            msg = self.tg.send_rich(chat_id, chunk, reply_to=reply_to if i == 0 else None,
                                    markup=keyboard if is_last else None, images=used)
            first, last = first or msg["message_id"], msg["message_id"]
        return first, last

    def keyboard(self, followups, retry=False):
        buttons = self.store.data.setdefault("buttons", {})
        rows = []
        for question in followups:
            key = uuid.uuid4().hex[:12]
            buttons[key] = question
            rows.append([{"text": f"💬 {question}", "callback_data": f"ask:{key}"}])
        for key in list(buttons)[:-BUTTONS_LIMIT]:
            del buttons[key]
        again = "🔁 Повторить" if retry else "🔄 Заново"
        rows.append([{"text": again, "callback_data": "again"},
                     {"text": "⚙️ Настройки", "callback_data": "menu"}])
        return {"inline_keyboard": rows}

    def context(self, chat_id, model):
        history = list(self.store.chat(chat_id)["history"])
        recent = {i for i, h in enumerate(history) if h["role"] == "user"}
        recent = set(sorted(recent)[-FILE_TURNS:])
        messages = []
        for i, h in enumerate(history):
            msg = {"role": h["role"], "text": h.get("text", "")}
            notes = []
            for f in h.get("files", []):
                readable = (f["mime"].startswith("image/") and model.vision
                            or f["mime"] == "application/pdf" and model.pdf)
                if not readable:
                    notes.append(f"[{kind_of(f)}: эта модель такие файлы не видит]")
                elif i not in recent:
                    notes.append(f"[{kind_of(f)} из начала разговора]")
                else:
                    data = self.file(f["file_id"])
                    if data:
                        msg.setdefault("files", []).append({**f, "data": data})
                    else:
                        notes.append(f"[{kind_of(f)}: не удалось скачать]")
            if notes:
                msg["text"] = (msg["text"] + "\n\n" + "\n".join(notes)).strip()
            messages.append(msg)
        return messages

    def file(self, file_id):
        if file_id not in self.files:
            try:
                self.files[file_id] = self.tg.download(file_id)
            except (TelegramError, OSError) as e:
                log(f"файл {file_id}: {e}")
                return None
        return self.files[file_id]


def on_tool(r, name, args):
    # текст до похода в интернет обычно "сейчас поищу" - переносим его в ход мыслей
    if r.text.strip():
        r.thinking += "\n\n" + r.text
        r.text = ""
    if name == "web_search":
        r.status = f"🔎 Ищу: {args.get('query', '')}"
    elif name == "open_page":
        r.status = f"📄 Читаю {web.domain(str(args.get('url', '')))}"


def preview(r):
    if r.text.strip():
        return prepare(r.text, final=False)[0] or "<tg-thinking>Пишу…</tg-thinking>"
    title = r.status or thinking_title(r.thinking) or "Думаю…"
    return f"<tg-thinking>{plain(title)}</tg-thinking>"


def web_block(seen):
    if not seen:
        return ""
    queries = [v for k, v in seen if k == "search"]
    pages = list(dict.fromkeys(v for k, v in seen if k in ("page", "source")))
    lines = [f"- 🔎 {plain(q)}" for q in queries]
    lines += [f"- [{plain(web.domain(u))}]({u})" for u in pages[:10]]
    title = f"🌐 Искал в интернете · {len(queries)} запр." if queries else "🌐 Читал страницы"
    return f"<details><summary>{title}</summary>\n\n" + "\n".join(lines) + "\n\n</details>"


def system_prompt(user=None):
    name = (user or {}).get("first_name") or "не представился"
    return PROMPT.replace("{now}", f"{now():%d.%m.%Y %H:%M} (МСК)").replace("{name}", name)


def kind_of(f):
    if f["mime"].startswith("image/"):
        return "фото"
    if f["mime"] == "application/pdf":
        return f"PDF {f.get('name', '')}".strip()
    return f"файл {f.get('name', '')}".strip()
