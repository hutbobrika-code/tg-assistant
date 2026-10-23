#!/usr/bin/env python3
"""Telegram bot on top of Gemini (through the gemini-web2api router).

Each message goes to the chat's Gemini dialog. While the model works, the chat
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
CLAUDE_BIN = os.path.expanduser(os.environ.get("CLAUDE_BIN", "~/.local/bin/claude"))
CLAUDE_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", GEMINI_MODELS[0]["id"])
DEFAULT_EFFORT = "xhigh"  # Claude; Gemini starts at medium (effort_for)
GEMINI_URL = os.environ.get("GEMINI_URL", "http://127.0.0.1:8083/v1")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "sk-gemini-internal")
WORKDIR = Path(os.path.expanduser(os.environ.get("OC_WORKDIR", str(BASE / "workspace")))).resolve()
UPLOADS = WORKDIR / "uploads"
TIMEOUT = int(os.environ.get("OC_TIMEOUT", "1800"))
STATE_FILE = BASE / "state.json"

tg = Telegram(TOKEN)
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
CLAUDE_ICON, GEMINI_ICON = "✴️", "♊"
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
        if not st["model"].startswith(("gemini/", "claude/")):  # left from the OpenCode models
            st["model"] = DEFAULT_MODEL
        st.pop("session", None)
        return st


def save_state():
    with state_lock:
        data = json.dumps(STATE, indent=1, ensure_ascii=False)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(data)
        tmp.replace(STATE_FILE)


def models():
    """Gemini models; Claude ones only when Claude Code is connected."""
    return (CLAUDE_MODELS if claude.available() else []) + GEMINI_MODELS


def model_info(model_id):
    return next((m for m in models() + CLAUDE_MODELS + GEMINI_MODELS if m["id"] == model_id),
                {"id": model_id, "name": model_id.split("/")[-1], "variants": [], "context": None})


def engine_of(model_id):
    return "claude" if model_id.startswith("claude/") else "gemini"


def engine_icon(model_id):
    return CLAUDE_ICON if engine_of(model_id) == "claude" else GEMINI_ICON


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
    """Reasoning level for a model: picked for model, else medium for Gemini, else st effort, else default."""
    model_id = model_id or st["model"]
    variants = model_info(model_id)["variants"]
    if not variants:
        return None
    picked = (st.get("efforts") or {}).get(model_id)
    if picked in variants:
        return picked
    if "gemini" in model_id:
        return "medium" if "medium" in variants else variants[0]
    general = st.get("effort")
    if general in variants:
        return general
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
        txt = (last.get("text") or "").strip()
        if txt and any(txt.startswith(pfx) for pfx in ("🎬", "🎵", "🎨", "🔬", "⏳")):
            first_line = txt.splitlines()[0]
            return f"**{md_escape(short(first_line, 80))}**"
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


SPINNER = ("◐", "◓", "◑", "◒")
STOP_KB = {"inline_keyboard": [[{"text": "⏹ Остановить", "callback_data": "stop"}]]}
MAIN_REPLY_KEYBOARD = {"remove_keyboard": True}


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
    """One request to a model. GeminiRun and ClaudeRun supply the backend."""
    engine = ""

    def __init__(self, chat, job):
        self.chat, self.job = chat, job
        self.model, self.effort = job["model"], job.get("effort")
        self.started = time.time()
        self.done = threading.Event()
        self.stopped_at = None
        self.timed_out = False
        self.error = None
        self.live = None
        self.followups = []

    @property
    def stopped(self):
        return self.stopped_at is not None

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
            if now - last_typing >= 5:
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
        followups = getattr(self, "followups", [])
        markup = answer_keyboard(files, followups=followups, chat=chat)

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


def friendly_gemini_error(err):
    if not err:
        return None
    if isinstance(err, str):
        low = err.lower()
        if "cookie" in low or "snlm0e" in low or "not signed in" in low:
            return ("⚠️ Сессия Google Gemini обновляется.\n\n"
                    "GitHub Actions автоматически ротирует сессию каждые 10 минут. "
                    "Попробуйте повторить запрос через минуту.")
        if "timed out" in low or "timeout" in low:
            return ("⏱️ Время ожидания ответа от Google Gemini истекло.\n\n"
                    "Модель 4 Argon выполняет глубокие рассуждения — нажмите «🔁 Ещё раз» для повтора.")
        if "media generation succeeded" in low or "artifact retrieval failed" in low or "hnvqhb" in low:
            return ("🎵 Медиафайл создаётся на сервере Google Gemini.\n\n"
                    "Нажмите «🔁 Ещё раз», чтобы забрать готовый результат.")
        if "502" in low and ("upstream" in low or "bad gateway" in low):
            return ("⚠️ Временная ошибка связи с серверами Google Gemini (502 Bad Gateway).\n\n"
                    "Попробуйте нажать «🔁 Ещё раз».")
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
        if self.turn and hasattr(self.turn, "followups"):
            self.followups = list(self.turn.followups)
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
        return parts, friendly_gemini_error(err)

    def regen_info(self):
        return {"gemini": True}


def changed_files(since):
    return [p for mt, _, p in sorted(walk_files(), reverse=True) if mt >= since]


def answer_keyboard(files, followups=None, chat=None):
    rows = []
    if followups and chat is not None:
        chat.st["last_followups"] = followups[:3]
        for idx, fu in enumerate(followups[:3]):
            label = fu.get("label") or fu.get("query") or ""
            rows.append([btn(f"💡 {short(label, 36)}", f"do_fu:{idx}")])
    rows.append([btn("🔁 Ещё раз", "regen"), btn("🆕 Новый диалог", "new")])
    fb = [btn(f"📎 {short(p.name, 26)}", f"f:{file_token(p)}") for p in files]
    rows += [fb[i:i + 2] for i in range(0, len(fb), 2)]
    return {"inline_keyboard": rows}


def execute(chat, job):
    st = chat.st
    job.setdefault("model", st["model"])
    job.setdefault("effort", effort_for(st, job["model"]))
    react(chat.id, job.get("reply_to"), "👀")
    if engine_of(job["model"]) == "claude":
        if not claude.available():
            send_md(chat, f"🔑 **Claude не подключён.** Выберите модель Gemini в /settings.",
                    reply_to=job.get("reply_to"))
            react(chat.id, job.get("reply_to"))
            return
        run = ClaudeRun(chat, job)
    else:
        if not gemini.available():
            send_md(chat, "🔑 **Gemini роутер недоступен.** Попробуйте через минуту.",
                    reply_to=job.get("reply_to"))
            react(chat.id, job.get("reply_to"))
            return
        run = GeminiRun(chat, job)
    if chat.cancelled:
        react(chat.id, job.get("reply_to"))
        return

    chat.run = run
    try:
        run.live = LiveMessage(run)
        run.start()
        run.wait()
    except OSError as e:
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


# ---------- screens ----------

def welcome_md(chat):
    m = model_info(chat.st["model"])
    claude_line = (f"- {CLAUDE_ICON} Claude Opus 5.5, Fable 5.1 и др. — по вашей подписке\n"
                   if claude.available() else "")
    return (
        f"# 👋 Привет!\n\n"
        f"## 🚀 **Google Gemini AI & Telegram Assistant**\n\n"
        f"Текущая модель: **{engine_icon(m['id'])} {md_escape(short_name(m))}**\n\n"
        "Общайтесь с Gemini обычным текстом или используйте кнопки быстрого запуска:\n"
        "- 🎨 **Фото** — генерация изображений (**Nano Banana / Imagen**)\n"
        "- 🎬 **Видео** — кинематографичные ролики (**Google Veo 2**)\n"
        "- 🎵 **Музыка** — синтез аудиотреков (**Google Lyria 3.5**)\n"
        "- 🔬 **Deep Research** — глубокие исследовательские отчёты (**4 Argon Thinking**)\n"
        "- 📊 **Лимиты** — 5-часовые и недельные квоты вашего аккаунта\n"
        "- 💬 **Чаты** — синхронизация диалогов с веб-версией Gemini\n"
        "- 🧮 Формулы, графики, таблицы, анализ PDF, документов и изображений\n"
        + claude_line +
        "\n_Используйте кнопки прямо в сообщении или пишите любой запрос прямо в чат._"
    )


def welcome_kb():
    return {"inline_keyboard": [
        [btn("🎨 Фото", "prompt_guide:img"), btn("🎬 Видео", "prompt_guide:vid"), btn("🎵 Музыка", "prompt_guide:mus")],
        [btn("🔬 Deep Research", "prompt_guide:dr"), btn("📊 Лимиты", "limits"), btn("💬 Веб-чаты", "gw:list")],
        [btn("⚙️ Настройки", "settings"), btn("🆕 Новый диалог", "new")],
    ]}


HELP_MD = """## ❓ Возможности ассистента

