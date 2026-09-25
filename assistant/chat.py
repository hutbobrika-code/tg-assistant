import random
import threading
import time
import uuid

from . import log, models, now
from .config import ROOT
from .llm import LLMError, stream
from .markdown import (
    duration,
    plain,
    prepare,
    split_long,
    thinking_block,
    thinking_title,
    without_followups,
)
from .telegram import TelegramError

PROMPT = (ROOT / "prompt.md").read_text(encoding="utf-8")
FILE_TURNS = 4  # файлы отдаём модели только из последних сообщений, старые заменяем пометкой


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


class Responder:
    def __init__(self, tg, store):
        self.tg = tg
        self.store = store
        self.followups = {}  # ключ кнопки -> вопрос
        self.files = {}  # file_id -> байты, кэш на смену
        self.running = {}  # id черновика -> Event остановки

    def stop(self, draft_id):
        event = self.running.get(draft_id)
        if event:
            event.set()

    def answer(self, chat_id, reply_to, asked=None):
        conf = self.store.chat(chat_id)
        model = models.get(conf["model"])
        effort = models.effort_for(model, conf["effort"])
        messages = self.context(chat_id, model)

        draft = Draft(self.tg, chat_id)
        stop = threading.Event()
        self.running[draft.id] = stop
        draft.show("<tg-thinking>Думаю…</tg-thinking>")

        thinking, text, error = "", "", None
        started = time.monotonic()
        try:
            for kind, piece in stream(model, system_prompt(), messages, effort, stop):
                if kind == "thinking":
                    thinking += piece
                else:
                    text += piece
                draft.show(preview(thinking, text))
        except LLMError as e:
            error = str(e)
            log(f"модель: {e}")
        finally:
            draft.close()
            self.running.pop(draft.id, None)
        seconds = time.monotonic() - started

        if not text.strip():
            reason = "остановлено" if stop.is_set() else (error or "пустой ответ")
            self.tg.send(chat_id, f"⚠️ Модель не ответила: {plain(reason)}", reply_to=reply_to,
                         markup=self.keyboard([]))
            return

        md, images, followups = prepare(text)
        mode = f" · {models.EFFORTS[effort]}" if effort else ""
        parts = [
            f"> 💬 {plain(asked)}" if asked else "",
            thinking_block(thinking, seconds),
            md,
            "_⏹ Остановлено_" if stop.is_set() else "",
            f"_⚠️ Ответ оборвался: {plain(error)}_" if error else "",
            f"<footer>{model.name}{mode} · {duration(seconds)}</footer>",
        ]
        full = "\n\n".join(p for p in parts if p)
        sent = self.deliver(chat_id, full, images, reply_to, self.keyboard(followups), text)
        self.store.remember(chat_id, {"role": "assistant", "text": without_followups(text),
                                      "msg": sent})
        self.store.save()

    def deliver(self, chat_id, full, images, reply_to, keyboard, raw):
        chunks = split_long(full)
        sent = None
        try:
            for i, chunk in enumerate(chunks):
                used = [img for img in images if f"id={img[0]})" in chunk]
                last = i == len(chunks) - 1
                msg = self.tg.send_rich(chat_id, chunk, reply_to=reply_to if i == 0 else None,
                                        markup=keyboard if last else None, images=used)
                sent = sent or msg["message_id"]
            return sent
        except TelegramError as e:
            # если телега не приняла разметку, ответ всё равно должен дойти
            log(f"rich не отправился, шлю текстом: {e}")
        for i in range(0, len(raw), 4000):
            last = i + 4000 >= len(raw)
            msg = self.tg.send(chat_id, raw[i:i + 4000], reply_to=reply_to if i == 0 else None,
                               markup=keyboard if last else None, html=False)
            sent = sent or msg["message_id"]
        return sent

    def keyboard(self, followups):
        rows = []
        for question in followups:
            key = uuid.uuid4().hex[:12]
            self.followups[key] = question
            rows.append([{"text": f"💬 {question}", "callback_data": f"ask:{key}"}])
        rows.append([{"text": "🔄 Заново", "callback_data": "again"},
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


def preview(thinking, text):
    if text.strip():
        return prepare(text, final=False)[0] or "<tg-thinking>Пишу…</tg-thinking>"
    title = thinking_title(thinking)
    return f"<tg-thinking>{plain(title) if title else 'Думаю…'}</tg-thinking>"


def system_prompt():
    return PROMPT.replace("{date}", f"{now():%d.%m.%Y}")


def kind_of(f):
    if f["mime"].startswith("image/"):
        return "фото"
    if f["mime"] == "application/pdf":
        return f"PDF {f.get('name', '')}".strip()
    return f"файл {f.get('name', '')}".strip()
