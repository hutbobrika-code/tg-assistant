"""Model markdown -> Telegram rich message markdown.

Telegram rich messages understand GitHub-flavoured markdown with $math$,
tables and <details>, but silently drop unknown HTML tags and treat any
pair of dollars as a formula. prepare() fixes that and turns local image
and file links into uploaded media.
"""
import html
import re
from pathlib import Path
from urllib.parse import unquote

RICH_LIMIT = 30000

ALLOWED_TAGS = {
    "details", "summary", "u", "ins", "sub", "sup", "tg-spoiler", "tg-math", "tg-math-block",
    "mark", "b", "strong", "i", "em", "s", "del", "strike", "br", "aside", "cite", "code", "pre",
    "a", "blockquote", "p", "hr", "ul", "ol", "li", "table", "caption", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6", "footer", "figure", "figcaption",
}

PHOTO_EXT = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
AUDIO_EXT = {".mp3", ".m4a", ".ogg", ".oga", ".opus", ".flac", ".wav"}
PHOTO_MAX = 10 * 1024 * 1024
FILE_MAX = 50 * 1024 * 1024

TOKEN = re.compile(r"""
    \\\[(?P<bdmath>[\s\S]+?)\\\]
  | \\\((?P<bimath>[\s\S]+?)\\\)
  | (?P<esc>\\[\\`*_$<>\[\]|~])
  | (?P<code>`+)(?P<codebody>[\s\S]*?[^`])(?P=code)(?!`)
  | (?P<img>!\[(?P<alt>[^\]\n]*)\]\((?P<src><[^>\n]+>|[^)\s]+)(?:\s+"(?P<title>[^"\n]*)")?\))
  | (?P<link>\[(?P<ltext>[^\]\n]+)\]\((?P<lsrc>[^)\s]+)\))
  | (?P<dmath>\$\$[\s\S]+?\$\$)
  | (?<![\\$\w])\$(?P<imath>[^\s$](?:[^$\n]*?[^\s\\$])?)\$(?![\w$])
  | (?P<tag></?(?P<tagname>[a-zA-Z][\w-]*)(?:\s[^<>]*)?/?>)
  | (?P<lt><)
  | (?P<dollar>\$)
""", re.X)

MATHY = re.compile(r"[\\^_=+\-*/{}0-9]")
SHORT_VAR = re.compile(r"\s*[A-Za-z]{1,3}\s*")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def media_kind(path):
    ext = path.suffix.lower()
    if ext in PHOTO_EXT and path.stat().st_size <= PHOTO_MAX:
        return "photo"
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    return "document"


def resolve_local(src, cwd):
    """Path for a local link target, or None for URLs and anchors."""
    src = src.strip("<>")
    if re.match(r"^(https?|tg|mailto|tel):", src) or src.startswith("#"):
        return None
    if src.startswith("file://"):
        src = src[7:]
    p = Path(unquote(src)).expanduser()
    if not p.is_absolute():
        p = Path(cwd) / p
    return p.resolve()


def inside(path, root):
    try:
        path.relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def md_escape(s):
    """Escape plain text for embedding into rich markdown."""
    s = re.sub(r"([\\`*_\[\]|~$#])", r"\\\1", s)
    return s.replace("<", "&lt;").replace(">", "&gt;")


def _caption(s):
    return s.replace('"', "'").replace("\n", " ")


class Media:
    def __init__(self, upload):
        self.upload = upload
        self.items = []     # (id, kind, path)
        self.trailing = []  # media referenced inline that must go after the text

    def add(self, path):
        for mid, kind, p in self.items:
            if p == path:
                return mid, kind
        mid = f"m{len(self.items) + 1}"
        kind = media_kind(path)
        self.items.append((mid, kind, path))
        return mid, kind


def _in_table_row(m):
    start = m.string.rfind("\n", 0, m.start()) + 1
    return m.string[start:m.start()].lstrip().startswith("|")