**Генерация прямо текстом в чат:**
- 🎨 **Фото:** `нарисуй кота в очках`, `сгенерируй фото спорткара`, `картинка: закат на море`
- 🎬 **Видео:** `сделай видео водопада`, `создай видео как бежит собака`, `видео: ночной город`
- 🎵 **Музыка:** `сделай музыку в стиле рок`, `создай трек лоуфай`, `напиши музыку для отдыха`
- 🔬 **Исследование:** `исследуй тему квантовых процессоров`, `глубокое исследование термоядерного синтеза`

**Управление и настройки:**
Все кнопки (выбор модели, лимиты, история диалогов, веб-чаты) находятся прямо в чате под сообщениями.
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
    else:
        gem_st = st.get("gemini") or {}
        title = gem_st.get("title")
        gem_msgs = gem_st.get("messages", [])
        if not title and gem_msgs:
            title = f"Диалог ({len(gem_msgs) // 2} сообщ.)"
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
    on_claude = engine_of(m["id"]) == "claude"
    lines = [f"## ⚙️ Настройки", "", f"### {engine_icon(m['id'])} {md_escape(m['name'])}", ""]
    if eff:
        lines.append(f"- 🧠 **Рассуждения:** {meter(m, eff)} {effort_label(eff)}")
    if on_claude:
        lines.append("- 🛠 **Режим:** агент — выполняет команды и меняет файлы" if agent == "build"
                     else "- 📋 **Режим:** план — только анализирует и предлагает")
    lines.append(f"- 💬 **Диалог:** {dialog_line(chat)}")
    if engine_of(m["id"]) == "claude" and limits_text():
        lines.append(f"- 📊 **Лимит подписки Claude:** {limits_text()}")
    md = "\n".join(lines)

    rows = [[btn(f"🤖 Модель · {short_name(m)}", "models")]]
    if eff:
        rows.append([btn(f"🧠 Рассуждения · {effort_label(eff)}", "reasoning")])
    if on_claude:
        rows.append([btn(("✓ " if agent == "build" else "") + "🛠 Агент", "a:build", "primary" if agent == "build" else None),
                     btn(("✓ " if agent == "plan" else "") + "📋 План", "a:plan", "primary" if agent == "plan" else None)])
    rows.append([btn("📊 Лимиты Gemini", "limits"), btn("💬 Веб-чаты", "gw:list")])
    rows.append([btn("🗂 Диалоги", "sessions"), btn("📁 Файлы", "files")])
    rows.append([btn("🆕 Новый диалог", "new"), btn("✖️ Закрыть", "close")])
    return md, {"inline_keyboard": rows}


