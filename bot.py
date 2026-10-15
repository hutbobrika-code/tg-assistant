#!/usr/bin/env python3
"""Telegram bot on top of an OpenCode server.

Each message goes to an OpenCode session. While the agent works, the chat
shows a live progress message (status, steps, streamed text) that is edited
in place; the result is posted as a rich message with formulas and tables
rendered by Telegram and the steps folded into a <details> block.
"""
import html
import itertools
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import threading
import time
import traceback
import urllib.request
from collections import OrderedDict
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import urlparse

from cc import ClaudeCode
from gemini import GEMINI_MODELS, GeminiRouter
from oc import OpenCode, OpenCodeError
from render import RICH_LIMIT, md_escape, md_to_html, prepare, split_long
from tg import Telegram, log

BASE = Path(__file__).resolve().parent


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(BASE / ".env")

TOKEN = os.environ.get("TG_TOKEN", "")
ALLOWED = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_IDS") or os.environ.get("ALLOWED_USERS", "")) if x}
OC_URL = os.environ.get("OC_URL", "http://127.0.0.1:4096")
OC_PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD", "")
CLAUDE_BIN = os.path.expanduser(os.environ.get("CLAUDE_BIN", "~/.local/bin/claude"))
CLAUDE_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
DEFAULT_MODEL = os.environ.get("OC_MODEL", "opencode/muse-spark-1.3-contributor-free")
DEFAULT_EFFORT = os.environ.get("OC_EFFORT", "xhigh")
GEMINI_URL = os.environ.get("GEMINI_URL", "http://127.0.0.1:8083/v1")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "sk-gemini-internal")
WORKDIR = Path(os.path.expanduser(os.environ.get("OC_WORKDIR", str(BASE / "workspace")))).resolve()
UPLOADS = WORKDIR / "uploads"
TIMEOUT = int(os.environ.get("OC_TIMEOUT", "1800"))
STATE_FILE = BASE / "state.json"

tg = Telegram(TOKEN)
oc = OpenCode(OC_URL, OC_PASSWORD, WORKDIR)
claude = ClaudeCode(CLAUDE_BIN, WORKDIR, CLAUDE_TOKEN)
gemini = GeminiRouter(GEMINI_URL, GEMINI_API_KEY, WORKDIR)

EFFORTS = {  # label, hint
    "minimal": ("Минимум", "мгновенно"),
    "low": ("Низкий", "быстро, для простого"),
    "medium": ("Средний", "баланс скорости и качества"),
    "high": ("Высокий", "вдумчиво, для сложного"),
    "xhigh": ("Очень высокий", "глубоко: код и анализ"),
    "max": ("Максимум", "предельно тщательно, дольше всего"),
}
CLAUDE_ICON, OPENCODE_ICON, GEMINI_ICON = "✴️", "🔹", "♊"
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CLAUDE_MODELS = [
    {"id": "claude/claude-opus-5-5", "name": "Claude Opus 5.5", "short": "Opus 5.5",
     "variants": CLAUDE_EFFORTS, "context": 1_000_000},
    {"id": "claude/claude-opus-5", "name": "Claude Opus 5", "short": "Opus 5",
     "variants": CLAUDE_EFFORTS, "context": 1_000_000},
    {"id": "claude/claude-fable-5-1", "name": "Claude Fable 5.1", "short": "Fable 5.1",
     "variants": CLAUDE_EFFORTS, "context": 1_000_000},
    {"id": "claude/claude-sonnet-5", "name": "Claude Sonnet 5", "short": "Sonnet 5",
     "variants": CLAUDE_EFFORTS, "context": 1_000_000},
    {"id": "claude/claude-haiku-4-5", "name": "Claude Haiku 4.5", "short": "Haiku 4.5",
     "variants": [], "context": 200_000},
]
DEFAULT_CLAUDE_MODEL = CLAUDE_MODELS[0]["id"]
DEFAULT_GEMINI_MODEL = GEMINI_MODELS[0]["id"]
CLAUDE_LIMITS = {}  # latest rate_limit_info from Claude Code

# Telegram's AIActions set (made for AI bots). Custom emoji only render when the bot
# owner has Telegram Premium, so they are switched on by `is_premium` of the owner.
PREMIUM_ICONS = {
    "💭": "5535034915403333642", "🧠": "5535457114983497745", "🌐": "5535365052359507996",
    "🔎": "5535248817659576336", "📖": "5535039193190760468", "📝": "5537203062138994712",
    "✏️": "5535287489545109527", "💻": "5535251334510411788", "🤖": "5537515087218081814",
    "⚙️": "5537515087218081814", "🔧": "5537560399123054610", "✨": "5573389291184193551",
    "🖼": "5537651753077440526", "✍️": "5573451671289200650",
}
PREMIUM_RE = re.compile("|".join(re.escape(k) for k in sorted(PREMIUM_ICONS, key=len, reverse=True)))
TG_EMOJI_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>')
PREMIUM = {"on": False, "broken": 0}  # broken: when Telegram last stripped them (retried after PREMIUM_RETRY)
PREMIUM_RETRY = 6 * 3600
SKIP_DIRS = {"uploads", "node_modules", "__pycache__", "venv", ".venv", ".git"}
THINK_SHOWN = 4  # reasoning shorter than this (seconds) is not listed as a step
WALK_LIMIT = 20000
TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".tsv", ".yaml", ".yml", ".toml", ".ini",
            ".html", ".css", ".xml", ".sql", ".sh", ".c", ".h", ".cpp", ".hpp", ".java", ".kt", ".go",
            ".rs", ".rb", ".php", ".swift", ".cs", ".lua", ".log", ".tex", ".jsx", ".tsx", ".vue"}
MEDIA_KEYS = ("photo", "document", "voice", "audio", "video", "video_note", "animation")
BURST_WINDOW = 2.5  # склейка фото/файлов без media_group_id, пришедших пачкой, в один запрос
CAPTION_WAIT = 90  # фото без текста ждут подпись с заданием, а не уходят в модель сразу

EXAMPLES = [
    ("🧮 Формула", "Выведи формулу корней квадратного уравнения и коротко объясни каждый шаг"),
    ("📊 Таблица", "Сравни Python, Go и Rust таблицей: скорость, порог входа, где применяются"),
    ("📈 График", "Построй графики sin(x) и cos(x) на отрезке от −2π до 2π и пришли картинку"),
]


# ---------- small helpers ----------

def plural(n, one, few, many):
    n = abs(n) % 100
    if 10 < n < 20:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


def fmt_dur(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec} с"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m} мин {s} с" if s else f"{m} мин"
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин"


