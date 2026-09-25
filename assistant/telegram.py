import contextlib
import json
import urllib.error
import urllib.request
import uuid

from . import log

UPDATES = ["message", "callback_query", "stopped_message_generation"]


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}"
        self.files = f"https://api.telegram.org/file/bot{token}"

    def call(self, method, files=None, http_timeout=60, **params):
        params = {k: v for k, v in params.items() if v is not None}
        if files:
            body, ctype = multipart(params, files)
        else:
            body, ctype = json.dumps(params).encode(), "application/json"
        req = urllib.request.Request(f"{self.base}/{method}", body, {"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
                return json.loads(r.read())["result"]
        except urllib.error.HTTPError as e:
            raise TelegramError(f"{method}: {describe(e)}") from None
        except (OSError, ValueError, KeyError) as e:
            raise TelegramError(f"{method}: {e}") from None

    def updates(self, offset, wait):
        return self.call("getUpdates", offset=offset, timeout=wait, allowed_updates=UPDATES,
                         http_timeout=wait + 20)

    def send_rich(self, chat, markdown, reply_to=None, markup=None, images=None):
        # images: [(id, png bytes)], в markdown на них ссылки tg://photo?id=...
        rich = {"markdown": markdown}
        files = None
        if images:
            rich["media"] = [{"id": i, "media": {"type": "photo", "media": f"attach://{i}"}}
                             for i, _ in images]
            files = {i: (f"{i}.png", data, "image/png") for i, data in images}
        return self.call("sendRichMessage", files=files, http_timeout=120, chat_id=chat,
                         rich_message=rich, reply_parameters=reply(reply_to),
                         reply_markup=markup)

    def draft(self, chat, draft_id, markdown):
        return self.call("sendRichMessageDraft", chat_id=chat, draft_id=draft_id,
                         rich_message={"markdown": markdown}, can_stop=True)

    def send(self, chat, text, reply_to=None, markup=None, html=True):
        return self.call("sendMessage", chat_id=chat, text=text,
                         parse_mode="HTML" if html else None,
                         reply_parameters=reply(reply_to), reply_markup=markup,
                         link_preview_options={"is_disabled": True})

    def edit(self, chat, message_id, text, markup=None):
        return self.call("editMessageText", chat_id=chat, message_id=message_id, text=text,
                         parse_mode="HTML", reply_markup=markup)

    def delete(self, chat, message_id):
        try:
            self.call("deleteMessage", chat_id=chat, message_id=message_id)
        except TelegramError as e:
            log(e)

    def answer_callback(self, callback_id, text=None):
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError as e:
            log(e)

    def typing(self, chat):
        with contextlib.suppress(TelegramError):
            self.call("sendChatAction", chat_id=chat, action="typing")

    def download(self, file_id):
        path = self.call("getFile", file_id=file_id)["file_path"]
        with urllib.request.urlopen(f"{self.files}/{path}", timeout=120) as r:
            return r.read()


def reply(message_id):
    if not message_id:
        return None
    return {"message_id": message_id, "allow_sending_without_reply": True}


def multipart(params, files):
    boundary = uuid.uuid4().hex
    out = b""
    for key, value in params.items():
        if not isinstance(value, str):
            value = json.dumps(value)
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n").encode()
    for key, (filename, data, mime) in files.items():
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; '
                f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def describe(e):
    try:
        return json.loads(e.read())["description"]
    except (ValueError, KeyError):
        return str(e)