def make_progress_bar(pct, length=12):
    if pct is None:
        return "░" * length
    pct = max(0, min(100, pct))
    filled = int(round(pct / 100 * length))
    empty = length - filled
    return "█" * filled + "░" * empty


def limits_view(chat):
    info = gemini.get_limits_info()
    web = info.get("web_limits")

    tier_label = web.get("tier", "PRO") if web else "PRO"

    lines = [
        f"## 📊 Лимиты на использование Gemini · `{tier_label}`",
        "_От лимитов тарифного плана зависит, как часто вы можете использовать Gemini за определенный период._",
        "",
    ]

    if web:
        cur = web.get("current") or {}
        wk = web.get("weekly") or {}
        c_pct = cur.get("percent", 0)
        c_reset = cur.get("reset_time", "—")
        w_pct = wk.get("percent", 0)
        w_reset = wk.get("reset_time", "—")

        c_bar = make_progress_bar(c_pct, 12)
        w_bar = make_progress_bar(w_pct, 12)

        lines.extend([
            f"### ⏱ Текущее использование (5 ч): **Использовано {c_pct} %**",
            f"`[{c_bar}] {c_pct}%`",
            f"> ⏳ _Лимит обновится в {c_reset}_",
            "",
            f"### 📅 Недельный лимит: **Использовано {w_pct} %**",
            f"`[{w_bar}] {w_pct}%`",
            f"> ⏳ _Лимит обновится {w_reset}_",
        ])
    else:
        lines.extend([
            "### ⏱ Текущее использование (5 ч)",
            "`[░░░░░░░░░░░░] 0%`",
            "> ⏳ _Лимит доступен в полном объёме_",
            "",
            "### 📅 Недельный лимит",
            "`[░░░░░░░░░░░░] 0%`",
            "> ⏳ _Лимит доступен в полном объёме_",
        ])

    md = "\n".join(lines)
    rows = [
        [btn("🔄 Обновить лимиты", "limits")],
        [btn("🤖 Модели", "models")],
        [btn("‹ В настройки", "settings"), btn("✖️ Закрыть", "close")],
    ]
    return md, {"inline_keyboard": rows}