def fmt_num(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + " млн"
    if n >= 1000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + " тыс."
    return str(n)


def fmt_size(n):
    for unit in ("Б", "КБ", "МБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f}".rstrip("0").rstrip(".") + f" {unit}"
        n /= 1024
    return f"{n:.1f}".rstrip("0").rstrip(".") + " ГБ"


def ago(ts):
    d = time.time() - ts
    if d < 60:
        return "только что"
    if d < 3600:
        return f"{int(d // 60)} мин назад"
    if d < 86400:
        return f"{int(d // 3600)} ч назад"
    if d < 172800:
        return "вчера"
    return time.strftime("%d.%m", time.localtime(ts))


def short(s, n=90):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def rel(p):
    try:
        return str(Path(p).resolve().relative_to(WORKDIR)) or "."
    except (ValueError, OSError):
        return str(p)


def code(s, n=70):
    return "`" + short(s, n).replace("`", "ˋ") + "`"


def btn(text, data, style=None):
    b = {"text": text, "callback_data": data}
    if style:
        b["style"] = style
    return b


def tg_async(method, **params):
    """Fire-and-forget call for cosmetic updates: Telegram sometimes holds a request for ~30 s."""
    threading.Thread(target=tg, args=(method,), kwargs=dict(params, _retries=1), daemon=True).start()


class Reactions:
    """Sets reactions from a background thread, in order; only the latest wish per message is sent."""

    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.want = OrderedDict()
        self.busy = False
        self.cv = threading.Condition()
        threading.Thread(target=self.loop, daemon=True).start()

    def set(self, msg_id, emoji):
        with self.cv:
            self.want[msg_id] = emoji
            self.want.move_to_end(msg_id)
            self.cv.notify()

    def pending(self):
        with self.cv:
            return bool(self.want) or self.busy

    def loop(self):
        while True:
            with self.cv:
                self.cv.wait_for(lambda: self.want)
                msg_id, emoji = self.want.popitem(last=False)
                self.busy = True
            try:
                tg("setMessageReaction", _timeout=40, _retries=1, chat_id=self.chat_id, message_id=msg_id,
                   reaction=[{"type": "emoji", "emoji": emoji}] if emoji else [])
            finally:
                with self.cv:
                    self.busy = False


REACTIONS = {}
REACTIONS_LOCK = threading.Lock()


def react(chat_id, msg_id, emoji=None):
    if not msg_id:
        return
    with REACTIONS_LOCK:
        r = REACTIONS.get(chat_id) or REACTIONS.setdefault(chat_id, Reactions(chat_id))
    r.set(msg_id, emoji)


def reactions_pending():
    with REACTIONS_LOCK:
        return any(r.pending() for r in REACTIONS.values())


def walk_files(skip=SKIP_DIRS):
    """(mtime, size, path) for files in the workspace, skipping hidden and bulky dirs."""
    seen = 0
    for root, dirs, names in os.walk(WORKDIR):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip]
        for n in names:
            seen += 1
            if seen > WALK_LIMIT:
                return
            if n.startswith("."):
                continue
            p = Path(root) / n
            try:
                s = p.stat()
            except OSError:
                continue
            yield s.st_mtime, s.st_size, p


# ---------- persistent state ----------

state_lock = threading.Lock()
try:
    STATE = json.loads(STATE_FILE.read_text())
except (OSError, ValueError):
    STATE = {}


def chat_state(chat_id):
    with state_lock:
        st = STATE.setdefault(str(chat_id), {})
        st.setdefault("model", DEFAULT_MODEL)
        st.setdefault("effort", DEFAULT_EFFORT)
        st.setdefault("agent", "build")
        st.setdefault("session", None)
        return st


def save_state():
    with state_lock:
        data = json.dumps(STATE, indent=1, ensure_ascii=False)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(data)
        tmp.replace(STATE_FILE)


_models = {"at": 0, "list": []}


def models():
    """Claude models first, then Gemini (if connected), then free OpenCode ones."""
    if time.time() - _models["at"] > 600 or not _models["list"]:
        try:
            _models["list"] = oc.models()
            _models["at"] = time.time()
        except OpenCodeError as e:
            log("models:", e)
    gemini_list = GEMINI_MODELS if gemini.available() else []
    return (CLAUDE_MODELS if claude.available() else []) + gemini_list + _models["list"]


def model_info(model_id):
    return next((m for m in models() + CLAUDE_MODELS + GEMINI_MODELS if m["id"] == model_id),
                {"id": model_id, "name": model_id.split("/")[-1], "variants": [], "context": None})


def engine_of(model_id):
    if model_id.startswith("claude/"):
        return "claude"
    if model_id.startswith("gemini/"):
        return "gemini"
    return "opencode"


def engine_icon(model_id):
    eng = engine_of(model_id)
    if eng == "claude":
        return CLAUDE_ICON
    if eng == "gemini":
        return GEMINI_ICON
    return OPENCODE_ICON


def short_name(m):
    return m.get("short") or re.sub(r"\s+Free$", "", m["name"])


def effort_label(v):
    return EFFORTS.get(v, (v, ""))[0]


def meter(m, v):
    """▰▰▰▱▱ - how deep this level is among the model's levels."""
    levels = m["variants"]
    filled = max(1, round((levels.index(v) + 1) / len(levels) * 5)) if v in levels else 0
    return "▰" * filled + "▱" * (5 - filled)


def effort_for(st, model_id=None):
    """Reasoning level for a model: the one picked for it, else xhigh, else the deepest it has."""
    model_id = model_id or st["model"]
    variants = model_info(model_id)["variants"]
    if not variants:
        return None
    for v in ((st.get("efforts") or {}).get(model_id), st.get("effort")):
        if v in variants:
            return v
    return "xhigh" if "xhigh" in variants else variants[-1]


def premiumize(md):
    """Swap plain icons in bot-authored text for animated custom emoji when they can render."""
    if not PREMIUM["on"] or time.time() - PREMIUM["broken"] < PREMIUM_RETRY:
        return md
    return PREMIUM_RE.sub(lambda m: f'<tg-emoji emoji-id="{PREMIUM_ICONS[m.group(0)]}">{m.group(0)}</tg-emoji>', md)


def plain_icons(md):
    return TG_EMOJI_RE.sub(r"\1", md)


def premium_kept(result):
    """False if Telegram stripped our custom emoji (then they show as the pack's generic smiley)."""
    return "custom_emoji" in json.dumps(result or {})


FILE_TOKENS = OrderedDict()
_file_seq = itertools.count(1)


def file_token(path):
    key = str(path)
    for k, v in FILE_TOKENS.items():
        if v == key:
            return k
    k = str(next(_file_seq))
    FILE_TOKENS[k] = key
    while len(FILE_TOKENS) > 500:
        FILE_TOKENS.popitem(last=False)
    return k


# ---------- describing agent steps ----------

def tool_info(part):
    """(icon, text, literal) for a tool part."""
    name = part.get("tool", "")
    st = part.get("state") or {}
    inp = st.get("input") or {}
    path = inp.get("filePath") or inp.get("path") or ""
    if name == "bash":
        if inp.get("description"):
            return "💻", inp["description"], None
        return "💻", "Выполняю", inp.get("command") or "команду"
    if name == "read":
        return "📖", "Читаю", rel(path)
    if name == "write":
        return "📝", "Создаю", rel(path)
    if name in ("edit", "multiedit", "patch", "apply_patch"):
        return "✏️", "Правлю", rel(path) if path else "файлы"
    if name == "glob":
        return "🔎", "Ищу файлы", inp.get("pattern", "")
    if name == "grep":
        return "🔎", "Ищу в коде", inp.get("pattern", "")
    if name == "list":
        return "📂", "Смотрю папку", rel(path) if path else "."
    if name == "webfetch":
        return "🌐", "Открываю", urlparse(inp.get("url", "")).netloc or inp.get("url", "")
    if name == "websearch":
        return "🌐", f"Ищу в интернете «{short(inp.get('query', ''), 60)}»", None
    if name == "todowrite":
        return "📋", "Обновляю план", None
    if name == "todoread":
        return "📋", "Сверяюсь с планом", None
    if name == "task":
        return "🤖", "Подзадача: " + short(inp.get("description", ""), 60), None
    if name == "question":
        return "❓", "Задаю вопрос", None
    if name == "skill":
        return "🧩", "Навык", inp.get("name", "")
    return "🔧", name, None


def tool_md(part):
    icon, text, lit = tool_info(part)
    s = f"{icon} {md_escape(short(text, 80))}".rstrip()
    if lit:
        s += " " + code(lit)
    if (part.get("state") or {}).get("status") == "error":
        s += " — ❌ ошибка"
    return s


def plan_md(todos):
    lines = []
    for t in todos:
        c = md_escape(short(t.get("content", ""), 110))
        s = t.get("status")
        if s == "completed":
            lines.append(f"- [x] {c}")
        elif s == "cancelled":
            lines.append(f"- [x] ~~{c}~~")
        elif s == "in_progress":
            lines.append(f"- [ ] **{c}** ⏳")
        else:
            lines.append(f"- [ ] {c}")
    return "\n".join(lines)


def step_md(step):
    kind, val = step
    if kind == "tool":
        return "- " + tool_md(val)
    if kind == "think":
        return f"- 🧠 Думал {fmt_dur(val)}"
    return "- 💭 _" + md_escape(short(val, 180)) + "_"


def step_html(step):
    """One line of the live steps quote (inside an HTML block, so no markdown)."""
    kind, val = step
    if kind == "think":
        return f"🧠 Думал {fmt_dur(val)}"
    if kind == "narr":
        return "💭 <i>" + html.escape(short(val, 140)) + "</i>"
    icon, text, lit = tool_info(val)
    s = f"{icon} {html.escape(short(text, 70))}" if text else icon
    if lit:
        s += " <code>" + html.escape(short(lit, 60)) + "</code>"
    status = (val.get("state") or {}).get("status")
    if status == "error":
        s += " ❌"
    elif status in ("pending", "running"):
        s += " …"
    return s


def steps_quote(steps, limit=6):
    """Latest steps first, in an expandable quote."""
    lines = [step_html(s) for s in reversed(steps[-limit:])]
    if len(steps) > limit:
        lines.append(f"… и ещё {len(steps) - limit}")
    return "<blockquote expandable>" + "<br>".join(lines) + "</blockquote>"


def reasoning_seconds(part):
    t = part.get("time") or {}
    return (t["end"] - t["start"]) / 1000 if t.get("start") and t.get("end") else 0


def analyze(parts):
    """Split assistant parts into steps (tools + narration before them), the trailing answer and the plan."""
    steps, answer, todos = [], [], None
    for p in parts:
        t = p.get("type")
        if t == "reasoning" and reasoning_seconds(p) >= THINK_SHOWN:
            steps.append(("think", reasoning_seconds(p)))
        elif t == "tool":
            steps += [("narr", x) for x in answer]
            answer = []
            if p.get("tool") == "todowrite":
                todos = ((p.get("state") or {}).get("input") or {}).get("todos") or todos
            steps.append(("tool", p))
        elif t == "text" and not p.get("synthetic") and not p.get("ignored"):
            if (p.get("text") or "").strip():
                answer.append(p["text"])
    return steps, "\n\n".join(answer).strip(), todos


def activity_md(parts):
    """Bold status line for what the agent is doing right now."""
    last = parts[-1] if parts else None
    if last and last["type"] == "tool" and (last.get("state") or {}).get("status") in ("pending", "running"):
        icon, text, lit = tool_info(last)
        s = f"{icon} **{md_escape(short(text, 60))}**" if text else icon
        return s + (" " + code(lit, 50) if lit else "")
    if last and last["type"] == "reasoning" and not (last.get("time") or {}).get("end"):
        return "💭 **Размышляю**"
    if last and last["type"] == "text" and not (last.get("time") or {}).get("end"):
        return "✍️ **Пишу ответ**"
    return "💭 **Думаю**"


def steps_block(steps, todos, title, open_=False, limit=80):
    lines = []
    if todos:
        lines += ["**📋 План**", "", plan_md(todos), ""]
    shown = steps[-limit:]
    if len(steps) > limit:
        lines.append(f"- … и ещё {len(steps) - limit}")
    lines += [step_md(s) for s in shown]
    return f"<details{' open' if open_ else ''}><summary>{title}</summary>\n\n" + "\n".join(lines) + "\n\n</details>"


def steps_title(n, seconds):
    return f"⚙️ {n} {plural(n, 'шаг', 'шага', 'шагов')} · {fmt_dur(seconds)}"


def error_text(err):
    if not err:
        return None
    if isinstance(err, str):
        return err
    data = err.get("data") or {}
    return data.get("message") or err.get("message") or err.get("name") or "неизвестная ошибка"


# ---------- sending rich messages ----------

def input_media(media):
    return [{"id": mid, "media": {"type": kind, "media": f"attach://{mid}"}} for mid, kind, _ in media]


def send_rich(chat, md, media=(), reply_to=None, markup=None, plain=None):
    """Send one rich message; retries media as documents, then falls back to HTML text."""
    params = dict(chat_id=chat.id, reply_markup=markup,
                  reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None)
    rm = {"markdown": md.strip() or "…"}
    if media:
        rm["media"] = input_media(media)
        r = tg.upload("sendRichMessage", {mid: p for mid, _, p in media}, rich_message=rm, **params)
        if not r.get("ok") and any(k != "document" for _, k, _ in media):
            log("sendRichMessage with media failed:", r.get("description"), "- retrying as documents")
            media = [(mid, "document", p) for mid, _, p in media]
            md = re.sub(r"tg://(photo|video|audio)\?id=", "tg://document?id=", md)
            rm = {"markdown": md.strip(), "media": input_media(media)}
            r = tg.upload("sendRichMessage", {mid: p for mid, _, p in media}, rich_message=rm, **params)
    else:
        r = tg("sendRichMessage", rich_message=rm, **params)
    if r.get("ok"):
        mid = r["result"]["message_id"]
        chat.last_seen = max(chat.last_seen, mid)
        if not check_premium(md, r["result"]) and not media:
            tg("editMessageText", chat_id=chat.id, message_id=mid, reply_markup=markup,
               rich_message=dict(rm, markdown=plain_icons(rm["markdown"])))
        return mid
    log("sendRichMessage failed:", r.get("description"))
    return send_fallback(chat, plain if plain is not None else md, media, reply_to, markup)


def send_fallback(chat, text, media, reply_to, markup):
    mid = None
    chunks = split_long(text or "…", 3800)
    for i, chunk in enumerate(chunks):
        params = dict(chat_id=chat.id, link_preview_options={"is_disabled": True},
                      reply_markup=markup if i == len(chunks) - 1 and not media else None,
                      reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True}
                      if reply_to and i == 0 else None)
        r = tg("sendMessage", text=md_to_html(chunk), parse_mode="HTML", **params)
        if not r.get("ok"):
            r = tg("sendMessage", text=chunk, **params)
        if r.get("ok"):
            mid = r["result"]["message_id"]
        else:
            log("sendMessage failed:", r.get("description"))
    for j, (_, kind, path) in enumerate(media):
        if kind == "photo":
            method, field = "sendPhoto", "photo"
        elif kind == "video":
            method, field = "sendVideo", "video"
        elif kind == "audio":
            method, field = "sendAudio", "audio"
        else:
            method, field = "sendDocument", "document"
        r = tg.upload(method, {field: path}, chat_id=chat.id,
                      reply_markup=markup if j == len(media) - 1 else None)
        if r.get("ok"):
            mid = r["result"]["message_id"]
    if mid:
        chat.last_seen = max(chat.last_seen, mid)
    return mid


def send_md(chat, md, markup=None, reply_to=None):
    """Bot-authored rich text (already escaped)."""
    return send_rich(chat, premiumize(md), reply_to=reply_to, markup=markup, plain=md)


def check_premium(md, result):
    """After sending custom emoji: if Telegram stripped them, stop using them. Returns False then."""
    if "<tg-emoji" not in md or premium_kept(result):
        return True
    log("custom emoji are not available to this bot; plain emoji for the next 6 h")
    PREMIUM["broken"] = time.time()
    return False


def edit_md(chat, msg_id, md, markup=None):
    md = premiumize(md)
    r = tg("editMessageText", chat_id=chat.id, message_id=msg_id, rich_message={"markdown": md},
           reply_markup=markup or {"inline_keyboard": []})
    if r.get("ok") and not check_premium(md, r.get("result")):
        r = tg("editMessageText", chat_id=chat.id, message_id=msg_id, rich_message={"markdown": plain_icons(md)},
               reply_markup=markup or {"inline_keyboard": []})
    if not r.get("ok") and "not modified" not in r.get("description", ""):
        log("editMessageText:", r.get("description"))
    return r.get("ok")


# ---------- a single request to the agent ----------

RUNS = {}
RUNS_LOCK = threading.Lock()


SPINNER = ("◐", "◓", "◑", "◒")
STOP_KB = {"inline_keyboard": [[{"text": "⏹ Остановить", "callback_data": "stop"}]]}


class LiveMessage:
    """Progress message that is edited while the agent works and removed once the answer is posted.

    Telegram drafts (sendRichMessageDraft) were tried first, but Telegram sometimes
    holds a draft request for 30-45 s and then shows it after the final answer as a
    stale duplicate. An ordinary message can always be deleted.
    """
    MIN_GAP = 1.5

    def __init__(self, run):
        self.run = run
        self.msg_id = None
        self.cv = threading.Condition()
        self.dirty = True
        self.closed = False
        self.frame = 0
        self.last_md = None
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def poke(self):
        with self.cv:
            self.dirty = True
            self.cv.notify()

    def loop(self):
        last = 0.0
        while True:
            with self.cv:
                # tick every 2.5 s even without events so the spinner and timer move
                self.cv.wait_for(lambda: self.dirty or self.closed, timeout=2.5)
                if self.closed:
                    return
                self.dirty = False
                pause = self.MIN_GAP - (time.time() - last)
                if pause > 0 and self.cv.wait_for(lambda: self.closed, timeout=pause):
                    return
            last = time.time()
            self.frame += 1
            md = self.run.live_md(self.frame)
            if md != self.last_md:
                self.push(md)

    def push(self, md):
        chat_id = self.run.chat.id
        shown = premiumize(md)
        if self.msg_id is None:
            r = tg("sendRichMessage", _retries=1, chat_id=chat_id, rich_message={"markdown": shown},
                   reply_markup=STOP_KB, disable_notification=True)
            if r.get("ok"):
                self.msg_id = r["result"]["message_id"]
                check_premium(shown, r["result"])
                if self.closed:  # the answer went out while this request was in flight
                    self.delete()
        else:
            r = tg("editMessageText", _retries=1, chat_id=chat_id, message_id=self.msg_id,
                   rich_message={"markdown": shown}, reply_markup=STOP_KB)
        desc = r.get("description", "")
        if r.get("ok") or "not modified" in desc:
            self.last_md = md
        elif "not found" in desc:
            self.msg_id = None
            self.closed = True  # the user deleted it
        else:
            log("live message:", desc)

    def close(self):
        """Stop updating; the message stays until delete(). Never waits on Telegram."""
        with self.cv:
            self.closed = True
            self.cv.notify()

    def delete(self):
        mid, self.msg_id = self.msg_id, None
        if mid:
            tg_async("deleteMessage", chat_id=self.run.chat.id, message_id=mid)


