# Телеграм-бот с моделями OpenCode Zen (по умолчанию Muse Spark 1.3 на xhigh).
# Крутится сменами в GitHub Actions, как gemini-check: смена 5 часов, потом следующая.
#
#   python3 bot.py

import queue
import sys
import threading
import time

from assistant import log, models
from assistant.chat import Responder
from assistant.config import settings
from assistant.markdown import plain
from assistant.store import Store
from assistant.telegram import Telegram, TelegramError

TEXT_FILES = (".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".yml", ".yaml", ".html",
              ".css", ".xml", ".log", ".ini", ".toml", ".sql", ".sh", ".java", ".c", ".cpp",
              ".h", ".go", ".rs", ".kt", ".swift", ".php", ".rb")
TEXT_LIMIT = 300_000
FILE_LIMIT = 15_000_000
GATHER = 1.5  # сколько ждём, не допишет ли человек ещё сообщение (длинный текст, альбом)

COMMANDS = [
    {"command": "new", "description": "🧹 Новый диалог"},
    {"command": "model", "description": "🧠 Модель, режим, интернет"},
    {"command": "help", "description": "❔ Что я умею"},
]

EFFORT_HINTS = {
    "minimal": "почти без размышлений, самый быстрый",
    "low": "быстрые ответы на простые вопросы",
    "medium": "баланс скорости и качества",
    "high": "думает дольше, для сложных задач",
    "xhigh": "максимум размышлений, самые точные ответы",
    "max": "предельная глубина, может думать минутами",
}

HELP = """<b>Привет{name}! Я ассистент на {model}</b>

Пиши вопрос или присылай фото, скриншоты, PDF и текстовые файлы — я их читаю. \
Если на картинке мелкий текст, пришли её файлом, без сжатия.

Отвечаю с таблицами, формулами и схемами, могу поискать в интернете. \
Пока думаю, видно, что именно делаю, а если долго — жми «стоп».

/model — модель, режим размышлений, интернет
/new — новый диалог, я забуду предыдущий

Под ответом кнопки: 💬 продолжить разговор, 🔄 переписать ответ.

<i>Muse Spark бесплатна, потому что Meta может учить модели на переписке. \
Совсем личное лучше не присылать.</i>"""


