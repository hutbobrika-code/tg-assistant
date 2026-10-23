"""Test doubles and environment setup shared by the tests.

Import this module before `bot`: it points the bot at a temporary workspace
and state file so tests never touch the real ones.
"""
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="ocbot-test-"))
os.environ["OC_WORKDIR"] = str(TMP / "workspace")
os.environ.setdefault("TG_TOKEN", "0:test")
os.environ.setdefault("ALLOWED_IDS", "42")

import bot  # noqa: E402

bot.STATE_FILE = TMP / "state.json"
bot.STATE.clear()
bot.WORKDIR.mkdir(parents=True, exist_ok=True)
bot.UPLOADS.mkdir(parents=True, exist_ok=True)

USER = 42


class FakeTelegram:
    """Records every call; messages get increasing ids. Drafts can be made slow."""

    def __init__(self, live_delay=0.0):
        self.calls = []
        self.lock = threading.Lock()
        self.next_id = 1000
        self.live_delay = live_delay  # how long progress-message requests hang
        self.fail = set()  # methods that should return ok=False
        self.edit_error = None  # description returned by editMessageText
        self.ids = []  # (message_id, method, params) of everything that created a message
        self.echo = {}  # extra fields in results of message-creating calls
        self.delays = {}  # method -> seconds the request hangs

    def _record(self, method, params, files=None):
        with self.lock:
            self.calls.append((time.time(), method, params, files))

    def call_index(self, method, pred=lambda p: True):
        """Positions of matching calls in the call log."""
        with self.lock:
            return [i for i, (_, m, p, _) in enumerate(self.calls) if m == method and pred(p)]

    def _result(self, method):
        if method in self.fail:
            return {"ok": False, "description": "Bad Request: forced failure"}
        if method in ("sendRichMessage", "sendMessage", "sendDocument", "sendPhoto"):
            with self.lock:
                self.next_id += 1
                return {"ok": True, "result": {"message_id": self.next_id, **self.echo}}
        return {"ok": True, "result": True}

    def __call__(self, method, _timeout=30, _retries=3, **params):
        self._record(method, params)
        if is_live(params) and self.live_delay:
            time.sleep(self.live_delay)
        if self.delays.get(method):
            time.sleep(self.delays[method])
        if method == "editMessageText" and self.edit_error:
            return {"ok": False, "description": self.edit_error}
        return self._created(method, params, self._result(method))

    def _created(self, method, params, r):
        if r.get("ok") and isinstance(r.get("result"), dict):
            with self.lock:
                self.ids.append((r["result"]["message_id"], method, params))
        return r

    def final_ids(self):
        with self.lock:
            return [i for i, m, p in self.ids if m == "sendRichMessage" and not is_live(p)]

    def live_ids(self):
        with self.lock:
            return [i for i, m, p in self.ids if is_live(p)]

    def upload(self, method, files, **params):
        self._record(method, params, files)
        return self._created(method, params, self._result(method))

    def download(self, file_id, dest):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake " + file_id.encode())
        return dest

    def sent(self, *methods):
        with self.lock:
            return [(m, p, f) for _, m, p, f in self.calls if m in methods]

    def texts(self, *methods):
        methods = methods or ("sendRichMessage",)
        return [p.get("rich_message", {}).get("markdown") or p.get("text", "") for m, p, _ in self.sent(*methods)]

    def finals(self):
        """Answers and bot messages, without the progress message."""
        return [p.get("rich_message", {}).get("markdown", "") for m, p, _ in self.sent("sendRichMessage")
                if not is_live(p)]

    def live(self):
        """Every version of the progress message."""
        return [p["rich_message"]["markdown"] for m, p, _ in self.sent("sendRichMessage", "editMessageText")
                if is_live(p)]

    def reactions(self):
        return [(p["message_id"], [r["emoji"] for r in p["reaction"]]) for _, p, _ in self.sent("setMessageReaction")]


def is_live(params):
    return params.get("reply_markup") == bot.STOP_KB


def use_fake(live_delay=0.0):
    fake = FakeTelegram(live_delay)
    bot.tg = fake
    return fake


def fresh_chat(chat_id=USER, ctype="private"):
    bot.CHATS.pop(chat_id, None)
    bot.STATE.pop(str(chat_id), None)
    return bot.get_chat(chat_id, ctype)


def message(text=None, mid=1, **extra):
    m = {"message_id": mid, "chat": {"id": USER, "type": "private"}, "from": {"id": USER, "username": "tester"}}
    if text is not None:
        m["text"] = text
    m.update(extra)
    return m


# ---------- scripted model turns ----------

