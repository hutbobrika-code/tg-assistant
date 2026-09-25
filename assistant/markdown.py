import html
import re
import subprocess

# html-теги, которые телега понимает в rich-сообщениях. всё остальное с "<"
# экранируем, иначе "a < b" в ответе модели ломает разметку
TAGS = ("b|strong|i|em|u|ins|s|strike|del|code|mark|sub|sup|tg-spoiler|a|tg-reference|tg-emoji|"
        "img|tg-time|tg-math|tg-math-block|h[1-6]|p|pre|footer|hr|ul|ol|li|input|blockquote|"
        "cite|aside|br|video|audio|tg-document|figure|figcaption|tg-map|tg-collage|"
        "tg-slideshow|table|tr|th|td|caption|details|summary")
STRAY_LT = re.compile(rf"<(?!/?(?:{TAGS})[\s>/])")
# куски, внутри которых ничего не трогаем: код и формулы
PROTECTED = re.compile(r"(`+)[\s\S]*?\1|\$\$[\s\S]*?\$\$|\$[^$\n]+\$")
FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w+-]*)")

LIMIT = 30000  # у телеги 32768 на сообщение, оставляем запас


def prepare(text, final=True):
    # ответ модели -> (markdown для телеги, картинки [(id, png)], подсказки)
    out, images, followups = [], [], []
    for kind, lang, body in split_fences(text):
        if lang == "followups":
            if final or kind == "code":
                followups = parse_followups(body)
        elif kind == "text":
            out.append(fix_text(body))
        elif lang == "svg":
            png = svg_to_png(body) if final and kind == "code" else None
            if png:
                images.append((f"img{len(images) + 1}", png))
                out.append(f"\n![](tg://photo?id={images[-1][0]})\n")
            elif final:
                out.append(f"```svg\n{body}\n```")
            else:
                out.append("\n_🖼 рисую схему…_\n")
        else:
            out.append(f"```{lang}\n{body}\n```")
    return "\n".join(out).strip(), images, followups


def split_fences(text):
    # [(вид, язык, текст)], вид: text, code или open (блок кода ещё не закрыт)
    parts, buf, fence, lang = [], [], None, ""
    for line in text.split("\n"):
        if fence is None:
            m = FENCE.match(line)
            if m:
                if buf:
                    parts.append(("text", "", "\n".join(buf)))
                buf, fence, lang = [], m.group(1), m.group(2).lower()
            else:
                buf.append(line)
        elif line.strip() and set(line.strip()) == {fence[0]} and len(line.strip()) >= len(fence):
            parts.append(("code", lang, "\n".join(buf)))
            buf, fence = [], None
        else:
            buf.append(line)
    if fence is not None:
        parts.append(("open", lang, "\n".join(buf)))
    elif buf:
        parts.append(("text", "", "\n".join(buf)))
    return parts


def fix_text(text):
    # формулы вида \( \) и \[ \] телега не понимает, только $ и $$
    text = re.sub(r"\\\[([\s\S]+?)\\\]", lambda m: f"$${m.group(1).strip()}$$", text)
    text = re.sub(r"\\\((.+?)\\\)", lambda m: f"${m.group(1).strip()}$", text)

    out, pos = [], 0
    for m in PROTECTED.finditer(text):
        out.append(STRAY_LT.sub("&lt;", text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(STRAY_LT.sub("&lt;", text[pos:]))
    return "".join(out)


WEB_IMAGE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)[^)]*\)")


def web_images(md):
    return list(dict.fromkeys(WEB_IMAGE.findall(md)))


def drop_images(md, urls=None):
    # убирает картинки по ссылкам (все, если urls не задан) и опустевшие коллажи
    def keep(m):
        return "" if urls is None or m.group(1) in urls else m.group(0)
    md = WEB_IMAGE.sub(keep, md)
    md = re.sub(r"<tg-(collage|slideshow)>\s*</tg-\1>", "", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def without_followups(text):
    # ответ без блока с подсказками, в таком виде он идёт в историю
    return "\n".join(f"```{lang}\n{body}\n```" if kind != "text" else body
                     for kind, lang, body in split_fences(text) if lang != "followups").strip()


def parse_followups(body):
    items = []
    for line in body.split("\n"):
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip().strip("\"«»")
        if line:
            items.append(line[:60])
    return items[:3]


def svg_to_png(svg):
    svg = svg.strip()
    if not svg.startswith("<svg") and "<svg" in svg:
        svg = svg[svg.index("<svg"):]
    if "xmlns=" not in svg:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    try:
        res = subprocess.run(["rsvg-convert", "-f", "png", "-w", "1280", "-b", "white"],
                             input=svg.encode(), capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 and res.stdout else None


def thinking_block(text, seconds):
    # ход мыслей свёрнутым блоком над ответом
    text = text.strip()
    if len(text) > 3500:
        text = "…" + text[-3500:]
    title = f"💭 Думал {duration(seconds)}"
    if not text:
        return ""
    return f"<details><summary>{title}</summary>\n\n{fix_text(text)}\n\n</details>"


def thinking_title(text):
    # заголовок текущего шага рассуждений для черновика: "**Считаю площадь**" -> "Считаю площадь"
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    if not lines:
        return ""
    heads = [line for line in lines if line.startswith("**") and line.endswith("**")]
    title = (heads or lines)[-1].strip("*# ").strip()
    return title[:120]


def split_long(md, limit=LIMIT):
    if len(md) <= limit:
        return [md]
    parts, cur, size, in_code = [], [], 0, False
    for line in md.split("\n"):
        # делим только вне блоков кода, иначе разметка развалится в обоих кусках
        if cur and not in_code and size + len(line) > limit:
            parts.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
        if FENCE.match(line):
            in_code = not in_code
    if cur:
        parts.append("\n".join(cur))
    return parts


def duration(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} с"
    return f"{seconds // 60} мин {seconds % 60} с"


def plain(text):
    return html.escape(text, quote=False)
