import gzip
import json
import os
import subprocess
import threading

from . import log
from .config import settings

HISTORY_LIMIT = 60  # сообщений на чат
HISTORY_CHARS = 200_000


class Store:
    # настройки и история чатов. в репозиторий попадает только зашифрованный файл,
    # репо публичный

    def __init__(self):
        self.path = settings.state_dir / "state.enc"
        self.lock = threading.RLock()
        self.data = self.load()

    def load(self):
        empty = {"chats": {}, "offset": 0}
        if not settings.state_key or not self.path.exists():
            return empty
        try:
            return json.loads(gzip.decompress(openssl(self.path.read_bytes(), decrypt=True)))
        except (OSError, ValueError, subprocess.CalledProcessError) as e:
            log(f"не смог прочитать состояние, начинаю с чистого: {e}")
            return empty

    def save(self):
        if not settings.state_key:
            return
        with self.lock:
            raw = gzip.compress(json.dumps(self.data, ensure_ascii=False).encode())
        encrypted = openssl(raw)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(encrypted)
        tmp.replace(self.path)

    def chat(self, chat_id):
        with self.lock:
            return self.data["chats"].setdefault(str(chat_id), {
                "model": settings.default_model,
                "effort": settings.default_effort,
                "history": [],
            })

    def remember(self, chat_id, entry):
        with self.lock:
            history = self.chat(chat_id)["history"]
            history.append(entry)
            del history[:-HISTORY_LIMIT]
            while len(history) > 2 and sum(len(h.get("text", "")) for h in history) > HISTORY_CHARS:
                del history[0]

    def reset(self, chat_id):
        with self.lock:
            self.chat(chat_id)["history"] = []

    @property
    def offset(self):
        return self.data.get("offset", 0)

    @offset.setter
    def offset(self, value):
        with self.lock:
            self.data["offset"] = value


def openssl(data, decrypt=False):
    cmd = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt",
           "-pass", "env:STATE_KEY"]
    if decrypt:
        cmd.append("-d")
    env = {**os.environ, "STATE_KEY": settings.state_key}
    return subprocess.run(cmd, input=data, capture_output=True, check=True, env=env).stdout