class Run:
    """One request to an agent. OpenCodeRun and ClaudeRun supply the backend."""
    engine = ""

    def __init__(self, chat, job):
        self.chat, self.job = chat, job
        self.model, self.effort = job["model"], job.get("effort")
        self.started = time.time()
        self.done = threading.Event()
        self.stopped_at = None
        self.timed_out = False
        self.error = None
        self.retry = None
        self.live = None

    @property
    def stopped(self):
        return self.stopped_at is not None

    def waiting_user(self):
        return self.chat.question is not None or self.chat.permission is not None

    def changed(self):
        if self.live:
            self.live.poke()

    # backend hooks
    def snapshot(self):
        return []

    def start(self):
        pass

    def close(self):
        pass

    def stop(self):
        if not self.stopped:
            self.stopped_at = time.time()
        self.done.set()

    def finished(self, now):
        """Backend-side check that the run is over (in case an event was missed)."""
        return False

    def settle(self):
        """After a stop: give the backend a moment to save the partial answer."""

    def collect(self):
        """(parts, error) of the finished run; updates the dialog state."""
        return self.snapshot(), self.error

    def regen_info(self):
        return None

    def badge(self):
        m = model_info(self.model)
        if "deep-research" in self.model:
            return f"🔬 Deep Research · 4 Argon"
        if "video" in self.model:
            return f"🎬 Veo 2 Video"
        if "music" in self.model:
            return f"🎵 Lyria 3.5 Music"
        if "image" in self.model:
            return f"🎨 Gemini Image"
        s = f"{engine_icon(self.model)} {short_name(m)}"
        return s + (f" · {effort_label(self.effort).lower()}" if self.effort else "")

    def live_md(self, frame=0):
        parts = self.snapshot()
        steps, answer, todos = analyze(parts)
        n = sum(1 for s in steps if s[0] == "tool")
        if self.stopped:
            status = "✋ **Останавливаю**"
        elif self.waiting_user():
            status = "❓ **Жду вашего ответа**"
        elif self.retry:
            status = f"🔁 **Модель занята, повтор #{self.retry.get('attempt', 1)}**"
        else:
            status = activity_md(parts)
        head = f"{SPINNER[frame % len(SPINNER)]} {status} · {fmt_dur(time.time() - self.started)}"
        if n:
            head += f" · {n} {plural(n, 'шаг', 'шага', 'шагов')}"
        out = [head]
        if steps:
            out.append(steps_quote(steps))
        if todos:
            out.append(plan_md(todos))
        if answer:
            if len(answer) > 20000:
                answer = answer[-20000:]
                answer = answer[answer.find("\n\n") + 2:] if "\n\n" in answer else answer
            out.append(prepare(answer, WORKDIR, upload=False)[0])
        out.append(f"<footer>{self.badge()}</footer>")
        return "\n\n".join(out)[:RICH_LIMIT]

    def wait(self):
        last_typing = 0.0
        while not self.done.wait(0.5):
            now = time.time()
            if now - self.started > TIMEOUT and not self.timed_out:
                self.timed_out = True
                self.stop()
                break
            if not self.waiting_user() and now - last_typing >= 5:
                last_typing = now
                threading.Thread(target=tg, args=("sendChatAction",), daemon=True,
                                 kwargs=dict(_timeout=10, _retries=1, chat_id=self.chat.id, action="typing")).start()
            if self.finished(now):
                break
        if self.stopped:
            self.settle()

    def finish(self):
        """Post the final answer. Must never raise: the chat has to be left in a clean state."""
        finished = time.time()
        chat = self.chat
        try:
            self._finish(finished)
        except Exception:
            log(traceback.format_exc())
            send_md(chat, "⚠️ Не удалось оформить ответ — ошибка бота. Попробуйте ещё раз.")
        finally:
            if self.live:
                self.live.delete()  # only after the answer is posted, so the chat never looks empty
            react(chat.id, self.job.get("reply_to"))
            log(f"-> answered #{self.job.get('reply_to')} in {time.time() - self.started:.1f} s "
                f"({self.model}{'/' + self.effort if self.effort else ''}{', stopped' if self.stopped else ''})")

    def _finish(self, finished):
        chat = self.chat
        for kind in ("question", "permission"):
            pending = getattr(chat, kind)
            if pending:
                setattr(chat, kind, None)
                if pending.get("msg"):
                    edit_md(chat, pending["msg"], "❓ *Вопрос снят — задача завершилась*" if kind == "question"
                            else "🔐 *Запрос снят — задача завершилась*")

        parts, err = self.collect()
        steps, answer, todos = analyze(parts)
        if not answer:
            narr = [v for k, v in steps if k == "narr"]
            answer = narr[-1] if narr else ""
        n = sum(1 for s in steps if s[0] == "tool")
        header = premiumize(steps_block(steps, todos, steps_title(n, finished - self.started))) if n else ""
        footer = ""
        if self.timed_out:
            footer = f"⏱ *Остановлено: задача шла дольше {fmt_dur(TIMEOUT)}*"
        elif self.stopped:
            footer = "✋ *Остановлено*"
        elif err:
            footer = "⚠️ " + md_escape(short(error_text(err), 600))
        if not answer and not footer:
            answer = "✅ Готово." if n else "🤷 Модель ничего не ответила. Попробуйте переформулировать."
        meta = self.badge()
        thought = sum(reasoning_seconds(p) for p in parts if p.get("type") == "reasoning")
        if not n and thought >= 3:
            # with tools the steps block already shows timing
            meta += f" · 🧠 Думал {fmt_dur(thought)}"
        footer = (footer + "\n\n" if footer else "") + premiumize(f"<footer>{meta}</footer>")

        chunks = split_long(answer) if answer else [""]
        prepared = [prepare(c, WORKDIR, upload=True) for c in chunks]
        embedded = {p for _, media in prepared for _, _, p in media}
        files = [f for f in changed_files(self.started - 1) if f not in embedded][:6]
        markup = answer_keyboard(files)
        if isinstance(err, str) and err.startswith(("🔑", "⏳")):
            markup["inline_keyboard"].append([btn(f"{OPENCODE_ICON} Перейти на бесплатную модель", "fallback")])

        reply_to = self.job.get("reply_to") if chat.last_seen != self.job.get("reply_to") else None
        msg_id = None
        for i, (md, media) in enumerate(prepared):
            last = i == len(prepared) - 1
            if i == 0 and header:
                md = header + "\n\n" + md
            if last and footer:
                md = (md + "\n\n" + footer).strip()
            plain = plain_icons(chunks[i] + ("\n\n" + re.sub(r"</?footer>", "", footer) if last and footer else "")).strip()
            msg_id = send_rich(chat, md, media, reply_to if i == 0 else None,
                               markup if last else None, plain=plain) or msg_id

        chat.set_last_answer(msg_id, markup)
        info = self.regen_info()
        chat.last_regen = dict(info, engine=self.engine, model=self.model, parts=self.job["parts"],
                               reply_to=self.job.get("reply_to")) if info else None


class OpenCodeRun(Run):
    engine = "opencode"

    def __init__(self, chat, job, sid):
        super().__init__(chat, job)
        self.sid = sid
        self.parts = OrderedDict()
        self.roles = {}
        self.lock = threading.Lock()
        self.busy_seen = False
        self.user_mid = None
        self._last_check = time.time()
        self._idle_checks = 0

    # events from the OpenCode server (SSE thread)
    def on_event(self, ev):
        t, p = ev.get("type"), ev.get("properties") or {}
        changed = False
        with self.lock:
            if t == "message.updated":
                info = p.get("info") or {}
                self.roles[info.get("id")] = info.get("role")
            elif t == "message.part.updated":
                part = p.get("part") or {}
                # the stream also carries the parts of our own prompt
                if part.get("type") in ("text", "reasoning", "tool") and \
                        self.roles.get(part.get("messageID")) == "assistant":
                    self.parts[part["id"]] = part
                    changed = True
            elif t == "message.part.delta":
                part = self.parts.get(p.get("partID"))
                if part is not None and p.get("field") == "text":
                    part["text"] = (part.get("text") or "") + (p.get("delta") or "")
                    changed = True
            elif t == "session.status":
                st = p.get("status") or {}
                if st.get("type") in ("busy", "retry"):
                    self.busy_seen = True
                self.retry = st if st.get("type") == "retry" else None
                changed = True
                if st.get("type") == "idle" and self.busy_seen:
                    self.done.set()
            elif t == "session.idle" and self.busy_seen:
                self.done.set()
            elif t == "session.error":
                err = p.get("error") or {}
                if err.get("name") != "MessageAbortedError":
                    self.error = err
        if changed:
            self.changed()
        if t == "question.asked":
            threading.Thread(target=ask_question, args=(self.chat, p), daemon=True).start()
        elif t == "permission.asked":
            threading.Thread(target=ask_permission, args=(self.chat, p), daemon=True).start()

    def snapshot(self):
        with self.lock:
            return [dict(p) for p in self.parts.values()]

    def start(self):
        with RUNS_LOCK:
            RUNS[self.sid] = self
        oc.prompt(self.sid, self.job["parts"], self.model, self.effort, self.chat.st.get("agent"))

    def close(self):
        with RUNS_LOCK:
            RUNS.pop(self.sid, None)

    def stop(self):
        super().stop()
        oc.abort(self.sid)

    def finished(self, now):
        # watchdog in case an idle event was missed
        if now - self._last_check < 4:
            return False
        self._last_check = now
        try:
            busy = self.sid in oc.status()
        except OpenCodeError:
            busy = True
        if busy:
            self.busy_seen = True
            self._idle_checks = 0
        elif self.busy_seen or now - self.started > 8:
            self._idle_checks += 1
        return self._idle_checks >= 2

    def settle(self):
        for _ in range(20):
            try:
                if self.sid not in oc.status():
                    break
            except OpenCodeError:
                break
            time.sleep(0.5)

    def collect(self):
        parts, infos = self.snapshot(), []
        try:
            msgs = oc.messages(self.sid)
            idx = max(i for i, m in enumerate(msgs) if m["info"]["role"] == "user")
            self.user_mid = msgs[idx]["info"]["id"]
            parts = [p for m in msgs[idx + 1:] for p in m.get("parts", [])
                     if p.get("type") in ("text", "tool", "reasoning")]
            infos = [m["info"] for m in msgs[idx + 1:]]
        except (OpenCodeError, ValueError) as e:
            log("finish: messages:", e)
        err = self.error
        for info in infos:
            e = info.get("error")
            if e and e.get("name") == "MessageAbortedError":
                self.stopped_at = self.stopped_at or time.time()
            elif e and not err:
                err = e
        for info in reversed(infos):
            tk = info.get("tokens") or {}
            cache = tk.get("cache") or {}
            if tk.get("input") or cache.get("read"):
                self.chat.context = tk.get("input", 0) + cache.get("read", 0) + cache.get("write", 0)
                break
        return parts, err

    def regen_info(self):
        return {"user_mid": self.user_mid, "sid": self.sid} if self.user_mid else None


def friendly_claude_error(err):
    if not err:
        return None
    low = err.lower()
    if any(k in low for k in ("oauth", "unauthorized", "401", "invalid api key", "/login", "authentication",
                              "not logged in", "subscription", "credit balance")):
        return ("🔑 Claude недоступен: закончилась подписка или слетела авторизация. "
                "Можно переключиться на бесплатную модель.")
    if any(k in low for k in ("rate limit", "usage limit", "limit reached", "rate_limit")):
        return "⏳ Лимит Claude на сейчас исчерпан. Попробуйте позже или выберите бесплатную модель."
    return err


def claude_refs(st):
    """Session ids that dialogs still point at (their files must stay)."""
    refs = set()
    for d in [st.get("claude") or {}] + list(st.get("claude_dialogs") or []):
        refs.update(x for x in (d.get("sid"), d.get("prev")) if x)
    return refs


def dialog_title(parts):
    text = next((p["text"] for p in parts if p.get("type") == "text"), "")
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith((">", "[Пользователь"))]
    return short(lines[0] if lines else "Диалог", 48)


class ClaudeRun(Run):
    """A turn of Claude Code (`claude -p`), resumed from the dialog's last session with --fork-session."""
    engine = "claude"

    def __init__(self, chat, job):
        super().__init__(chat, job)
        cl = chat.st.get("claude") or {}
        self.base = job.get("base") if job.get("regen") else cl.get("sid")
        self.turn = None
        self.new_sid = None

    def start(self):
        self.turn = claude.start(self.job["parts"], self.model.split("/", 1)[1], self.effort, resume=self.base,
                                 plan=self.chat.st.get("agent") == "plan", on_change=self._on_change)

    def _on_change(self):
        if self.turn is not None and self.turn.done.is_set():
            self.done.set()
        self.changed()

    def snapshot(self):
        return self.turn.snapshot() if self.turn else []

    def stop(self):
        super().stop()
        if self.turn:
            self.turn.stop()

    def finished(self, now):
        return bool(self.turn and self.turn.done.is_set())

    def settle(self):
        if self.turn:
            self.turn.done.wait(8)

    def collect(self):
        t = self.turn
        parts = self.snapshot()
        err = self.error or (None if self.stopped or not t else t.error())
        if t:
            if t.context:
                self.chat.context = t.context
            if t.rate_limit:
                CLAUDE_LIMITS.update(t.rate_limit)
            self._advance(t, parts, err)
        return parts, friendly_claude_error(err)

    def _advance(self, t, parts, err):
        """Point the dialog at the new session and drop transcripts nobody needs any more."""
        new = t.session_id if t.session_id and t.session_id != self.base else None
        if not new:
            return
        if not parts and err:  # nothing happened in this fork
            claude.forget(new)
            return
        st = self.chat.st
        cl = st.setdefault("claude", {})
        superseded = cl.get("sid") if self.job.get("regen") else cl.get("prev")
        cl.update(sid=new, prev=self.base, updated=time.time())
        cl.setdefault("title", dialog_title(self.job["parts"]))
        self.new_sid = new
        save_state()
        if superseded and superseded not in claude_refs(st):
            claude.forget(superseded)

    def regen_info(self):
        return {"base": self.base, "sid": self.new_sid} if self.new_sid else None