IMG_SAMPLES = [
    ("🐱 Кот-космонавт", "Рыжий кот в детализированном скафандре исследует поверхность Марса на фоне заката, фотореализм 8k"),
    ("🏙 Киберпанк мегаполис", "Футуристический мегаполис в дождливую ночь с неоновыми вывесками, летающими машинами и отражениями в лужах"),
    ("☕ Уютная кофейня", "Акварельная иллюстрация теплой уютной кофейни с книжными полками и мягким утренним светом"),
    ("🐉 Изумрудный дракон", "Величественный восточный изумрудный дракон парит над заснеженными вершинами гор на рассвете, фэнтези арт"),
]

VID_SAMPLES = [
    ("💧 Капли в slow-mo", "Крупный план: капли кристально чистой воды падают на гладкую водную гладь с волнами и брызгами, супер замедленная съемка"),
    ("🚀 Гиперпрыжок", "Космический корабль ускоряется и совершает гиперпространственный прыжок сквозь разноцветную туманность"),
    ("🌊 Штормовой маяк", "Мощные морские волны разбиваются о скалистый берег вокруг старинного светящегося маяка на закате"),
    ("🏎 Ночной дрифт", "Спортивный неоновый суперкар скользит в дрифте по мокрому асфальту ночного мегаполиса, клубы дыма из-под колес"),
]

MUS_SAMPLES = [
    ("⚡ Синтвейв 80-х", "Драйвовый ретровейв/синтвейв 80-х с мощным аналоговым басом и неоновой атмосферой ночной трассы"),
    ("☕ Lo-Fi Chill", "Расслабляющий спокойный Lo-Fi хип-хоп бит с теплым пианино, виниловым потрескиванием и мягким басом"),
    ("🎸 Драйвовый Рок", "Энергичный рок-трек с мощными гитарными риффами, перегруженным басом и живыми ударными"),
    ("🌌 Ambient Космос", "Глубокий атмосферный космический эмбиент с воздушными пэдами, мерцающими синтами и ощущением невесомости"),
    ("🎹 Неоклассика", "Эмоциональная неоклассическая мелодия на акустическом рояле в сопровождении струнного оркестра"),
    ("🕹 8-бит Аркада", "Бодрая энергичная мелодия в стиле 8-bit chiptune для ретро-аркадной игры с динамичным битом"),
]