def _prose(text, cwd, media):
    def local_file(src):
        """(path, problem) for a local link target; path is None for URLs."""
        path = resolve_local(src, cwd)
        if path is None:
            return None, None
        if not inside(path, cwd):
            return path, "вне рабочей папки"
        if not path.is_file():
            return path, "не найден"
        if path.stat().st_size > FILE_MAX:
            return path, "больше 50 МБ — заберите через /files"
        return path, None

    def sub(m):
        if m.group("esc") or m.group("code") or m.group("dmath") or m.group("imath"):
            return m.group(0)
        if m.group("img"):
            path, problem = local_file(m.group("src"))
            if path is None:
                return m.group(0)
            caption = m.group("title") or m.group("alt")
            icon = "🖼" if path.suffix.lower() in PHOTO_EXT else "📎"
            if problem:
                return f"{icon} *{md_escape(caption or path.name)}* (файл `{path.name}` {problem})"
            if not media.upload:
                return f"{icon} *{md_escape(caption or path.name)}*"
            mid, kind = media.add(path)
            if _in_table_row(m):
                # media is a block; inside a table cell it would break the table
                media.trailing.append((mid, kind, caption or path.name))
                return f"{icon} {md_escape(caption or path.name)}"
            cap = f' "{_caption(caption)}"' if caption else ""
            return f"\n\n![](tg://{kind}?id={mid}{cap})\n\n"
        if m.group("link"):
            path, problem = local_file(m.group("lsrc"))
            if path is None or problem:
                return m.group(0) if path is None else f"**{md_escape(m.group('ltext'))}** (файл {problem})"
            name = m.group("ltext")
            if media.upload:
                mid, kind = media.add(path)
                media.trailing.append((mid, kind, name))
            return f"**{md_escape(name)}**"
        for grp, delim in (("bdmath", "$$"), ("bimath", "$")):
            body = m.group(grp)
            if body is not None:
                # \[text\] may be escaped brackets rather than LaTeX
                if MATHY.search(body) or SHORT_VAR.fullmatch(body):
                    return f"{delim}{body.strip()}{delim}"
                return m.group(0)
        if m.group("tag"):
            if m.group("tagname").lower() in ALLOWED_TAGS:
                return m.group(0)
            return "&lt;" + m.group(0)[1:]
        if m.group("lt"):
            return "&lt;"
        if m.group("dollar"):
            return "\\$"
        return m.group(0)

    return re.sub(r"\n{3,}", "\n\n", TOKEN.sub(sub, text))


def prepare(text, cwd, upload=True):
    """Returns (markdown, [(media_id, kind, path)])."""
    media = Media(upload)
    out, prose, fence = [], [], None
    for line in text.split("\n"):
        m = FENCE.match(line)
        if fence is None:
            if m:
                out.append(_prose("\n".join(prose), cwd, media))
                prose = []
                fence = m.group(1)[0] * len(m.group(1))
                out.append(line)
            else:
                prose.append(line)
        else:
            out.append(line)
            if m and m.group(1).startswith(fence) and not line.strip()[len(m.group(1)):].strip():
                fence = None
    if fence is not None:
        out.append(fence)  # close a fence left open by a streaming draft
    out.append(_prose("\n".join(prose), cwd, media))
    md = "\n".join(out)
    seen = set()
    for mid, kind, name in media.trailing:
        if mid not in seen:
            seen.add(mid)
            md += f'\n\n![](tg://{kind}?id={mid} "{_caption(name)}")'
    return md.strip(), media.items


def split_long(text, limit=RICH_LIMIT):
    """Split on line boundaries, closing and reopening code fences between chunks."""
    if len(text) <= limit:
        return [text]
    lines = []
    for line in text.split("\n"):
        while len(line) > limit:
            lines.append(line[:limit])
            line = line[limit:]
        lines.append(line)
    chunks, cur, size, fence = [], [], 0, None
    for line in lines:
        if size + len(line) + 1 > limit - 10 and cur:
            if fence is not None:
                cur.append(fence)
            chunks.append("\n".join(cur))
            cur = [fence] if fence is not None else []
            size = sum(len(x) + 1 for x in cur)
        cur.append(line)
        size += len(line) + 1
        m = FENCE.match(line)
        if m:
            fence = m.group(1) if fence is None else None
    if cur and "\n".join(cur).strip():
        chunks.append("\n".join(cur))
    return chunks


# ---------- plain HTML fallback for when rich messages fail ----------

def _inline_html(s):
    out, pos = [], 0
    for m in re.finditer(r"`([^`\n]+)`", s):
        out.append(_plain_html(s[pos:m.start()]))
        out.append(f"<code>{html.escape(m.group(1), quote=False)}</code>")
        pos = m.end()
    out.append(_plain_html(s[pos:]))
    return "".join(out)


def _plain_html(s):
    s = html.escape(s, quote=False)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s)
    return s


def md_to_html(text):
    out, code, lang = [], None, ""
    for line in text.split("\n"):
        fence = re.match(r"^\s*```(\S*)", line)
        if fence:
            if code is None:
                code, lang = [], fence.group(1)
            else:
                cls = f' class="language-{html.escape(lang)}"' if lang else ""
                out.append(f"<pre><code{cls}>{html.escape(chr(10).join(code), quote=False)}</code></pre>")
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        h = re.match(r"^#{1,6}\s+(.*)", line)
        if h:
            out.append(f"<b>{_inline_html(h.group(1))}</b>")
            continue
        line = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", line)
        out.append(_inline_html(line))
    if code is not None:
        out.append(f"<pre><code>{html.escape(chr(10).join(code), quote=False)}</code></pre>")
    return "\n".join(out)