class GeminiRun(Run):
    engine = "gemini"

    def __init__(self, chat, job):
        super().__init__(chat, job)
        st = chat.st
        self.turn = None
        gem_st = st.setdefault("gemini", {})
        self.dialog = gem_st.setdefault("messages", [])
        if job.get("regen") and len(self.dialog) >= 2:
            self.dialog.pop()
            self.dialog.pop()

    def start(self):
        self.turn = gemini.start(self.job["parts"], self.model, self.effort,
                                 dialog=list(self.dialog), on_change=self._on_change)

    def _on_change(self):
        if self.turn is not None and self.turn.done.is_set():
            self.done.set()
        self.changed()

    def snapshot(self):
        return self.turn.snapshot() if self.turn else []

    def stop(self):
        super().stop()
        if self.turn:
            self.turn.stop()

    def finished(self, now):
        return bool(self.turn and self.turn.done.is_set())

    def settle(self):
        if self.turn:
            self.turn.done.wait(5)

    def collect(self):
        parts = self.snapshot()
        err = self.error or (None if self.stopped or not self.turn else self.turn.error())
        if not err and parts:
            prompt_text = "\n\n".join(p["text"] for p in self.job["parts"] if p.get("type") == "text").strip()
            assistant_text = "\n\n".join(p["text"] for p in parts if p.get("type") == "text").strip()
            if prompt_text:
                self.dialog.append({"role": "user", "content": prompt_text})
            if assistant_text:
                self.dialog.append({"role": "assistant", "content": assistant_text})
            gem_st = self.chat.st.setdefault("gemini", {})
            gem_st["updated"] = time.time()
            if not gem_st.get("title") and prompt_text:
                gem_st["title"] = short(prompt_text, 40)
            save_state()
        return parts, err

    def regen_info(self):
        return {"gemini": True}


def changed_files(since):
    return [p for mt, _, p in sorted(walk_files(), reverse=True) if mt >= since]


def answer_keyboard(files):
    rows = [[btn("🔁 Ещё раз", "regen"), btn("🆕 Новый диалог", "new")]]
    fb = [btn(f"📎 {short(p.name, 26)}", f"f:{file_token(p)}") for p in files]
    rows += [fb[i:i + 2] for i in range(0, len(fb), 2)]
    return {"inline_keyboard": rows}


def opencode_session(chat, job):
    """The chat's OpenCode session (created if needed), or None after telling the user."""
    st = chat.st
    try:
        sid = st.get("session")
        if sid and oc.session(sid) is None:
            sid = None
        if not sid:
            sid = oc.create_session()
            st["session"] = sid
            save_state()
        if job.get("revert"):
            try:
                oc.revert(sid, job["revert"])
            except OpenCodeError as e:
                log("revert:", e)
        return sid
    except OpenCodeError as e:
        log("execute:", e)
        send_md(chat, "⚠️ **Сервер OpenCode не отвечает.** Попробуйте через минуту.\n\n" + code(e, 300),
                reply_to=job.get("reply_to"))
        return None


def execute(chat, job):
    st = chat.st
    job.setdefault("model", st["model"])
    job.setdefault("effort", effort_for(st, job["model"]))
    react(chat.id, job.get("reply_to"), "👀")
    if engine_of(job["model"]) == "claude":
        if not claude.available():
            send_md(chat, f"🔑 **Claude не подключён.** Выберите бесплатную модель в /settings.",
                    markup={"inline_keyboard": [[btn(f"{OPENCODE_ICON} Перейти на бесплатную модель", "fallback")]]},
                    reply_to=job.get("reply_to"))
            react(chat.id, job.get("reply_to"))
            return
        run = ClaudeRun(chat, job)
    elif engine_of(job["model"]) == "gemini":
        if not gemini.available():
            send_md(chat, f"🔑 **Gemini роутер недоступен.** Выберите бесплатную модель в /settings.",
                    markup={"inline_keyboard": [[btn(f"{OPENCODE_ICON} Перейти на бесплатную модель", "fallback")]]},
                    reply_to=job.get("reply_to"))
            react(chat.id, job.get("reply_to"))
            return
        run = GeminiRun(chat, job)
    else:
        sid = opencode_session(chat, job)
        if sid is None:
            react(chat.id, job.get("reply_to"))
            return
        run = OpenCodeRun(chat, job, sid)
    if chat.cancelled:
        react(chat.id, job.get("reply_to"))
        return

    chat.run = run
    try:
        run.live = LiveMessage(run)
        run.start()
        run.wait()
    except (OpenCodeError, OSError) as e:
        log("run:", e)
        run.error = str(e)
    finally:
        run.close()
        if run.live:
            run.live.close()
    run.finish()

# ---------- chats ----------

class Chat:
    def __init__(self, chat_id, ctype):
        self.id = chat_id
        self.private = ctype == "private"
        self.queue = Queue()
        self.run = None
        self.question = None
        self.permission = None
        self.last_regen = None
        self.last_markup = None
        self.context = None
        self.last_seen = 0
        self.albums = {}
        self.album_timers = {}   # gid -> Timer, чтобы /stop отменял и их
        self.batch = []          # сообщения без media_group_id для склейки в один запрос
        self.batch_timer = None
        self.caption = []        # фото без текста: ждут подпись с заданием до CAPTION_WAIT
        self.caption_timer = None
        self.lock = threading.Lock()
        self.pending = 0         # queued + running jobs
        self.cancelled = False   # /stop arrived before the current job reached the model
        threading.Thread(target=self.loop, daemon=True).start()

    @property
    def st(self):
        return chat_state(self.id)

    def loop(self):
        while True:
            job = self.queue.get()
            self.cancelled = False
            try:
                execute(self, job)
            except Exception:
                log(traceback.format_exc())
                send_md(self, "⚠️ Внутренняя ошибка бота. Попробуйте ещё раз.")
                react(self.id, job.get("reply_to"))
            finally:
                self.run = None
                with self.lock:
                    self.pending -= 1

    def busy(self):
        return self.pending > 0

    def stop(self):
        drop_pending(self)
        dropped = 0
        while True:
            try:
                job = self.queue.get_nowait()
            except Empty:
                break
            dropped += 1
            react(self.id, job.get("reply_to"))
        with self.lock:
            self.pending -= dropped
            working = self.pending > 0
        if self.question:
            try:
                oc.question_reject(self.question["id"])
            except OpenCodeError:
                pass
        run = self.run
        if run:
            run.stop()
        elif working:
            self.cancelled = True
        return working, dropped

    def strip_answer_buttons(self):
        """Leave only file buttons on the previous answer."""
        old = self.st.get("last_answer")
        if old:
            rows = (self.last_markup or {}).get("inline_keyboard", [])[1:]
            tg_async("editMessageReplyMarkup", chat_id=self.id, message_id=old, reply_markup={"inline_keyboard": rows})
        self.st["last_answer"] = None

    def set_last_answer(self, msg_id, markup):
        if self.st.get("last_answer") != msg_id:
            self.strip_answer_buttons()
        self.st["last_answer"] = msg_id
        self.last_markup = markup
        save_state()

    def enqueue(self, job):
        with self.lock:
            busy = self.pending > 0
            self.pending += 1
        if busy:
            react(self.id, job.get("reply_to"), "👌")
        self.queue.put(job)


CHATS = {}
CHATS_LOCK = threading.Lock()


def get_chat(chat_id, ctype="private"):
    with CHATS_LOCK:
        if chat_id not in CHATS:
            CHATS[chat_id] = Chat(chat_id, ctype)
        return CHATS[chat_id]


# ---------- questions and permissions from the agent ----------

def ask_question(chat, props):
    chat.question = {"id": props["id"], "items": props.get("questions") or [], "idx": 0,
                     "answers": [], "selected": [], "msg": None}
    show_question(chat)


def question_view(q):
    item = q["items"][q["idx"]]
    head = md_escape(item.get("header") or "Вопрос")
    count = f" · {q['idx'] + 1}/{len(q['items'])}" if len(q["items"]) > 1 else ""
    md = f"❓ **{head}**{count}\n\n" + prepare(item.get("question", ""), WORKDIR, upload=False)[0]
    opts = item.get("options") or []
    described = [o for o in opts if o.get("description") and o["description"] != o.get("label")]
    if described:
        md += "\n\n" + "\n".join(f"- **{md_escape(o['label'])}** — {md_escape(o['description'])}" for o in described)
    if item.get("custom", True):
        md += "\n\n_Или напишите свой ответ сообщением._"
    rows = []
    for i, o in enumerate(opts):
        mark = "✅ " if i in q["selected"] else ""
        rows.append([btn(mark + short(o.get("label", "?"), 40), f"q:{i}")])
    last = []
    if item.get("multiple"):
        last.append(btn("✔️ Готово", "q:done", "success"))
    last.append(btn("⏭ Пропустить", "q:skip"))
    rows.append(last)
    return md, {"inline_keyboard": rows}


def show_question(chat):
    q = chat.question
    if not q or not q["items"]:
        return
    md, kb = question_view(q)
    if q["msg"]:
        edit_md(chat, q["msg"], md, kb)
    else:
        q["msg"] = send_md(chat, md, kb)


def answer_question(chat, labels):
    q = chat.question
    if not q:
        return
    q["answers"].append(labels)
    q["idx"] += 1
    q["selected"] = []
    if q["idx"] < len(q["items"]):
        show_question(chat)
        return
    chat.question = None
    summary = "\n\n".join(f"❓ {md_escape(it.get('header') or 'Вопрос')} → ✅ **{md_escape(', '.join(a) or '—')}**"
                          for it, a in zip(q["items"], q["answers"]))
    if q["msg"]:
        edit_md(chat, q["msg"], summary)
    try:
        oc.question_reply(q["id"], q["answers"])
    except OpenCodeError as e:
        send_md(chat, "⚠️ Не удалось передать ответ: " + md_escape(str(e)))


def ask_permission(chat, props):
    pats = ", ".join(props.get("patterns") or [])
    md = (f"🔐 **Агент просит разрешение**\n\n{md_escape(props.get('permission', ''))}"
          + (f": {code(pats, 200)}" if pats else ""))
    kb = {"inline_keyboard": [[btn("✅ Разрешить", "p:once", "success"), btn("♾ Всегда", "p:always"),
                               btn("⛔ Запретить", "p:reject", "danger")]]}
    chat.permission = {"id": props["id"], "md": md, "msg": None}
    chat.permission["msg"] = send_md(chat, md, kb)


# ---------- screens ----------

def welcome_md(chat):
    m = model_info(chat.st["model"])
    claude_line = (f"- {CLAUDE_ICON} Claude Opus 5.5, Fable 5.1 и др. — по вашей подписке\n"
                   if claude.available() else "")
    return (
        f"# 👋 Привет!\n\n"
        f"## 🚀 **Google Gemini AI & Telegram Assistant**\n\n"
        f"Текущая модель: **{engine_icon(m['id'])} {md_escape(short_name(m))}**\n\n"
        "Пишите обычным текстом или используйте выделенные команды:\n"
        "- 🎬 `/video [сцена]` — кинематографичные видео высокого качества (**Google Veo 2**)\n"
        "- 🎵 `/music [стиль]` — генерация треков и музыки (**Google Lyria 3.5**)\n"
        "- 🎨 `/image [промпт]` — генерация изображений (**Nano Banana / Imagen**)\n"
        "- 🔬 `/deepresearch [тема]` — глубокий исследовательский отчёт (**4 Argon Thinking**)\n"
        "- 📊 `/limits` — квоты, лимиты и статистика вашего аккаунта Google One AI Premium\n"
        "- 💬 `/chats` — синхронизация и продолжение диалогов из Gemini Web\n"
        "- 🧮 Формулы, графики, таблицы, чтение PDF, фото и документов\n"
        + claude_line +
        "\n**Быстрый старт:**"
    )


def welcome_kb():
    return {"inline_keyboard": [
        [btn("🎨 Сгенерировать фото", "prompt_guide:img"), btn("🎬 Создать видео", "prompt_guide:vid")],
        [btn("🎵 Создать музыку", "prompt_guide:mus"), btn("🔬 Deep Research", "prompt_guide:dr")],
        [btn("📊 Лимиты Gemini", "limits"), btn("💬 Веб-чаты", "gw:list")],
        [btn("⚙️ Настройки", "settings"), btn("❓ Помощь", "help")],
    ]}


HELP_MD = """## ❓ Команды и возможности ассистента

**Генерация контента:**
- 🎨 `/image <промпт>` (или `/draw`, `/img`) — генерация фото и иллюстраций (**Nano Banana / Imagen**).
- 🎬 `/video <промпт>` (или `/veo`) — создание видеороликов через **Google Veo 2**.
- 🎵 `/music <стиль>` (или `/audio`, `/track`) — генерация музыки через **Google Lyria 3.5**.
- 🔬 `/deepresearch <тема>` — многоэтапное глубокое исследование с фактологическим отчётом (**Gemini 4 Argon High Reasoning**).

**Управление аккаунтом и диалогами:**
- 📊 `/limits` — просмотр квот моделей, статуса Google One AI Premium и статистики запросов за 24ч.
- 💬 `/chats` (или `/gemini`) — список диалогов из веб-версии Gemini с возможностью загрузить любой прямо в Telegram.
- 🆕 `/new` — начать новый чистый диалог.
- ⏹ `/stop` — немедленно остановить генерацию ответа.
- ⚙️ `/settings` (или `/model`) — выбор модели (Gemini, Claude, OpenCode) и глубины рассуждений.
- 🗂 `/sessions` — история всех диалогов.
- 📁 `/files` — файлы в рабочей директории агента.

**Работа в чате:**
- Вы можете отправлять фото, аудио, документы, PDF — модель внимательно их изучит.
- Во время генерации ответа в реальном времени отображаются шаги и мысли модели.
"""