class Bot:
    def __init__(self, tg):
        self.tg = tg
        self.store = Store()
        self.responder = Responder(tg, self.store)
        self.queues = {}
        self.inbox = {}  # chat -> сообщения, которые ещё собираем в один вопрос
        self.saved_offset = self.store.offset
        self.saved_at = time.monotonic()

    def run(self, minutes):
        try:
            self.tg.call("setMyCommands", commands=COMMANDS)
        except TelegramError as e:
            log(e)
        log(f"смена на {minutes} мин")
        end = time.monotonic() + minutes * 60
        while time.monotonic() < end:
            self.flush_inbox()
            self.poll(1 if self.inbox else 50)
            self.save_offset()
        self.finish()

    def poll(self, wait):
        try:
            updates = self.tg.updates(self.store.offset, wait)
        except TelegramError as e:
            log(e)
            time.sleep(5)
            return
        for u in updates:
            self.store.offset = u["update_id"] + 1
            try:
                self.on_update(u)
            except Exception as e:
                log(f"апдейт {u['update_id']}: {e!r}")

    def save_offset(self):
        # если смена упадёт, следующая не должна отвечать на те же сообщения второй раз
        if self.store.offset != self.saved_offset and time.monotonic() - self.saved_at > 30:
            self.store.save()
            self.saved_offset, self.saved_at = self.store.offset, time.monotonic()

    def finish(self):
        # даём дописать начатые ответы, потом сохраняемся
        self.flush_inbox(force=True)
        deadline = time.monotonic() + 25 * 60
        while any(q.unfinished_tasks for q in self.queues.values()) and time.monotonic() < deadline:
            time.sleep(1)
        self.store.save()
        log("смена закончилась")

    def on_update(self, u):
        if "message" in u:
            self.on_message(u["message"])
        elif "callback_query" in u:
            self.on_button(u["callback_query"])
        elif "stopped_message_generation" in u:
            self.responder.stop(u["stopped_message_generation"]["draft_id"])

    def allowed(self, user, chat):
        if str(user.get("id")) in settings.allowed:
            return True
        self.tg.send(chat, f"🔒 Это личный бот.\nТвой id: <code>{user.get('id')}</code>")
        return False

    def on_message(self, msg):
        chat = msg["chat"]["id"]
        if msg["chat"].get("type") != "private" or not self.allowed(msg.get("from", {}), chat):
            return
        text = msg.get("text") or ""
        if text.startswith("/"):
            return self.command(chat, text.split()[0].split("@")[0].lower(), msg.get("from"))
        if msg.get("voice") or msg.get("video_note") or msg.get("audio"):
            return self.tg.send(chat, "🎙 Голосовые пока не понимаю, напиши текстом",
                                reply_to=msg["message_id"])
        box = self.inbox.setdefault(chat, {"msgs": []})
        box["msgs"].append(msg)
        box["at"] = time.monotonic()

    def flush_inbox(self, force=False):
        # телега режет длинный текст на несколько сообщений и шлёт альбом по одной
        # фотке, поэтому всё, что пришло подряд, считаем одним вопросом
        for chat, box in list(self.inbox.items()):
            if force or time.monotonic() - box["at"] > GATHER:
                del self.inbox[chat]
                self.ask(chat, sorted(box["msgs"], key=lambda m: m["message_id"]))

    def ask(self, chat, msgs):
        entry = self.user_entry(msgs)
        if not entry["text"] and not entry["files"]:
            return self.tg.send(chat, "Такое пока не понимаю 🙂 Пришли текст, фото или файл.",
                                reply_to=msgs[-1]["message_id"])
        entry["msg"] = msgs[-1]["message_id"]
        user = msgs[-1].get("from")
        self.enqueue(chat, entry, lambda: self.responder.answer(chat, entry["msg"], user))

    def user_entry(self, msgs):
        texts, files = [], []
        for m in msgs:
            if m.get("text") or m.get("caption"):
                texts.append(m.get("text") or m.get("caption"))
            if m.get("photo"):
                files.append({"file_id": m["photo"][-1]["file_id"], "mime": "image/jpeg",
                              "name": "photo.jpg"})
            doc = m.get("document")
            if doc:
                texts.append(self.document(doc, files))

        first = msgs[0]
        replied = first.get("quote", {}).get("text")
        if not replied:
            r = first.get("reply_to_message") or {}
            replied = r.get("text") or r.get("caption")
        text = "\n\n".join(t for t in texts if t)
        if replied:
            text = f"> {replied[:1500]}\n\n{text}"
        return {"role": "user", "text": text.strip(), "files": files}

    def document(self, doc, files):
        name = doc.get("file_name", "file")
        mime = doc.get("mime_type", "")
        if doc.get("file_size", 0) > FILE_LIMIT:
            return f"[файл {name} слишком большой, читаю до 15 МБ]"
        if mime.startswith("image/") or mime == "application/pdf":
            files.append({"file_id": doc["file_id"], "mime": mime, "name": name})
            return ""
        if not (mime.startswith("text/") or name.lower().endswith(TEXT_FILES)):
            return f"[файл {name}: такой формат я не читаю]"
        if doc.get("file_size", 0) > TEXT_LIMIT:
            return f"[файл {name} слишком большой, текстовые читаю до 300 КБ]"
        try:
            content = self.tg.download(doc["file_id"]).decode("utf-8", "replace")
        except (TelegramError, OSError) as e:
            return f"[файл {name} не скачался: {e}]"
        return f"Файл {name}:\n```\n{content}\n```"

    def enqueue(self, chat, entry, job):
        # у каждого чата своя очередь. если пока модель отвечала, пришло ещё несколько
        # вопросов, отвечаем на них разом, а не по очереди
        q = self.queues.get(chat)
        if q is None:
            q = self.queues[chat] = queue.Queue()
            threading.Thread(target=self.worker, args=(chat, q), daemon=True).start()
        q.put((entry, job))

    def worker(self, chat, q):
        while True:
            batch = [q.get()]
            while not q.empty():
                batch.append(q.get_nowait())
            try:
                for entry, _ in batch:
                    if entry:
                        self.store.remember(chat, entry)
                batch[-1][1]()
            except Exception as e:
                log(f"ответ в {chat} упал: {e!r}")
                self.tg.send(chat, f"⚠️ Что-то сломалось: <code>{plain(repr(e))[:300]}</code>")
            finally:
                for _ in batch:
                    q.task_done()

    def command(self, chat, cmd, user):
        if cmd in ("/start", "/help"):
            model = models.get(self.store.chat(chat)["model"])
            name = f", {plain(user['first_name'])}" if user and user.get("first_name") else ""
            self.tg.send(chat, HELP.format(model=model.name, name=name))
        elif cmd == "/new":
            self.store.reset(chat)
            self.store.save()
            self.tg.send(chat, "🧹 Начали с чистого листа. О чём поговорим?")
        elif cmd in ("/model", "/mode", "/settings"):
            text, markup = self.menu(chat)
            self.tg.send(chat, text, markup=markup)

    def menu(self, chat):
        conf = self.store.chat(chat)
        model = models.get(conf["model"])
        effort = models.effort_for(model, conf["effort"])
        web = conf.get("web", True)

        abilities = " и ".join(a for a in ("фото" if model.vision else "",
                                           "PDF" if model.pdf else "") if a)
        lines = ["<b>⚙️ Настройки</b>", "",
                 f"🧠 Модель: <b>{model.name}</b>" + (f" · видит {abilities}" if abilities else "")]
        if effort:
            lines.append(f"💭 Режим: <b>{models.EFFORTS[effort]}</b> — {EFFORT_HINTS[effort]}")
        else:
            lines.append("💭 У этой модели нет режимов размышлений")
        lines.append("🌐 Интернет: " + ("<b>включён</b>, ищу сам, когда нужно" if web
                                        else "<b>выключен</b>"))

        rows = grid([{"text": ("✓ " if m.id == model.id else "") + m.name +
                      (" 🖼" if m.vision else ""), "callback_data": f"model:{i}"}
                     for i, m in enumerate(models.MODELS)], 2)
        rows += grid([{"text": ("✓ " if e == effort else "") + models.EFFORTS[e],
                       "callback_data": f"effort:{e}"} for e in model.efforts], 3)
        rows.append([{"text": "🌐 Интернет: вкл" if web else "🌐 Интернет: выкл",
                      "callback_data": "web"},
                     {"text": "🧹 Новый диалог", "callback_data": "new"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def on_button(self, cb):
        chat = cb["message"]["chat"]["id"]
        if not self.allowed(cb.get("from", {}), chat):
            return self.tg.answer_callback(cb["id"])
        data = cb.get("data", "")
        conf = self.store.chat(chat)

        if data.startswith("model:"):
            conf["model"] = models.MODELS[int(data[6:])].id
            self.update_menu(cb, f"Модель: {models.get(conf['model']).name}")
        elif data.startswith("effort:"):
            conf["effort"] = data[7:]
            self.update_menu(cb, f"Режим: {models.EFFORTS[conf['effort']]}")
        elif data == "web":
            conf["web"] = not conf.get("web", True)
            self.update_menu(cb, "Интернет включён" if conf["web"] else "Интернет выключен")
        elif data == "menu":
            self.tg.answer_callback(cb["id"])
            text, markup = self.menu(chat)
            self.tg.send(chat, text, markup=markup)
        elif data == "new":
            self.store.reset(chat)
            self.tg.answer_callback(cb["id"], "Начали новый диалог")
            self.tg.send(chat, "🧹 Начали с чистого листа. О чём поговорим?")
        elif data.startswith("ask:"):
            question = self.store.data.get("buttons", {}).get(data[4:])
            if not question:
                return self.tg.answer_callback(cb["id"], "Кнопка устарела")
            self.tg.answer_callback(cb["id"])
            self.follow_up(chat, cb["message"]["message_id"], question, cb.get("from"))
        elif data == "again":
            self.again(cb)
        self.store.save()

    def update_menu(self, cb, note):
        self.tg.answer_callback(cb["id"], note)
        text, markup = self.menu(cb["message"]["chat"]["id"])
        try:
            self.tg.edit(cb["message"]["chat"]["id"], cb["message"]["message_id"], text, markup)
        except TelegramError as e:
            log(e)

    def follow_up(self, chat, message_id, question, user):
        entry = {"role": "user", "text": question, "files": [], "msg": message_id,
                 "button": True}
        self.enqueue(chat, entry,
                     lambda: self.responder.answer(chat, message_id, user, asked=question))

    def again(self, cb):
        chat = cb["message"]["chat"]["id"]
        history = self.store.chat(chat)["history"]
        last = history[-1] if history else {}
        pressed = cb["message"]["message_id"]
        # переписать можно только последний ответ, иначе история разъедется
        if last.get("role") == "assistant" and pressed not in (last.get("msg"), last.get("last")):
            return self.tg.answer_callback(cb["id"], "Переписать можно только последний ответ")
        self.tg.answer_callback(cb["id"], "Переписываю…")

        def job():
            history = self.store.chat(chat)["history"]
            if history and history[-1]["role"] == "assistant":
                history.pop()
            users = [h for h in history if h["role"] == "user"]
            if not users:
                return self.tg.send(chat, "Нечего переписывать — начни с вопроса 🙂")
            q = users[-1]
            asked = q["text"] if q.get("button") else None
            self.responder.answer(chat, q.get("msg"), cb.get("from"), asked=asked)
        self.enqueue(chat, None, job)


def grid(buttons, width):
    return [buttons[i:i + width] for i in range(0, len(buttons), width)]


def main():
    if not settings.tg_token:
        sys.exit("не задан TG_TOKEN")
    if not settings.api_key:
        sys.exit("не задан OPENCODE_API_KEY")
    Bot(Telegram(settings.tg_token)).run(settings.shift_minutes)


if __name__ == "__main__":
    main()