DR_SAMPLES = [
    ("🧠 Эволюция LLM 2026", "Архитектурные инновации в современных больших языковых моделях: сравнение MHA, MLA, MoE и подходов к рассуждениям (reasoning)"),
    ("⚡ EUV литография", "Анализ мирового рынка оборудования для фотолитографии, технологий High-NA EUV и перспектив полупроводниковой индустрии"),
    ("🔐 Постквантовая криптография", "Современные алгоритмы PQC (ML-KEM, SLH-DSA), переход на квантово-устойчивые стандарты безопасности и риски"),
    ("🚀 Частная космонавтика", "Сравнительный анализ проектов многоразовых сверхтяжёлых ракет, орбитальной заправки и марсианских экспедиций на период до 2030 года"),
]


def image_guide_view(chat):
    md = "🎨 **Режим фото.** Напишите, что сгенерировать:"
    return md, {"inline_keyboard": [[btn("✖️ Отмена", "cancel_pending")]]}


def video_guide_view(chat):
    md = "🎬 **Режим видео (Google Veo 2).** Напишите описание сцены:"
    return md, {"inline_keyboard": [[btn("✖️ Отмена", "cancel_pending")]]}


def music_guide_view(chat):
    md = "🎵 **Режим музыки (Google Lyria).** Напишите стиль или описание трека:"
    return md, {"inline_keyboard": [[btn("✖️ Отмена", "cancel_pending")]]}


def deepresearch_guide_view(chat):
    md = "🔬 **Deep Research.** Напишите тему для глубокого исследования:"
    return md, {"inline_keyboard": [[btn("✖️ Отмена", "cancel_pending")]]}


def models_view(chat):
    st = chat.st
    cur = st["model"]
    lines = [
        "## 🤖 Выбор модели",
        "",
        "Нажмите на нужную модель. Для моделей с рассуждениями можно настроить глубину ответа.",
        "",
    ]
    lines.append(f"- {GEMINI_ICON} **Gemini** — Google One AI Premium (4 Argon, Pro, Flash, Veo, Lyria)"
                 + ("" if gemini.available() else " · _роутер сейчас недоступен_"))
    if claude.available():
        lines.append(f"- {CLAUDE_ICON} **Claude** — по вашей подписке")
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
    on_gemini = engine_of(st["model"]) == "gemini"
    items = [(u, f"{CLAUDE_ICON} {t}", d, c) for u, t, d, c in claude_dialogs(st)]
    cur_gem = st.get("gemini") or {}
    if cur_gem.get("title") or cur_gem.get("messages"):
        items.append((cur_gem.get("updated", time.time()),
                      f"{GEMINI_ICON} {cur_gem.get('title') or 'Диалог Gemini'}", "gw:cur", on_gemini))
    items.sort(key=lambda x: -x[0])
    rows = [[btn(("✓ " if c else "") + short(t, 34) + " · " + ago(u), d, "primary" if c else None)]
            for u, t, d, c in items[:10]]
    rows.append([btn("♊ Веб-чаты Gemini (Google)", "gw:list")])
    rows.append([btn("🆕 Новый диалог", "new"), btn("‹ Назад", "settings")])
    md = "## 🗂 Диалоги\n\n" + ("Выберите, чтобы продолжить:" if items else "Пока нет сохранённых диалогов.")
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
    archive_claude(chat.st)
    chat.st["gemini"] = {"messages": [], "title": None, "cid": None}
    chat.context = None
    chat.last_regen = None
    chat.st.pop("pending_mode", None)
    save_state()
    md = "🆕 **Новый диалог.** Контекст очищен — пишите."
    inline_kb = {
        "inline_keyboard": [
            [btn("🎨 Фото", "prompt_guide:img"), btn("🎬 Видео", "prompt_guide:vid"), btn("🎵 Музыка", "prompt_guide:mus")],
            [btn("🔬 Deep Research", "prompt_guide:dr"), btn("📊 Лимиты", "limits"), btn("⚙️ Настройки", "settings")],
        ]
    }
    if edit_msg:
        edit_md(chat, edit_msg, md, markup=inline_kb)
    else:
        if not chat.st.get("kb_removed"):
            r = tg("sendMessage", chat_id=chat.id, text="✨", reply_markup={"remove_keyboard": True})
            if r.get("ok"):
                try:
                    tg("deleteMessage", chat_id=chat.id, message_id=r["result"]["message_id"])
                except Exception:
                    pass
            chat.st["kb_removed"] = True
            save_state()
        send_md(chat, md, markup=inline_kb)