def limits_text():
    """Claude subscription usage from the last rate_limit_event, e.g. «24% за 5 ч · 16% за неделю»."""
    info = CLAUDE_LIMITS
    if not info:
        return None
    if info.get("status") == "rejected" and info.get("resetsAt"):
        return "исчерпан до " + time.strftime("%H:%M", time.localtime(info["resetsAt"]))
    names = {"five_hour": "за 5 ч", "seven_day": "за неделю"}
    parts = [f"{round(w.get('utilization', 0) * 100)}% {names.get(k, k)}"
             for k, w in (info.get("unifiedWindows") or {}).items() if k in names]
    return " · ".join(parts) or None


def dialog_line(chat):
    st = chat.st
    m = model_info(st["model"])
    title = None
    if engine_of(st["model"]) == "claude":
        title = (st.get("claude") or {}).get("title")
    elif engine_of(st["model"]) == "gemini":
        gem_st = st.get("gemini") or {}
        title = gem_st.get("title")
        gem_msgs = gem_st.get("messages", [])
        if not title and gem_msgs:
            title = f"Диалог ({len(gem_msgs) // 2} сообщ.)"
    else:
        try:
            sess = oc.session(st["session"]) if st.get("session") else None
        except OpenCodeError:
            sess = None
        title = sess and (sess.get("title") or "без названия")
    if not title:
        return "новый"
    line = f"«{md_escape(short(title, 50))}»"
    if chat.context and m.get("context"):
        line += f" · контекст {fmt_num(chat.context)} из {fmt_num(m['context'])}"
    return line


def settings_view(chat):
    st = chat.st
    m = model_info(st["model"])
    eff = effort_for(st)
    agent = st.get("agent", "build")
    lines = [f"## ⚙️ Настройки", "", f"### {engine_icon(m['id'])} {md_escape(m['name'])}", ""]
    if eff:
        lines.append(f"- 🧠 **Рассуждения:** {meter(m, eff)} {effort_label(eff)}")
    lines.append("- 🛠 **Режим:** агент — выполняет команды и меняет файлы" if agent == "build"
                 else "- 📋 **Режим:** план — только анализирует и предлагает")
    lines.append(f"- 💬 **Диалог:** {dialog_line(chat)}")
    if engine_of(m["id"]) == "claude" and limits_text():
        lines.append(f"- 📊 **Лимит подписки Claude:** {limits_text()}")
    md = "\n".join(lines)

    rows = [[btn(f"🤖 Модель · {short_name(m)}", "models")]]
    if eff:
        rows.append([btn(f"🧠 Рассуждения · {effort_label(eff)}", "reasoning")])
    rows.append([btn(("✓ " if agent == "build" else "") + "🛠 Агент", "a:build", "primary" if agent == "build" else None),
                 btn(("✓ " if agent == "plan" else "") + "📋 План", "a:plan", "primary" if agent == "plan" else None)])
    rows.append([btn("📊 Лимиты Gemini", "limits"), btn("💬 Веб-чаты", "gw:list")])
    rows.append([btn("🗂 Диалоги", "sessions"), btn("📁 Файлы", "files")])
    rows.append([btn("🆕 Новый диалог", "new"), btn("✖️ Закрыть", "close")])
    return md, {"inline_keyboard": rows}


def limits_view(chat):
    info = gemini.get_limits_info()
    email = info.get("email", "artur.hodzhojan@gmail.com")
    plan = info.get("plan", "Google One AI Premium (Gemini Advanced / Ultra)")
    status = "🟢 Онлайн (Активен)" if info.get("account_status") == "enabled" else "⚠️ Требует проверки"
    cookies_str = f"12/12 ({info.get('cookie_health', 'ok')})"
    last_ok = ago(info["last_ok_at"]) if info.get("last_ok_at") else "недавно"

    reqs = info.get("requests_24h", 0)
    succ = info.get("success_24h", 0)
    rate = f"{round((succ / reqs) * 100)}%" if reqs > 0 else "100%"
    avg_s = f"{info.get('avg_ms_24h', 0)} с" if info.get("avg_ms_24h") else "—"

    lines = [
        "## 📊 Лимиты и статус Gemini",
        "",
        f"👤 **Аккаунт:** `{email}`",
        f"💎 **Тариф:** {plan}",
        f"⚡ **Статус сессии:** {status}",
        f"🛡 **Cookie Pool:** {cookies_str} · проверен {last_ok}",
        f"🔄 **Авто-ротация:** каждые 10 мин (GitHub Actions Runner)",
        "",
        "### ♊ Доступные модели и квоты:",
    ]
    for q in info.get("quotas", []):
        lines.append(f"- {q['status']} **{q['model']}** — `{q['quota']}`")

    lines.extend([
        "",
        "### 📈 Статистика запросов за 24 часа:",
        f"- Всего запросов: **{reqs}** (успешных: **{succ}** · успех **{rate}**)",
        f"- Среднее время ответа (TTFB): **{avg_s}**",
    ])
    by_m = info.get("by_model", {})
    if by_m:
        m_lines = [f"  • `{m}`: {data['total']} зап. ({data['success']} OK)" for m, data in by_m.items()]
        lines.append("- По моделям:\n" + "\n".join(m_lines))

    md = "\n".join(lines)
    rows = [
        [btn("🔄 Обновить статус", "limits"), btn("⚙️ Модели", "models")],
        [btn("🎨 Сгенерировать фото", "prompt_guide:img"), btn("🎬 Создать видео", "prompt_guide:vid")],
        [btn("🎵 Создать музыку", "prompt_guide:mus"), btn("🔬 Deep Research", "prompt_guide:dr")],
        [btn("‹ В настройки", "settings"), btn("✖️ Закрыть", "close")],
    ]
    return md, {"inline_keyboard": rows}


IMG_SAMPLES = [
    ("🐱 Кот-космонавт", "Рыжий кот в детализированном скафандре исследует поверхность Марса на фоне заката, фотореализм 8k"),
    ("🏙 Киберпанк мегаполис", "Футуристический мегаполис в дождливую ночь с неоновыми вывесками, летающими машинами и отражениями в лужах"),
    ("☕ Уютная кофейня", "Акварельная иллюстрация теплой уютной кофейни с книжными полками и мягким утренним светом"),
]

VID_SAMPLES = [
    ("💧 Капли в slow-mo", "Крупный план: капли кристально чистой воды падают на гладкую водную гладь с волнами и брызгами, супер замедленная съемка"),
    ("🚀 Гиперпрыжок", "Космический корабль ускоряется и совершает гиперпространственный прыжок сквозь разноцветную туманность"),
    ("🌊 Штормовой маяк", "Мощные морские волны разбиваются о скалистый берег вокруг старинного светящегося маяка на закате"),
]

MUS_SAMPLES = [
    ("🕹 8-бит Аркада", "Бодрая энергичная мелодия в стиле 8-bit chiptune для ретро-аркадной игры с динамичным битом"),
    ("☕ Lo-Fi Chill", "Расслабляющий спокойный Lo-Fi хип-хоп бит с теплым пианино, виниловым потрескиванием и мягким басом"),
    ("⚡ Синтвейв 80-х", "Драйвовый ретровейв/синтвейв 80-х с мощным аналоговым басом и неоновой атмосферой ночной трассы"),
]

DR_SAMPLES = [
    ("🧠 Эволюция LLM 2026", "Архитектурные инновации в современных больших языковых моделях: сравнение MHA, MLA, MoE и подходов к рассуждениям (reasoning)"),
    ("⚡ EUV литография", "Анализ мирового рынка оборудования для фотолитографии, технологий High-NA EUV и перспектив полупроводниковой индустрии"),
    ("🔐 Постквантовая криптография", "Современные алгоритмы PQC (ML-KEM, SLH-DSA), переход на квантово-устойчивые стандарты безопасности и риски"),
]


def image_guide_view(chat):
    md = (
        "## 🎨 Генерация изображений (Gemini Imagen)\n\n"
        "Отправьте команду: `/image <описание картинки>` (или `/draw`, `/img`).\n\n"
        "Нейросеть Nano Banana / Imagen создаст качественную иллюстрацию и пришлёт её прямо в чат.\n\n"
        "**Быстрые примеры:**"
    )
    rows = [[btn(label, f"do_prompt:img:{i}")] for i, (label, _) in enumerate(IMG_SAMPLES)]
    rows.append([btn("‹ В меню", "settings"), btn("✖️ Закрыть", "close")])
    return md, {"inline_keyboard": rows}


def video_guide_view(chat):
    md = (
        "## 🎬 Генерация видео (Google Veo 2)\n\n"
        "Отправьте команду: `/video <описание сцены>` (или `/veo`).\n\n"
        "Кинематографичная модель **Veo 2** сгенерирует плавный видеоролик в MP4 и отправит его вам как нативное видео в Telegram.\n\n"
        "⏱ *Время генерации обычно занимает от 1 до 2 минут.*\n\n"
        "**Быстрые примеры:**"
    )
    rows = [[btn(label, f"do_prompt:vid:{i}")] for i, (label, _) in enumerate(VID_SAMPLES)]
    rows.append([btn("‹ В меню", "settings"), btn("✖️ Закрыть", "close")])
    return md, {"inline_keyboard": rows}


def music_guide_view(chat):
    md = (
        "## 🎵 Генерация музыки (Google Lyria 3.5)\n\n"
        "Отправьте команду: `/music <стиль или описание трека>` (или `/audio`, `/track`).\n\n"
        "Модель **Lyria 3.5** синтезирует полноценный аудиотрек и отправит его в Telegram.\n\n"
        "**Быстрые примеры:**"
    )
    rows = [[btn(label, f"do_prompt:mus:{i}")] for i, (label, _) in enumerate(MUS_SAMPLES)]
    rows.append([btn("‹ В меню", "settings"), btn("✖️ Закрыть", "close")])
    return md, {"inline_keyboard": rows}


def deepresearch_guide_view(chat):
    md = (
        "## 🔬 Gemini Deep Research (Глубокое исследование)\n\n"
        "Отправьте команду: `/deepresearch <тема исследования>` (или `/research`).\n\n"
        "Флагман **Gemini 4 Argon** с максимальной глубиной рассуждений проведёт структурированный всесторонний анализ темы с фактами, таблицами и выводами.\n\n"
        "**Примеры исследовательских тем:**"
    )
    rows = [[btn(label, f"do_prompt:dr:{i}")] for i, (label, _) in enumerate(DR_SAMPLES)]
    rows.append([btn("‹ В меню", "settings"), btn("✖️ Закрыть", "close")])
    return md, {"inline_keyboard": rows}


def models_view(chat):
    st = chat.st
    cur = st["model"]
    lines = [
        "## 🤖 Выбор модели",
        "",
        "Нажмите на нужную модель. Для моделей с рассуждениями можно настроить глубину ответа.",
        "",
    ]
    if gemini.available():
        lines.append(f"- {GEMINI_ICON} **Gemini** — Google One AI Premium (4 Argon, Pro, Flash, Veo, Lyria)")
    if claude.available():
        lines.append(f"- {CLAUDE_ICON} **Claude** — по вашей подписке")
    else:
        lines.append(f"- {CLAUDE_ICON} **Claude** — не подключён")
    lines.append(f"- {OPENCODE_ICON} **OpenCode** — бесплатные открытые модели")
    rows = []
    for i, m in enumerate(models()):
        on = m["id"] == cur
        tail = f" · {effort_label(effort_for(st, m['id']))}" if on and m["variants"] else ""
        icon = engine_icon(m["id"])
        if "deep-research" in m["id"]:
            icon = "🔬"
        elif "video" in m["id"]:
            icon = "🎬"
        elif "music" in m["id"]:
            icon = "🎵"
        elif "image" in m["id"]:
            icon = "🎨"
        rows.append([btn(f"{'✓ ' if on else ''}{icon} {short_name(m)}{tail}", f"m:{i}",
                         "primary" if on else None)])
    rows.append([btn("📊 Лимиты и статус Gemini", "limits")])
    rows.append([btn("‹ Назад", "settings")])
    return "\n".join(lines), {"inline_keyboard": rows}


def effort_view(chat, i):
    m = models()[i]
    cur = effort_for(chat.st, m["id"])
    md = (f"## 🧠 {engine_icon(m['id'])} {md_escape(m['name'])}\n\n"
          "Выберите глубину рассуждений: глубже — умнее, но дольше.")
    rows = [[btn(f"{'✓ ' if v == cur else ''}{meter(m, v)} {effort_label(v)} — {EFFORTS.get(v, ('', ''))[1]}",
                 f"e:{i}:{v}", "primary" if v == cur else None)] for v in m["variants"]]
    rows.append([btn("‹ К моделям", "models")])
    return md, {"inline_keyboard": rows}


