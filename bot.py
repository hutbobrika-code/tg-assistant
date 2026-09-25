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

COMMANDS = [
    {"command": "new", "description": "🧹 Новый диалог"},
    {"command": "model", "description": "🧠 Модель и режим"},
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

HELP = """<b>Привет! Я ассистент на {model}</b>

Пиши вопрос или присылай фото, скриншоты, PDF и текстовые файлы — я их читаю. \
Можно альбомом, можно с подписью.

Отвечаю с таблицами, формулами и схемами. Пока думаю, видно, над чем именно. \
Если долго — жми «стоп».

/model — выбрать модель и режим размышлений
/new — начать новый диалог (я забуду предыдущий)

Под ответом есть кнопки: 💬 продолжить разговор, 🔄 переписать ответ."""


class Bot:
    def __init__(self, tg):
        self.tg = tg
        self.store = Store()
        self.responder = Responder(tg, self.store)
        self.queues = {}
        self.albums = {}  # media_group_id -> сообщения альбома, телега присылает их по одному

    def run(self, minutes):
        try:
            self.tg.call("setMyCommands", commands=COMMANDS)
        except TelegramError as e:
            log(e)
        log(f"смена на {minutes} мин")
        end = time.monotonic() + minutes * 60
        while time.monotonic() < end:
            self.flush_albums()
            self.poll(1 if self.albums else 50)
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

    def finish(self):
        # даём дописать начатые ответы, потом сохраняемся
        deadline = time.monotonic() + 180
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
            return self.command(chat, text.split()[0].split("@")[0].lower())
        if msg.get("voice") or msg.get("video_note") or msg.get("audio"):
            return self.tg.send(chat, "🎙 Голосовые пока не понимаю, напиши текстом",
                                reply_to=msg["message_id"])
        if msg.get("media_group_id"):
            album = self.albums.setdefault(msg["media_group_id"], {"msgs": []})
            album["msgs"].append(msg)
            album["at"] = time.monotonic()
            return
        self.ask(chat, [msg])

    def flush_albums(self):
        for key, album in list(self.albums.items()):
            if time.monotonic() - album["at"] > 1.5:
                del self.albums[key]
                msgs = sorted(album["msgs"], key=lambda m: m["message_id"])
                self.ask(msgs[0]["chat"]["id"], msgs)

    def ask(self, chat, msgs):
        entry = self.user_entry(msgs)
        if not entry["text"] and not entry["files"]:
            return self.tg.send(chat, "Такое пока не понимаю 🙂 Пришли текст, фото или файл.",
                                reply_to=msgs[0]["message_id"])
        entry["msg"] = msgs[0]["message_id"]

        def job():
            self.store.remember(chat, entry)
            self.responder.answer(chat, entry["msg"])
        self.enqueue(chat, job)

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
                name = doc.get("file_name", "file")
                mime = doc.get("mime_type", "")
                if mime.startswith("image/") or mime == "application/pdf":
                    files.append({"file_id": doc["file_id"], "mime": mime, "name": name})
                elif mime.startswith("text/") or name.lower().endswith(TEXT_FILES):
                    texts.append(self.text_file(doc, name))
                else:
                    texts.append(f"[файл {name}: такой формат я не читаю]")

        quote = msgs[0].get("quote", {}).get("text")
        replied = msgs[0].get("reply_to_message") or {}
        replied_text = quote or replied.get("text") or replied.get("caption")
        text = "\n\n".join(texts)
        if replied_text:
            text = f"> {replied_text[:1500]}\n\n{text}"
        return {"role": "user", "text": text.strip(), "files": files}

    def text_file(self, doc, name):
        if doc.get("file_size", 0) > TEXT_LIMIT:
            return f"[файл {name} слишком большой, читаю файлы до 300 КБ]"
        try:
            content = self.tg.download(doc["file_id"]).decode("utf-8", "replace")
        except (TelegramError, OSError) as e:
            return f"[файл {name} не скачался: {e}]"
        return f"Файл {name}:\n```\n{content}\n```"

    def enqueue(self, chat, job):
        # у каждого чата своя очередь: следующее сообщение ждёт, пока допишется ответ
        q = self.queues.get(chat)
        if q is None:
            q = self.queues[chat] = queue.Queue()
            threading.Thread(target=self.worker, args=(chat, q), daemon=True).start()
        q.put(job)

    def worker(self, chat, q):
        while True:
            job = q.get()
            try:
                job()
            except Exception as e:
                log(f"ответ в {chat} упал: {e!r}")
                self.tg.send(chat, f"⚠️ Что-то сломалось: <code>{plain(repr(e))[:300]}</code>")
            finally:
                q.task_done()

    def command(self, chat, cmd):
        if cmd in ("/start", "/help"):
            model = models.get(self.store.chat(chat)["model"])
            self.tg.send(chat, HELP.format(model=model.name))
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

        abilities = ["фото" if model.vision else "", "PDF" if model.pdf else ""]
        abilities = " и ".join(a for a in abilities if a)
        lines = ["<b>⚙️ Настройки</b>", "",
                 f"🧠 Модель: <b>{model.name}</b>" + (f" · видит {abilities}" if abilities else "")]
        if effort:
            lines.append(f"💭 Режим: <b>{models.EFFORTS[effort]}</b> — {EFFORT_HINTS[effort]}")
        else:
            lines.append("💭 У этой модели нет режимов размышлений")

        rows, row = [], []
        for i, m in enumerate(models.MODELS):
            mark = "✓ " if m.id == model.id else ""
            eye = " 🖼" if m.vision else ""
            row.append({"text": f"{mark}{m.name}{eye}", "callback_data": f"model:{i}"})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        row = []
        for e in model.efforts:
            mark = "✓ " if e == effort else ""
            row.append({"text": f"{mark}{models.EFFORTS[e]}", "callback_data": f"effort:{e}"})
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([{"text": "🧹 Новый диалог", "callback_data": "new"}])
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
        elif data == "menu":
            self.tg.answer_callback(cb["id"])
            text, markup = self.menu(chat)
            self.tg.send(chat, text, markup=markup)
        elif data == "new":
            self.store.reset(chat)
            self.tg.answer_callback(cb["id"], "Начали новый диалог")
            self.tg.send(chat, "🧹 Начали с чистого листа. О чём поговорим?")
        elif data.startswith("ask:"):
            question = self.responder.followups.get(data[4:])
            if not question:
                return self.tg.answer_callback(cb["id"], "Кнопка устарела")
            self.tg.answer_callback(cb["id"])
            self.follow_up(chat, cb["message"]["message_id"], question)
        elif data == "again":
            self.tg.answer_callback(cb["id"], "Переписываю…")
            self.enqueue(chat, lambda: self.again(chat))
        self.store.save()

    def update_menu(self, cb, note):
        self.tg.answer_callback(cb["id"], note)
        text, markup = self.menu(cb["message"]["chat"]["id"])
        try:
            self.tg.edit(cb["message"]["chat"]["id"], cb["message"]["message_id"], text, markup)
        except TelegramError as e:
            log(e)

    def follow_up(self, chat, message_id, question):
        def job():
            self.store.remember(chat, {"role": "user", "text": question, "files": [],
                                       "msg": message_id, "button": True})
            self.responder.answer(chat, message_id, asked=question)
        self.enqueue(chat, job)

    def again(self, chat):
        history = self.store.chat(chat)["history"]
        if history and history[-1]["role"] == "assistant":
            history.pop()
        users = [h for h in history if h["role"] == "user"]
        if not users:
            return self.tg.send(chat, "Нечего переписывать — начни с вопроса 🙂")
        last = users[-1]
        asked = last["text"] if last.get("button") else None
        self.responder.answer(chat, last.get("msg"), asked=asked)


def main():
    if not settings.tg_token:
        sys.exit("не задан TG_TOKEN")
    if not settings.api_key:
        sys.exit("не задан OPENCODE_API_KEY")
    Bot(Telegram(settings.tg_token)).run(settings.shift_minutes)


if __name__ == "__main__":
    main()