# ---------- incoming updates ----------

def handle_command(chat, msg, text):
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    if cmd == "/start":
        tg("sendMessage", chat_id=chat.id, text="👋 **Ассистент готов к работе!** Все кнопки и управление находятся прямо в чате.", reply_markup={"remove_keyboard": True}, parse_mode="Markdown")
        chat.st["kb_removed"] = True
        save_state()
        send_md(chat, welcome_md(chat), markup=welcome_kb())
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


def handle_menu_button(chat, text, msg):
    t = text.strip()
    if not chat.st.get("kb_removed"):
        tg("sendMessage", chat_id=chat.id, text="📌 Нижняя панель скрыта — кнопки теперь прямо в чате.", reply_markup={"remove_keyboard": True})
        chat.st["kb_removed"] = True
        save_state()
    if t in ("🎨 Фото", "Фото", "🎨 Сгенерировать фото"):
        chat.st["pending_mode"] = "image"
        show_screen(chat, image_guide_view)
        return True
    if t in ("🎬 Видео", "Видео", "🎬 Создать видео"):
        chat.st["pending_mode"] = "video"
        show_screen(chat, video_guide_view)
        return True
    if t in ("🎵 Музыка", "Музыка", "🎵 Создать музыку"):
        chat.st["pending_mode"] = "music"
        show_screen(chat, music_guide_view)
        return True
    if t in ("🔬 Исследование", "Исследование", "🔬 Deep Research"):
        chat.st["pending_mode"] = "deepresearch"
        show_screen(chat, deepresearch_guide_view)
        return True
    if t in ("📊 Лимиты", "Лимиты", "📊 Лимиты Gemini"):
        chat.st.pop("pending_mode", None)
        if gemini.available():
            try:
                gemini.get_limits_info(force_web=True)
            except Exception:
                pass
        show_screen(chat, limits_view)
        return True
    if t in ("♊ Чаты", "Чаты", "💬 Веб-чаты", "Веб-чаты"):
        chat.st.pop("pending_mode", None)
        show_screen(chat, lambda c: gemini_chats_view(c, 0))
        return True
    if t in ("⚙️ Настройки", "Настройки", "🤖 Модели"):
        chat.st.pop("pending_mode", None)
        show_screen(chat, settings_view)
        return True
    if t in ("🆕 Новый диалог", "Новый диалог"):
        chat.st.pop("pending_mode", None)
        new_dialog(chat)
        return True
    return False