def claude_dialogs(st):
    """(updated, title, callback data, is_current) for the chat's Claude dialogs."""
    out = []
    cur = st.get("claude") or {}
    on_claude = engine_of(st["model"]) == "claude"
    if cur.get("sid"):
        out.append((cur.get("updated", 0), cur.get("title") or "Диалог", "cd:cur", on_claude))
    for j, d in enumerate(st.get("claude_dialogs") or []):
        out.append((d.get("updated", 0), d.get("title") or "Диалог", f"cd:{j}", False))
    return out


def sessions_view(chat):
    st = chat.st
    on_opencode = engine_of(st["model"]) == "opencode"
    on_gemini = engine_of(st["model"]) == "gemini"
    items = [(u, f"{CLAUDE_ICON} {t}", d, c) for u, t, d, c in claude_dialogs(st)]
    cur_gem = st.get("gemini") or {}
    if cur_gem.get("title") or cur_gem.get("messages"):
        items.append((cur_gem.get("updated", time.time()),
                      f"{GEMINI_ICON} {cur_gem.get('title') or 'Диалог Gemini'}", "gw:cur", on_gemini))
    note = ""
    try:
        for s_ in oc.sessions():
            if not s_.get("parentID"):
                items.append(((s_.get("time") or {}).get("updated", 0) / 1000,
                              f"{OPENCODE_ICON} {s_.get('title') or 'без названия'}", f"s:{s_['id']}",
                              on_opencode and s_["id"] == st.get("session")))
    except OpenCodeError:
        note = "\n\n_Список бесплатных диалогов сейчас недоступен._"
    items.sort(key=lambda x: -x[0])
    rows = [[btn(("✓ " if c else "") + short(t, 34) + " · " + ago(u), d, "primary" if c else None)]
            for u, t, d, c in items[:10]]
    rows.append([btn("♊ Веб-чаты Gemini (Google)", "gw:list")])
    rows.append([btn("🆕 Новый диалог", "new"), btn("‹ Назад", "settings")])
    md = "## 🗂 Диалоги\n\n" + ("Выберите, чтобы продолжить:" if items else "Пока нет сохранённых диалогов.") + note
    return md, {"inline_keyboard": rows}