import copy  # noqa: E402
import itertools  # noqa: E402


class Reply:
    """Script for one assistant turn. Steps:
    ("text", s) - streamed text; ("tool", name, input) - a tool call;
    ("file", name, bytes) - the model writes a file;
    ("think", wait, reported_seconds) - reasoning; ("sleep", seconds); ("error", message) - the turn fails.
    """

    def __init__(self, *steps, delay=0.01, chunk=8):
        self.steps = steps or (("text", "ок"),)
        self.delay = delay
        self.chunk = chunk


def wait_idle(chat, timeout=10):
    """Wait until the chat has no running or queued jobs."""
    end = time.time() + timeout
    time.sleep(0.05)
    while time.time() < end:
        if not chat.busy() and not bot.reactions_pending():
            time.sleep(0.05)  # let fire-and-forget calls land
            return True
        time.sleep(0.02)
    raise AssertionError("chat did not become idle")


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    raise AssertionError("condition not met")


# ---------- a scripted Claude Code ----------

from collections import OrderedDict  # noqa: E402

class FakeTurn:
    def __init__(self, fake, reply, on_change):
        self.fake, self.reply, self.on_change = fake, reply, on_change or (lambda: None)
        self.parts = OrderedDict()
        self.lock = threading.Lock()
        self.session_id = None
        self.context = 4321
        self.rate_limit = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.24},
                                                                   "seven_day": {"utilization": 0.16}}}
        self.done = threading.Event()
        self.stopped = threading.Event()
        self._error = None
        threading.Thread(target=self._play, daemon=True).start()

    def _add(self, **part):
        part["id"] = f"p{len(self.parts)}"
        with self.lock:
            self.parts[part["id"]] = part
        return part

    def _play(self):
        self.session_id = self.fake.new_sid()
        self.on_change()
        for step in self.reply.steps:
            if self.stopped.is_set():
                break
            kind = step[0]
            if kind == "text":
                p = self._add(type="text", text="", time={"start": 1})
                for i in range(0, len(step[1]), self.reply.chunk):
                    if self.stopped.is_set():
                        break
                    with self.lock:
                        p["text"] += step[1][i:i + self.reply.chunk]
                    self.on_change()
                    time.sleep(self.reply.delay)
                p["time"]["end"] = 2
            elif kind == "tool":
                p = self._add(type="tool", tool=step[1], state={"status": "running", "input": step[2]})
                time.sleep(self.reply.delay)
                p["state"]["status"] = "completed"
            elif kind == "think":
                t0 = time.time() * 1000
                p = self._add(type="reasoning", text="", time={"start": t0})
                self.stopped.wait(step[1])
                p["time"]["end"] = t0 + step[2] * 1000
            elif kind == "file":
                (bot.WORKDIR / step[1]).write_bytes(step[2])
            elif kind == "sleep":
                self.stopped.wait(step[1])
            elif kind == "error":
                self._error = step[1]
                break
            self.on_change()
        self.done.set()
        self.on_change()

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(list(self.parts.values()))

    def stop(self):
        self.fake.stops += 1
        self.stopped.set()

    def error(self):
        return self._error


class FakeClaude:
    def __init__(self, available=True):
        self.is_available = available
        self.replies = []
        self.starts = []
        self.forgotten = []
        self.stops = 0
        self._seq = itertools.count(1)

    def available(self):
        return self.is_available

    def new_sid(self):
        return f"cs-{next(self._seq):03d}"

    def start(self, parts, model, effort=None, resume=None, plan=False, on_change=None):
        self.starts.append({"parts": parts, "model": model, "effort": effort, "resume": resume, "plan": plan})
        return FakeTurn(self, self.replies.pop(0) if self.replies else Reply(), on_change)

    def forget(self, sid):
        self.forgotten.append(sid)


def use_fake_claude(available=True):
    fake = FakeClaude(available)
    bot.claude = fake
    return fake


# ---------- a scripted Gemini router ----------

class FakeGemini:
    def __init__(self, available=True):
        self.is_available = available
        self.replies = []
        self.starts = []
        self.stops = 0
        self._seq = itertools.count(1)

    def available(self):
        return self.is_available

    def new_sid(self):
        return f"gs-{next(self._seq):03d}"

    def start(self, parts, model_id, effort=None, dialog=None, on_change=None):
        self.starts.append({"parts": parts, "model": model_id, "effort": effort, "dialog": dialog})
        return FakeTurn(self, self.replies.pop(0) if self.replies else Reply(), on_change)


def use_fake_gemini(available=True):
    fake = FakeGemini(available)
    bot.gemini = fake
    return fake