def process_messages(chat, msgs):
    first = msgs[0]
    text = "\n".join(m.get("text") or m.get("caption") or "" for m in msgs).strip()
    has_media = any(k in m for m in msgs for k in MEDIA_KEYS)

    if not chat.st.get("kb_removed"):
        r = tg("sendMessage", chat_id=chat.id, text="✨", reply_markup={"remove_keyboard": True})
        if r.get("ok"):
            try:
                tg("deleteMessage", chat_id=chat.id, message_id=r["result"]["message_id"])
            except Exception:
                pass
        chat.st["kb_removed"] = True
        save_state()

    if len(msgs) == 1 and not has_media:
        if text.startswith("/") and handle_command(chat, first, text):
            return
        if handle_menu_button(chat, text, first):
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

    # Check for pending modal actions triggered by menu buttons
    pending = chat.st.pop("pending_mode", None)
    if pending == "image" and body:
        use_model(chat.st, "gemini/gemini-image")
        echo = send_md(chat, f"🎨 **Генерация фото:** _{md_escape(body)}_", reply_to=first["message_id"])
        chat.enqueue({"parts": [{"type": "text", "text": body}], "reply_to": echo or first["message_id"]})
        return
    elif pending == "video" and body:
        use_model(chat.st, "gemini/gemini-video")
        echo = send_md(chat, f"🎬 **Генерация видео (Veo 2):** _{md_escape(body)}_", reply_to=first["message_id"])
        chat.enqueue({"parts": [{"type": "text", "text": body}], "reply_to": echo or first["message_id"]})
        return
    elif pending == "music" and body:
        use_model(chat.st, "gemini/gemini-music")
        echo = send_md(chat, f"🎵 **Генерация музыки (Lyria):** _{md_escape(body)}_", reply_to=first["message_id"])
        chat.enqueue({"parts": [{"type": "text", "text": body}], "reply_to": echo or first["message_id"]})
        return
    elif pending == "deepresearch" and body:
        use_model(chat.st, "gemini/gemini-deep-research")
        dr_text = (
            "🔬 [DEEP RESEARCH MODE: Comprehensive Research Report]\n"
            f"Проведи глубокое, всестороннее исследование по теме:\n\n{body}\n\n"
            "Структурируй детальный фактологический отчёт: Резюме (Executive Summary), "
            "Ключевые аспекты и механизмы, Сравнительный анализ, Факты и ссылки, Перспективы и выводы."
        )
        echo = send_md(chat, f"🔬 **Deep Research:** _{md_escape(short(body, 60))}_", reply_to=first["message_id"])
        chat.enqueue({"parts": [{"type": "text", "text": dr_text}], "reply_to": echo or first["message_id"]})
        return

    # Direct prompt intent detection: user just types a prompt directly without clicking buttons or menus
    if body and not quote:
        img_match = re.match(
            r"^(?:нарисуй(?:\s+мне)?|отрисуй(?:\s+мне)?|изобрази(?:\s+мне)?|(?:сгенерируй|создай|сделай)(?:\s+мне)?\s+(?:фото|картинку|изображение|арт|рисунок|иллюстрацию)|картинка:|фото:|изображение:)\s*(.+)$",
            body.strip(),
            re.IGNORECASE | re.DOTALL,
        )
        if img_match:
            prompt_content = img_match.group(1).strip()
            use_model(chat.st, "gemini/gemini-image")
            echo = send_md(chat, f"🎨 **Генерация фото:** _{md_escape(prompt_content)}_", reply_to=first["message_id"])
            parts.insert(0, {"type": "text", "text": prompt_content})
            chat.enqueue({"parts": parts, "reply_to": echo or first["message_id"]})
            return

        vid_match = re.match(
            r"^(?:(?:сгенерируй|создай|сделай|смонтируй)(?:\s+мне)?\s+видео(?:\s*ролик|\s*клип)?|видео:|ролик:|видеоклип:)\s*(.+)$",
            body.strip(),
            re.IGNORECASE | re.DOTALL,
        )
        if vid_match:
            prompt_content = vid_match.group(1).strip()
            use_model(chat.st, "gemini/gemini-video")
            echo = send_md(chat, f"🎬 **Генерация видео (Veo 2):** _{md_escape(prompt_content)}_", reply_to=first["message_id"])
            chat.enqueue({"parts": [{"type": "text", "text": prompt_content}], "reply_to": echo or first["message_id"]})
            return

        mus_match = re.match(
            r"^(?:(?:сгенерируй|создай|сделай|напиши|сочини)(?:\s+мне)?\s+(?:музыку|трек|песню|мелодию|бит)|музыка:|трек:|мелодия:)\s*(.+)$",
            body.strip(),
            re.IGNORECASE | re.DOTALL,
        )
        if mus_match:
            prompt_content = mus_match.group(1).strip()
            use_model(chat.st, "gemini/gemini-music")
            echo = send_md(chat, f"🎵 **Генерация музыки (Lyria):** _{md_escape(prompt_content)}_", reply_to=first["message_id"])
            chat.enqueue({"parts": [{"type": "text", "text": prompt_content}], "reply_to": echo or first["message_id"]})
            return

        dr_match = re.match(
            r"^(?:глубокое\s+исследование|исследуй(?:\s+тему)?|проведи\s+исследование(?:\s+по)?|исследование:)\s*(.+)$",
            body.strip(),
            re.IGNORECASE | re.DOTALL,
        )
        if dr_match:
            topic = dr_match.group(1).strip()
            use_model(chat.st, "gemini/gemini-deep-research")
            dr_text = (
                "🔬 [DEEP RESEARCH MODE: Comprehensive Research Report]\n"
                f"Проведи глубокое, всестороннее исследование по теме:\n\n{topic}\n\n"
                "Структурируй детальный фактологический отчёт: Резюме (Executive Summary), "
                "Ключевые аспекты и механизмы, Сравнительный анализ, Факты и ссылки, Перспективы и выводы."
            )
            echo = send_md(chat, f"🔬 **Deep Research:** _{md_escape(short(topic, 60))}_", reply_to=first["message_id"])
            chat.enqueue({"parts": [{"type": "text", "text": dr_text}], "reply_to": echo or first["message_id"]})
            return

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
        if not running:
            tg_async("deleteMessage", chat_id=chat.id, message_id=mid)
        return "Останавливаю…" if running else "Уже готово"
    if data == "settings":
        show_screen(chat, settings_view, mid)
    elif data == "limits":
        if gemini.available():
            try:
                gemini.get_limits_info(force_web=True)
            except Exception:
                pass
        show_screen(chat, limits_view, mid)
        return "Лимиты обновлены"
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
    elif data.startswith("do_fu:"):
        idx = int(data.split(":")[1])
        fu_list = chat.st.get("last_followups") or []
        if 0 <= idx < len(fu_list):
            fu = fu_list[idx]
            query = fu.get("query") or fu.get("label") or ""
            label = fu.get("label") or query
            if query:
                echo = send_md(chat, f"💡 **{md_escape(label)}**")
                chat.enqueue({"parts": [{"type": "text", "text": query}], "reply_to": echo or mid})
                return f"Запрос: {short(label, 20)}"
        return "Предложение устарело"
    elif data == "cancel_pending":
        chat.st.pop("pending_mode", None)
        tg("deleteMessage", chat_id=chat.id, message_id=mid)
        return "Отменено"
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
        on_claude = (lr or {}).get("engine") == "claude"
        if not lr or mid != st.get("last_answer") or \
                (on_claude and lr.get("sid") != (st.get("claude") or {}).get("sid")):
            return "Повторить можно только последний ответ"
        chat.strip_answer_buttons()
        save_state()
        job = {"parts": lr["parts"], "reply_to": lr["reply_to"], "model": lr["model"], "regen": True}
        if on_claude:
            job["base"] = lr["base"]
        chat.enqueue(job)
        return "Генерирую заново"
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
        note += "\nУ Claude и Gemini свои диалоги — контекст не переносится"
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
    # Remove all slash commands and command autocomplete menus
    tg("deleteMyCommands")
    tg("deleteMyCommands", scope={"type": "all_private_chats"})
    tg("deleteMyCommands", scope={"type": "all_group_chats"})
    tg("deleteMyCommands", scope={"type": "all_chat_administrators"})
    tg("setChatMenuButton", menu_button={"type": "default"})
    desc = ("AI-ассистент на Gemini 4 Argon, Veo 2 и Lyria 3.5: "
            "генерация видео, музыки, картинок, глубокие исследования, код, таблицы и файлы.")
    if (tg("getMyDescription").get("result") or {}).get("description") != desc:
        tg("setMyDescription", description=desc)
    short_desc = "Gemini 4 Argon, Veo 2 & Lyria: видео, музыка, фото, код и исследования"
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
    log(f"@{me['result']['username']} запущен; {DEFAULT_MODEL}; папка {WORKDIR}; "
        f"доступ: {sorted(ALLOWED) or 'никому'}")
    setup_profile()
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