def gemini_chats_view(chat, page=0):
    st = chat.st
    chats = []
    err_msg = ""
    try:
        chats = gemini.list_web_chats(limit=50)
    except Exception as e:
        err_msg = str(e)

    cur_cid = (st.get("gemini") or {}).get("cid")
    if not chats:
        md = ("## ♊ Чаты Gemini Web (Google)\n\n"
              "**Аккаунт:** `artur.hodzhojan@gmail.com`\n\n" +
              (f"⚠️ Не удалось загрузить чаты: _{md_escape(err_msg)}_" if err_msg else "Чаты в веб-версии Gemini не найдены."))
        rows = [[btn("🔄 Обновить", "gw:list")],
                [btn("‹ К диалогам", "sessions"), btn("‹ В настройки", "settings")]]
        return md, {"inline_keyboard": rows}

    per_page = 6
    total_pages = max(1, (len(chats) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    page_chats = chats[page * per_page:(page + 1) * per_page]

    rows = []
    for c in page_chats:
        is_cur = c["id"] == cur_cid
        txt = ("✓ " if is_cur else "") + "💬 " + short(c["title"], 32)
        rows.append([btn(txt, f"gw:o:{c['id']}", "primary" if is_cur else None)])

    nav = []
    if page > 0:
        nav.append(btn("« Назад", f"gw:p:{page - 1}"))
    nav.append(btn(f"{page + 1}/{total_pages}", "gw:noop"))
    if page < total_pages - 1:
        nav.append(btn("Вперёд »", f"gw:p:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([btn("🔄 Обновить", f"gw:p:{page}"), btn("🆕 Новый диалог", "new"), btn("‹ Назад", "sessions")])

    cur_title = (st.get("gemini") or {}).get("title")
    active_line = f"\n\n**Активный чат:** «{md_escape(cur_title)}»" if cur_title else ""
    md = ("## ♊ Чаты Gemini Web (Google)\n"
          "**Аккаунт:** `artur.hodzhojan@gmail.com`" + active_line + "\n\n"
          "Выберите веб-чат, чтобы загрузить его историю и продолжить общение прямо в Telegram:")
    return md, {"inline_keyboard": rows}


def files_view(chat):
    items = sorted(walk_files(SKIP_DIRS - {"uploads"}), reverse=True)[:10]
    rows = [[btn(f"📄 {short(rel(p), 30)} · {fmt_size(size)} · {ago(mt)}", f"f:{file_token(p)}")]
            for mt, size, p in items]
    rows.append([btn("‹ Назад", "settings")])
    md = "## 📁 Файлы\n\nРабочая папка агента. Нажмите на файл, чтобы получить его:" if items else \
        "## 📁 Файлы\n\nПока пусто — агент ещё ничего не создал."
    return md, {"inline_keyboard": rows}


def show_screen(chat, view, msg_id=None):
    md, kb = view(chat) if callable(view) else view
    if msg_id:
        edit_md(chat, msg_id, md, kb)
    else:
        send_md(chat, md, kb)


def send_file(chat, path):
    path = Path(path)
    if not path.is_file():
        send_md(chat, f"Файл {code(rel(path))} не найден")
        return
    r = tg.upload("sendDocument", {"document": path}, chat_id=chat.id)
    if not r.get("ok"):
        send_md(chat, "⚠️ " + md_escape(r.get("description", "не удалось отправить")))


def archive_claude(st):
    """Move the current Claude dialog into the list of past ones."""
    cur = st.get("claude") or {}
    if cur.get("sid"):
        st.setdefault("claude_dialogs", []).insert(0, cur)
        del st["claude_dialogs"][20:]
    st["claude"] = {}


def use_model(st, model_id):
    st["model"] = model_id
    st.setdefault("last_model", {})[engine_of(model_id)] = model_id


def new_dialog(chat, edit_msg=None):
    chat.stop()
    chat.strip_answer_buttons()
    chat.st["session"] = None
    archive_claude(chat.st)
    chat.st["gemini"] = {"messages": [], "title": None, "cid": None}
    chat.context = None
    chat.last_regen = None
    save_state()
    md = "🆕 **Новый диалог.** Контекст очищен — пишите."
    if edit_msg:
        edit_md(chat, edit_msg, md)
    else:
        send_md(chat, md)


# ---------- incoming updates ----------

def handle_command(chat, msg, text):
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    if cmd == "/start":
        send_md(chat, welcome_md(chat), welcome_kb())
    elif cmd == "/help":
        send_md(chat, HELP_MD)
    elif cmd == "/new":
        new_dialog(chat)
    elif cmd == "/stop":
        running, dropped = chat.stop()
        parts = ["⏹ Останавливаю." if running else "Сейчас ничего не выполняется."]
        if dropped:
            parts.append(f"Убрал из очереди: {dropped}.")
        send_md(chat, " ".join(parts))
    elif cmd in ("/settings", "/model", "/effort"):
        show_screen(chat, settings_view)
    elif cmd in ("/limits", "/gemini_limits"):
        show_screen(chat, limits_view)
    elif cmd in ("/chats", "/gemini_chats", "/gemini"):
        show_screen(chat, lambda c: gemini_chats_view(c, 0))
    elif cmd in ("/image", "/draw", "/img"):
        if arg:
            use_model(chat.st, "gemini/gemini-image")
            echo = send_md(chat, f"🎨 **Генерация фото:** _{md_escape(arg)}_")
            chat.enqueue({"parts": [{"type": "text", "text": arg}], "reply_to": echo or msg["message_id"]})
        else:
            show_screen(chat, image_guide_view)
    elif cmd in ("/video", "/veo"):
        if arg:
            use_model(chat.st, "gemini/gemini-video")
            echo = send_md(chat, f"🎬 **Генерация видео (Veo 2):** _{md_escape(arg)}_")
            chat.enqueue({"parts": [{"type": "text", "text": arg}], "reply_to": echo or msg["message_id"]})
        else:
            show_screen(chat, video_guide_view)
    elif cmd in ("/music", "/audio", "/track"):
        if arg:
            use_model(chat.st, "gemini/gemini-music")
            echo = send_md(chat, f"🎵 **Генерация музыки (Lyria):** _{md_escape(arg)}_")
            chat.enqueue({"parts": [{"type": "text", "text": arg}], "reply_to": echo or msg["message_id"]})
        else:
            show_screen(chat, music_guide_view)
    elif cmd in ("/deepresearch", "/research"):
        if arg:
            use_model(chat.st, "gemini/gemini-deep-research")
            dr_text = (
                "🔬 [DEEP RESEARCH MODE: Comprehensive Research Report]\n"
                f"Проведи глубокое, всестороннее исследование по теме:\n\n{arg}\n\n"
                "Структурируй детальный фактологический отчёт: Резюме (Executive Summary), "
                "Ключевые аспекты и механизмы, Сравнительный анализ, Факты и ссылки, Перспективы и выводы."
            )
            echo = send_md(chat, f"🔬 **Deep Research:** _{md_escape(arg)}_")
            chat.enqueue({"parts": [{"type": "text", "text": dr_text}], "reply_to": echo or msg["message_id"]})
        else:
            show_screen(chat, deepresearch_guide_view)
    elif cmd == "/sessions":
        show_screen(chat, sessions_view)
    elif cmd == "/files":
        show_screen(chat, files_view)
    elif cmd == "/get":
        p = Path(os.path.expanduser(arg))
        send_file(chat, p if p.is_absolute() else WORKDIR / p)
    else:
        return False
    return True


def attachment(msg):
    """Download media from a message: (path, mime) or None."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if msg.get("photo"):
        ph = msg["photo"][-1]
        return tg.download(ph["file_id"], UPLOADS / f"photo-{stamp}-{ph['file_unique_id'][:6]}.jpg"), "image/jpeg"
    for key, default_name, default_mime in (("document", "file", "application/octet-stream"),
                                             ("audio", "audio.mp3", "audio/mpeg"),
                                             ("video", "video.mp4", "video/mp4"),
                                             ("video_note", "video_note.mp4", "video/mp4"),
                                             ("animation", "animation.mp4", "video/mp4")):
        m = msg.get(key)
        if m:
            name = re.sub(r"[^\w.\- ]", "_", m.get("file_name") or default_name)
            path = UPLOADS / f"{stamp}-{name}"
            mime = m.get("mime_type") or mimetypes.guess_type(name)[0] or default_mime
            return tg.download(m["file_id"], path), mime
    return None


def file_part(path, mime):
    ext = path.suffix.lower()
    if mime.startswith("image/") and ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        return {"type": "file", "mime": mime, "filename": path.name, "url": f"file://{path}"}
    if mime == "application/pdf":
        return {"type": "file", "mime": mime, "filename": path.name, "url": f"file://{path}"}
    if mime.startswith("text/") or ext in TEXT_EXT:
        return {"type": "file", "mime": "text/plain", "filename": path.name, "url": f"file://{path}"}
    return None


def process_messages(chat, msgs):
    first = msgs[0]
    text = "\n".join(m.get("text") or m.get("caption") or "" for m in msgs).strip()
    has_media = any(k in m for m in msgs for k in MEDIA_KEYS)
    if len(msgs) == 1 and text.startswith("/") and not has_media and handle_command(chat, first, text):
        return
    if chat.question and text and not has_media:
        answer_question(chat, [text])
        react(chat.id, first["message_id"], "👌")
        return
    if any("voice" in m for m in msgs) and not text:
        send_md(chat, "🎙 Голосовые пока не понимаю — напишите, пожалуйста, текстом.", reply_to=first["message_id"])
        return
    if not text and not has_media and first.get("sticker"):
        text = first["sticker"].get("emoji") or ""

    parts, notes = [], []
    for m in msgs:
        try:
            got = attachment(m)
        except Exception as e:
            send_md(chat, "⚠️ Не удалось скачать файл: " + md_escape(str(e)), reply_to=m["message_id"])
            return
        if not got:
            continue
        path, mime = got
        part = file_part(path, mime)
        if part:
            parts.append(part)
        notes.append(f"[Пользователь прислал файл: `{rel(path)}` ({mime}); он сохранён в рабочей папке.]")

    rt = first.get("reply_to_message")
    quote = (first.get("quote") or {}).get("text")
    if not quote and rt:
        quote = rt.get("text") or rt.get("caption")
    body = text or ("Посмотри вложение." if notes else "")
    if not body:
        return
    if quote:
        body = "> " + short(quote, 1500).replace("\n", "\n> ") + "\n\n" + body
    if notes:
        body += "\n\n" + "\n".join(notes)
    parts.insert(0, {"type": "text", "text": body})
    chat.enqueue({"parts": parts, "reply_to": first["message_id"]})


def flush_burst(chat):
    with chat.lock:
        msgs = chat.batch
        chat.batch = []
        chat.batch_timer = None
    if msgs:
        try:
            park_or_send(chat, sorted(msgs, key=lambda m: m["message_id"]))
        except Exception:
            log(traceback.format_exc())


def flush_caption(chat):
    with chat.lock:
        msgs = chat.caption
        chat.caption = []
        chat.caption_timer = None
    if msgs:
        try:
            process_messages(chat, sorted(msgs, key=lambda m: m["message_id"]))
        except Exception:
            log(traceback.format_exc())


def drop_pending(chat):
    """Убрать несклеенные и припаркованные сообщения (для /stop)."""
    with chat.lock:
        chat.batch = []
        t1 = chat.batch_timer
        chat.batch_timer = None
        chat.caption = []
        t2 = chat.caption_timer
        chat.caption_timer = None
        chat.albums = {}
        album_timers = dict(chat.album_timers)
        chat.album_timers = {}
    for t in [t1, t2, *album_timers.values()]:
        if t is not None:
            t.cancel()


def park_or_send(chat, msgs):
    """Пачка с фото, но без текста — ждёт подпись CAPTION_WAIT, остальное сразу."""
    text = "\n".join(m.get("text") or m.get("caption") or "" for m in msgs).strip()
    has_media = any(k in m for m in msgs for k in MEDIA_KEYS)
    if has_media and not text:
        with chat.lock:
            chat.caption.extend(msgs)
            first = chat.caption[0]["message_id"]
            if chat.caption_timer is None:
                chat.caption_timer = threading.Timer(CAPTION_WAIT, lambda: flush_caption(chat))
                chat.caption_timer.daemon = True
                chat.caption_timer.start()
        react(chat.id, first, "👌")
        return
    with chat.lock:
        parked = chat.caption
        chat.caption = []
        t = chat.caption_timer
        chat.caption_timer = None
    if t is not None:
        t.cancel()
    all_msgs = sorted(parked + msgs, key=lambda m: m["message_id"]) if parked else msgs
    process_messages(chat, all_msgs)


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user = msg.get("from") or {}
    if user.get("id") not in ALLOWED:
        log(f"denied user {user.get('id')} ({user.get('username')}) in chat {chat_id}")
        if msg["chat"]["type"] == "private":
            tg("sendMessage", chat_id=chat_id, text=f"Нет доступа. Ваш Telegram id: {user.get('id')}")
        return
    chat = get_chat(chat_id, msg["chat"]["type"])
    chat.last_seen = max(chat.last_seen, msg["message_id"])
    PREMIUM["on"] = bool(user.get("is_premium"))
    kind = next((k for k in ("text",) + MEDIA_KEYS + ("sticker",) if k in msg), "other")
    log(f"<- {kind} #{msg['message_id']} (in Telegram {max(0, int(time.time() - msg.get('date', time.time())))} s ago)")
    gid = msg.get("media_group_id")
    if gid:
        album = chat.albums.get(gid)
        if album is None:
            album = chat.albums[gid] = []
            react(chat.id, msg["message_id"], "👌")

            def flush():
                with chat.lock:
                    chat.album_timers.pop(gid, None)
                    msgs = chat.albums.pop(gid, [])
                if not msgs:
                    return
                try:
                    park_or_send(chat, sorted(msgs, key=lambda m: m["message_id"]))
                except Exception:
                    log(traceback.format_exc())
            t = threading.Timer(1.5, flush)
            t.daemon = True
            chat.album_timers[gid] = t
            t.start()
        album.append(msg)
        return
    text = msg.get("text") or ""
    has_media = any(k in msg for k in MEDIA_KEYS)
    if text.startswith("/") and not has_media:
        if text.split()[0] == "/stop":
            drop_pending(chat)
        else:
            flush_burst(chat)
            flush_caption(chat)
        process_messages(chat, [msg])
        return
    if chat.question and text and not has_media:
        process_messages(chat, [msg])
        return
    if has_media:
        with chat.lock:
            chat.batch.append(msg)
            first = len(chat.batch) == 1 and not chat.caption
            if chat.batch_timer is None:
                chat.batch_timer = threading.Timer(BURST_WINDOW, lambda: flush_burst(chat))
                chat.batch_timer.daemon = True
                chat.batch_timer.start()
        if first:
            react(chat.id, msg["message_id"], "👌")
        return
    with chat.lock:
        parked = chat.caption
        chat.caption = []
        t = chat.caption_timer
        chat.caption_timer = None
        if not parked:
            join = bool(chat.batch)
            if join:
                chat.batch.append(msg)
        else:
            join = False
    if t is not None:
        t.cancel()
    if parked:
        process_messages(chat, sorted(parked + [msg], key=lambda m: m["message_id"]))
        return
    if join:
        return
    process_messages(chat, [msg])


def handle_callback(cq):
    user = cq.get("from") or {}
    msg = cq.get("message") or {}
    if user.get("id") not in ALLOWED or not msg:
        tg("answerCallbackQuery", callback_query_id=cq["id"], text="Нет доступа")
        return
    note = None
    try:
        note = on_button(get_chat(msg["chat"]["id"], msg["chat"]["type"]), msg["message_id"], cq.get("data") or "")
    except Exception:
        log(traceback.format_exc())
        note = "Ошибка — попробуйте ещё раз"
    finally:
        tg("answerCallbackQuery", callback_query_id=cq["id"], text=note)


def on_button(chat, mid, data):
    """Handle an inline button; returns an optional toast text."""
    st = chat.st
    if data == "stop":
        running, _ = chat.stop()
        return "Останавливаю…" if running else "Уже готово"
    if data == "settings":
        show_screen(chat, settings_view, mid)
    elif data == "limits":
        show_screen(chat, limits_view, mid)
    elif data == "prompt_guide:img":
        show_screen(chat, image_guide_view, mid)
    elif data == "prompt_guide:vid":
        show_screen(chat, video_guide_view, mid)
    elif data == "prompt_guide:mus":
        show_screen(chat, music_guide_view, mid)
    elif data == "prompt_guide:dr":
        show_screen(chat, deepresearch_guide_view, mid)
    elif data.startswith("do_prompt:img:"):
        idx = int(data.split(":")[2])
        if idx < len(IMG_SAMPLES):
            prompt = IMG_SAMPLES[idx][1]
            use_model(chat.st, "gemini/gemini-image")
            echo = send_md(chat, f"🎨 **Генерация фото:** _{md_escape(prompt)}_")
            chat.enqueue({"parts": [{"type": "text", "text": prompt}], "reply_to": echo})
            return "Запущена генерация фото"
    elif data.startswith("do_prompt:vid:"):
        idx = int(data.split(":")[2])
        if idx < len(VID_SAMPLES):
            prompt = VID_SAMPLES[idx][1]
            use_model(chat.st, "gemini/gemini-video")
            echo = send_md(chat, f"🎬 **Генерация видео (Veo 2):** _{md_escape(prompt)}_")
            chat.enqueue({"parts": [{"type": "text", "text": prompt}], "reply_to": echo})
            return "Запущена генерация видео"
    elif data.startswith("do_prompt:mus:"):
        idx = int(data.split(":")[2])
        if idx < len(MUS_SAMPLES):
            prompt = MUS_SAMPLES[idx][1]
            use_model(chat.st, "gemini/gemini-music")
            echo = send_md(chat, f"🎵 **Генерация музыки (Lyria):** _{md_escape(prompt)}_")
            chat.enqueue({"parts": [{"type": "text", "text": prompt}], "reply_to": echo})
            return "Запущена генерация музыки"
    elif data.startswith("do_prompt:dr:"):
        idx = int(data.split(":")[2])
        if idx < len(DR_SAMPLES):
            topic = DR_SAMPLES[idx][1]
            use_model(chat.st, "gemini/gemini-deep-research")
            dr_text = (
                "🔬 [DEEP RESEARCH MODE: Comprehensive Research Report]\n"
                f"Проведи глубокое, всестороннее исследование по теме:\n\n{topic}\n\n"
                "Структурируй детальный фактологический отчёт: Резюме (Executive Summary), "
                "Ключевые аспекты и механизмы, Сравнительный анализ, Факты и ссылки, Перспективы и выводы."
            )
            echo = send_md(chat, f"🔬 **Deep Research:** _{md_escape(DR_SAMPLES[idx][0])}_\n_{md_escape(topic)}_")
            chat.enqueue({"parts": [{"type": "text", "text": dr_text}], "reply_to": echo})
            return "Запущен Deep Research"
    elif data == "models":
        show_screen(chat, models_view, mid)
    elif data == "sessions":
        show_screen(chat, sessions_view, mid)
    elif data == "files":
        show_screen(chat, files_view, mid)
    elif data == "help":
        send_md(chat, HELP_MD)
    elif data == "close":
        tg("deleteMessage", chat_id=chat.id, message_id=mid)
    elif data == "new":
        # from a settings screen: replace the screen; from an answer: post a new message
        new_dialog(chat, None if mid == st.get("last_answer") else mid)
        return "Новый диалог"
    elif data == "reasoning":
        lst = models()
        i = next((k for k, m in enumerate(lst) if m["id"] == st["model"]), None)
        if i is None or not lst[i]["variants"]:
            return "У этой модели нет уровней рассуждений"
        show_screen(chat, effort_view(chat, i), mid)
    elif data.startswith("m:"):
        lst = models()
        i = int(data[2:])
        if i >= len(lst):
            show_screen(chat, models_view, mid)
            return "Список моделей обновился"
        m = lst[i]
        if m["variants"]:
            show_screen(chat, effort_view(chat, i), mid)
            return None
        return pick_model(chat, mid, m, None)
    elif data.startswith("e:"):
        _, *rest = data.split(":")
        if len(rest) == 1:  # buttons from older settings screens: level for the current model
            st.setdefault("efforts", {})[st["model"]] = rest[0]
            save_state()
            show_screen(chat, settings_view, mid)
            return f"Рассуждения: {effort_label(rest[0])}"
        lst = models()
        i = int(rest[0])
        if i >= len(lst) or rest[1] not in lst[i]["variants"]:
            show_screen(chat, models_view, mid)
            return "Список моделей обновился"
        return pick_model(chat, mid, lst[i], rest[1])
    elif data.startswith("a:"):
        st["agent"] = "plan" if data == "a:plan" else "build"
        save_state()
        show_screen(chat, settings_view, mid)
        return "Режим: только план" if data == "a:plan" else "Режим: агент"
    elif data.startswith("s:"):
        try:
            sess = oc.session(data[2:])
        except OpenCodeError:
            return "Сервер OpenCode не отвечает"
        if not sess:
            show_screen(chat, sessions_view, mid)
            return "Этого диалога уже нет"
        st["session"] = data[2:]
        if engine_of(st["model"]) != "opencode":
            use_model(st, (st.get("last_model") or {}).get("opencode") or DEFAULT_MODEL)
        switched_dialog(chat, mid, sess.get("title"))
    elif data.startswith("cd:"):
        if not claude.available():
            return "Claude не подключён"
        if data != "cd:cur":
            j = int(data[3:])
            past = st.get("claude_dialogs") or []
            if j >= len(past):
                show_screen(chat, sessions_view, mid)
                return "Список диалогов обновился"
            chosen = past.pop(j)
            archive_claude(st)
            st["claude"] = chosen
        if engine_of(st["model"]) != "claude":
            use_model(st, (st.get("last_model") or {}).get("claude") or DEFAULT_CLAUDE_MODEL)
        switched_dialog(chat, mid, (st.get("claude") or {}).get("title"))
    elif data == "gw:list":
        show_screen(chat, lambda c: gemini_chats_view(c, 0), mid)
    elif data.startswith("gw:p:"):
        try:
            p_idx = int(data.split(":")[2])
        except (IndexError, ValueError):
            p_idx = 0
        show_screen(chat, lambda c: gemini_chats_view(c, p_idx), mid)
    elif data == "gw:cur":
        if engine_of(st["model"]) != "gemini":
            use_model(st, (st.get("last_model") or {}).get("gemini") or "gemini/gemini-4-argon")
        switched_dialog(chat, mid, (st.get("gemini") or {}).get("title"))
    elif data.startswith("gw:o:"):
        conv_id = data.split(":", 2)[2]
        try:
            edit_md(chat, mid, "⏳ Загружаю веб-чат...")
            res = gemini.get_web_chat(conv_id)
            msgs = res.get("messages", [])
            all_chats = gemini.list_web_chats(limit=50)
            found_title = next((c["title"] for c in all_chats if c["id"] == conv_id), None)
            if not found_title:
                found_title = "Веб-чат Gemini"
            if engine_of(st["model"]) != "gemini":
                use_model(st, (st.get("last_model") or {}).get("gemini") or "gemini/gemini-4-argon")
            st["gemini"] = {"cid": conv_id, "title": found_title, "messages": msgs, "updated": time.time()}
            chat.context = None
            chat.last_regen = None
            chat.strip_answer_buttons()
            save_state()
            m = model_info(st["model"])

            # Update menu message
            edit_md(chat, mid, f"✓ Веб-чат «{md_escape(found_title)}» загружен ниже ⬇️",
                    {"inline_keyboard": [
                        [btn("‹ Все веб-чаты", "gw:list"), btn("🆕 Новый диалог", "new")],
                    ]})

            # Render dialogue history
            display_msgs = msgs[-8:] if len(msgs) > 8 else msgs
            header_note = (
                f"📂 **Веб-чат Gemini:** «{md_escape(found_title)}»\n"
                f"Модель: {engine_icon(m['id'])} **{md_escape(short_name(m))}**\n"
            )
            if len(msgs) > len(display_msgs):
                header_note += f"_Показаны последние {len(display_msgs)} из {len(msgs)} сообщений:_"
            else:
                header_note += f"_История диалога ({len(msgs)} сообщ.):_"
            send_md(chat, header_note)

            for m_item in display_msgs:
                role = m_item.get("role")
                content = m_item.get("content") or ""
                images = m_item.get("images") or []

                if role == "user":
                    for img_url in images:
                        try:
                            fpath = gemini.download_web_image(img_url)
                            if fpath and fpath.is_file():
                                tg.upload("sendPhoto", {"photo": fpath}, chat_id=chat.id,
                                          caption="👤 **Вы (фото к вопросу):**")
                        except Exception as e:
                            log("send user photo error:", e)
                    if content:
                        send_md(chat, f"👤 **Вы:**\n\n{content}")

                elif role == "assistant":
                    if content:
                        send_md(chat, f"♊ **Gemini:**\n\n{content}")
                    for img_url in images:
                        try:
                            fpath = gemini.download_web_image(img_url)
                            if fpath and fpath.is_file():
                                tg.upload("sendPhoto", {"photo": fpath}, chat_id=chat.id,
                                          caption="♊ **Gemini (рисунок / чертёж):**")
                        except Exception as e:
                            log("send assistant photo error:", e)

            loaded_text = (
                f"✅ **Диалог готов к продолжению!**\n"
                f"«{md_escape(found_title)}» · {len(msgs)} сообщ. в памяти.\n\n"
                "✍️ **Пишите вопрос прямо сюда** — бот продолжит диалог с учётом всей истории и решений!"
            )
            send_md(chat, loaded_text,
                    markup={"inline_keyboard": [
                        [btn("🔁 Ещё раз", "regen"), btn("🆕 Новый диалог", "new")],
                        [btn("‹ Все веб-чаты", "gw:list"), btn("⚙️ Настройки", "settings")],
                    ]})
            return f"Загружен: {short(found_title, 20)}"
        except Exception as e:
            log("open web chat error:", e)
            return "Ошибка загрузки веб-чата"
    elif data == "gw:noop":
        return None
    elif data == "fallback":
        use_model(st, (st.get("last_model") or {}).get("opencode") or DEFAULT_MODEL)
        save_state()
        m = model_info(st["model"])
        send_md(chat, f"{OPENCODE_ICON} Переключил на **{md_escape(m['name'])}**. Можно повторить вопрос.")
        return short_name(m)
    elif data.startswith("f:"):
        path = FILE_TOKENS.get(data[2:])
        if not path:
            return "Ссылка устарела — откройте /files"
        send_file(chat, path)
    elif data.startswith("x:"):
        i = int(data[2:])
        if i >= len(EXAMPLES):
            return None
        prompt = EXAMPLES[i][1]
        echo = send_md(chat, f"💬 _{md_escape(prompt)}_")
        chat.enqueue({"parts": [{"type": "text", "text": prompt}], "reply_to": echo})
    elif data == "regen":
        lr = chat.last_regen
        if chat.busy():
            return "Подождите — я ещё отвечаю"
        current = (st.get("claude") or {}).get("sid") if (lr or {}).get("engine") == "claude" else st.get("session")
        if not lr or mid != st.get("last_answer") or lr.get("sid") != current:
            return "Повторить можно только последний ответ"
        chat.strip_answer_buttons()
        save_state()
        job = {"parts": lr["parts"], "reply_to": lr["reply_to"], "model": lr["model"]}
        if lr["engine"] == "claude":
            job.update(regen=True, base=lr["base"])
        else:
            job["revert"] = lr["user_mid"]
        chat.enqueue(job)
        return "Генерирую заново"
    elif data.startswith("q:"):
        return on_question_button(chat, mid, data)
    elif data.startswith("p:"):
        p = chat.permission
        if not p:
            return "Запрос уже закрыт"
        chat.permission = None
        reply = {"p:once": "once", "p:always": "always"}.get(data, "reject")
        try:
            oc.permission_reply(p["id"], reply)
        except OpenCodeError as e:
            log("permission reply:", e)
        verdict = {"once": "✅ разрешено", "always": "♾ разрешено всегда", "reject": "⛔ запрещено"}[reply]
        edit_md(chat, mid, p["md"] + f"\n\n**{verdict}**")
    return None


def pick_model(chat, mid, m, effort):
    """Apply a model (and level) chosen on the picker, then return to settings."""
    st = chat.st
    was = engine_of(st["model"])
    use_model(st, m["id"])
    if effort:
        st.setdefault("efforts", {})[m["id"]] = effort
    save_state()
    show_screen(chat, settings_view, mid)
    note = short_name(m) + (f" · {effort_label(effort)}" if effort else "")
    if engine_of(m["id"]) != was:
        note += "\nУ Claude и бесплатных моделей свои диалоги — контекст не переносится"
    return note


def switched_dialog(chat, mid, title):
    chat.context = None
    chat.last_regen = None
    chat.strip_answer_buttons()
    save_state()
    m = model_info(chat.st["model"])
    edit_md(chat, mid, f"💬 Продолжаем диалог «{md_escape(short(title or 'без названия', 60))}» · "
                       f"{engine_icon(m['id'])} {md_escape(short_name(m))}. Пишите.",
            {"inline_keyboard": [[btn("‹ К диалогам", "sessions")]]})


def on_question_button(chat, mid, data):
    q = chat.question
    if not q:
        return "Вопрос уже закрыт"
    item = q["items"][q["idx"]]
    opts = item.get("options") or []
    if data == "q:skip":
        chat.question = None
        edit_md(chat, mid, f"❓ {md_escape(item.get('header') or 'Вопрос')} → ⏭ пропущен")
        try:
            oc.question_reject(q["id"])
        except OpenCodeError as e:
            log("question reject:", e)
    elif data == "q:done":
        answer_question(chat, [opts[i]["label"] for i in sorted(q["selected"]) if i < len(opts)])
    else:
        i = int(data[2:])
        if i >= len(opts):
            return None
        if item.get("multiple"):
            q["selected"] = [x for x in q["selected"] if x != i] if i in q["selected"] else q["selected"] + [i]
            show_question(chat)
        else:
            answer_question(chat, [opts[i]["label"]])
    return None


def dispatch(ev):
    """Route a server event to the run waiting on its session."""
    p = ev.get("properties") or {}
    sid = p.get("sessionID") or (p.get("part") or {}).get("sessionID") or (p.get("info") or {}).get("sessionID")
    with RUNS_LOCK:
        run = RUNS.get(sid) if sid else None
    if run:
        try:
            run.on_event(ev)
        except Exception:
            log(traceback.format_exc())


def event_loop():
    for ev in oc.events():
        dispatch(ev)


def cookie_keeper_loop():
    """Background keeper: periodically rotates Google cookies via gemini-web2api router,
    checkpoints SQLite WAL to disk, and pushes updated state to GitHub so sessions never die."""
    state_key = os.environ.get("STATE_KEY")
    if not state_key:
        return
    admin_url = os.environ.get("GEMINI_ADMIN_URL", "http://127.0.0.1:8083")
    db_file = Path("data/gemini.db")
    enc_file = Path("state/gemini.db.enc")

    time.sleep(15)
    first_run = True
    while True:
        try:
            if not first_run:
                time.sleep(600)  # Check every 10 minutes
            first_run = False
            if not db_file.exists():
                continue

            # 1. Trigger rotate via router admin API
            try:
                req = urllib.request.Request(
                    f"{admin_url}/admin/api/cookies/1/rotate",
                    data=b"{}",
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8", errors="replace"))
                    if resp_data.get("ok"):
                        log("[cookie-keeper] Rotated Google session:", resp_data.get("refreshed"))
            except Exception as e:
                log("[cookie-keeper] router rotate ping:", e)

            # 2. Flush SQLite WAL frames into data/gemini.db
            try:
                con = sqlite3.connect(str(db_file))
                con.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                row = con.execute("SELECT cookie FROM accounts WHERE status='enabled' ORDER BY id DESC LIMIT 1").fetchone()
                con.close()
            except Exception as e:
                log("[cookie-keeper] wal checkpoint error:", e)
                continue

            if not row or not row[0]:
                continue
            cur_cookie = row[0]

            # 3. Check if current cookie differs from committed state.enc
            needs_save = True
            tmp_chk = Path(f"/tmp/chk_gemini_{int(time.time())}.db")
            if enc_file.exists():
                try:
                    cmd_dec = ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt",
                               "-pass", "env:STATE_KEY", "-in", str(enc_file), "-out", str(tmp_chk)]
                    r_dec = subprocess.run(cmd_dec, env=dict(os.environ, STATE_KEY=state_key), capture_output=True)
                    if r_dec.returncode == 0:
                        con2 = sqlite3.connect(str(tmp_chk))
                        old_row = con2.execute("SELECT cookie FROM accounts WHERE status='enabled' ORDER BY id DESC LIMIT 1").fetchone()
                        con2.close()
                        if old_row and old_row[0] == cur_cookie:
                            needs_save = False
                except Exception:
                    pass
                finally:
                    if tmp_chk.exists():
                        try:
                            tmp_chk.unlink()
                        except OSError:
                            pass

            if needs_save:
                enc_file.parent.mkdir(parents=True, exist_ok=True)
                cmd_enc = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt",
                           "-pass", "env:STATE_KEY", "-in", str(db_file), "-out", str(enc_file)]
                r_enc = subprocess.run(cmd_enc, env=dict(os.environ, STATE_KEY=state_key), capture_output=True)
                if r_enc.returncode == 0:
                    subprocess.run(["git", "config", "user.name", "assistant-bot"], capture_output=True)
                    subprocess.run(["git", "config", "user.email", "assistant-bot@users.noreply.github.com"], capture_output=True)
                    subprocess.run(["git", "add", "state/gemini.db.enc"], capture_output=True)
                    commit_msg = f"auto-renew: gemini session cookies ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())})"
                    diff = subprocess.run(["git", "diff", "--staged", "--quiet"])
                    if diff.returncode != 0:
                        subprocess.run(["git", "commit", "-m", commit_msg], capture_output=True)
                        subprocess.run(["git", "pull", "--rebase"], capture_output=True)
                        push_r = subprocess.run(["git", "push", "origin", "main"], capture_output=True)
                        if push_r.returncode == 0:
                            log("[cookie-keeper] Successfully persisted renewed Google session to GitHub!")
                        else:
                            log("[cookie-keeper] git push error:", push_r.stderr.decode()[:200])
        except Exception:
            log("[cookie-keeper] unexpected error:", traceback.format_exc())


def setup_profile():
    tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in [
        ("image", "🎨 Генерация фото"),
        ("video", "🎬 Генерация видео (Veo 2)"),
        ("music", "🎵 Генерация музыки (Lyria)"),
        ("deepresearch", "🔬 Deep Research (4 Argon)"),
        ("limits", "📊 Лимиты и статус Gemini"),
        ("chats", "♊ Веб-чаты Gemini"),
        ("new", "🆕 Новый диалог"),
        ("settings", "⚙️ Модель и настройки"),
        ("stop", "⏹ Остановить ответ"),
        ("sessions", "🗂 Прошлые диалоги"),
        ("files", "📁 Файлы агента"),
        ("help", "❓ Как пользоваться"),
    ]])
    desc = ("AI-ассистент с поддержкой Gemini 4 Argon, Veo 2, Lyria 3.5 и OpenCode: "
            "генерация видео, музыки, картинок, глубокие исследования, код, таблицы и файлы.")
    if (tg("getMyDescription").get("result") or {}).get("description") != desc:
        tg("setMyDescription", description=desc)
    short_desc = "Gemini 4 Argon, Veo 2 & OpenCode: видео, музыка, фото, код и исследования"
    if (tg("getMyShortDescription").get("result") or {}).get("short_description") != short_desc:
        tg("setMyShortDescription", short_description=short_desc)


def main():
    if not TOKEN:
        raise SystemExit("TG_TOKEN не задан (.env)")
    me = tg("getMe")
    if not me.get("ok"):
        raise SystemExit(f"getMe: {me}")
    WORKDIR.mkdir(parents=True, exist_ok=True)
    UPLOADS.mkdir(parents=True, exist_ok=True)
    log(f"@{me['result']['username']} запущен; {DEFAULT_MODEL} ({DEFAULT_EFFORT}); папка {WORKDIR}; "
        f"доступ: {sorted(ALLOWED) or 'никому'}")
    setup_profile()
    threading.Thread(target=event_loop, daemon=True).start()
    threading.Thread(target=cookie_keeper_loop, daemon=True).start()
    offset = None
    shift_until = None
    shift_min = os.environ.get("SHIFT_MINUTES")
    if shift_min:
        try:
            shift_until = time.time() + float(shift_min) * 60
            log(f"Смена активна до {time.strftime('%H:%M:%S', time.localtime(shift_until))} ({shift_min} мин)")
        except ValueError:
            pass
    while True:
        if shift_until and time.time() >= shift_until:
            log("Время смены истекло, ожидаю завершения текущих задач...")
            while any(c.busy() for c in CHATS.values()):
                time.sleep(2)
            log("Все задачи завершены. Завершаю работу смены.")
            break
        r = tg("getUpdates", _timeout=35, _retries=1, timeout=25, offset=offset,
               allowed_updates=["message", "callback_query"])
        if not r.get("ok"):
            if "timed out" not in r.get("description", ""):
                log("getUpdates:", r.get("description"))
                time.sleep(3)
            continue
        for upd in r["result"]:
            offset = upd["update_id"] + 1
            try:
                if "message" in upd:
                    handle_message(upd["message"])
                elif "callback_query" in upd:
                    handle_callback(upd["callback_query"])
            except Exception:
                log(traceback.format_exc())


if __name__ == "__main__":
    main()
