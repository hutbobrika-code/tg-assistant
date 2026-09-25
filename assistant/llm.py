import base64
import json
import urllib.error
import urllib.request

from . import log
from .config import settings
from .web import TOOLS

MAX_ROUNDS = 6  # сколько раз подряд модель может сходить в интернет
no_tools = set()  # модели, которые не приняли инструменты


class LLMError(Exception):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class ToolsRejected(Exception):
    pass


def stream(model, system, messages, effort=None, stop=None, tools=None):
    # события: ("thinking", кусок), ("text", кусок), ("tool", (имя, аргументы)).
    # tools - функция (имя, аргументы) -> результат; None значит без интернета
    loop = responses_loop if model.api == "responses" else chat_loop
    if tools and model.id not in no_tools:
        try:
            yield from loop(model, system, messages, effort, stop, tools)
            return
        except ToolsRejected as e:
            log(f"{model.id} не принимает инструменты, отвечаю без интернета: {e}")
            no_tools.add(model.id)
    yield from loop(model, system, messages, effort, stop, None)


def responses_loop(model, system, messages, effort, stop, tools):
    items = responses_input(messages)
    for round_ in range(MAX_ROUNDS + 1):
        use_tools = tools and round_ < MAX_ROUNDS
        body = {"model": model.id, "instructions": system, "input": items,
                "stream": True, "store": False}
        if effort:
            body["reasoning"] = {"effort": effort, "summary": "auto"}
            body["include"] = ["reasoning.encrypted_content"]
        if use_tools:
            body["tools"] = [{"type": "function", **t} for t in TOOLS]

        output = []
        for ev in post("/responses", body, probe=use_tools and round_ == 0):
            if stop is not None and stop.is_set():
                return
            kind = ev.get("type")
            if kind == "response.output_text.delta":
                yield "text", ev.get("delta", "")
            elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                yield "thinking", ev.get("delta", "")
            elif kind == "response.reasoning_summary_part.added":
                yield "thinking", "\n\n"
            elif kind == "response.output_item.done":
                output.append(ev["item"])
            elif kind == "response.completed":
                output = (ev.get("response") or {}).get("output") or output
            elif kind in ("response.failed", "error"):
                err = ev.get("error") or (ev.get("response") or {}).get("error") or ev
                raise LLMError(error_text(json.dumps({"error": err})))

        calls = [o for o in output if o.get("type") == "function_call"]
        if not calls:
            return
        items += output  # вместе с зашифрованными рассуждениями, чтобы модель не теряла мысль
        for call in calls:
            args = parse_args(call.get("arguments"))
            yield "tool", (call.get("name"), args)
            items.append({"type": "function_call_output", "call_id": call["call_id"],
                          "output": tools(call.get("name"), args)})


def chat_loop(model, system, messages, effort, stop, tools):
    msgs = chat_messages(system, messages)
    for round_ in range(MAX_ROUNDS + 1):
        use_tools = tools and round_ < MAX_ROUNDS
        body = {"model": model.id, "messages": msgs, "stream": True}
        if effort:
            body["reasoning_effort"] = effort
        if use_tools:
            body["tools"] = [{"type": "function", "function": t} for t in TOOLS]

        text, calls = "", {}
        for ev in post("/chat/completions", body, probe=use_tools and round_ == 0):
            if stop is not None and stop.is_set():
                return
            if ev.get("error"):
                raise LLMError(error_text(json.dumps(ev)))
            for choice in ev.get("choices", []):
                delta = choice.get("delta") or {}
                thinking = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(thinking, str) and thinking:
                    yield "thinking", thinking
                if delta.get("content"):
                    text += delta["content"]
                    yield "text", delta["content"]
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    fn = tc.get("function") or {}
                    slot["id"] = tc.get("id") or slot["id"]
                    slot["name"] = fn.get("name") or slot["name"]
                    slot["args"] += fn.get("arguments") or ""

        if not calls:
            return
        calls = [dict(c, id=c["id"] or f"call_{i}") for i, c in sorted(calls.items())]
        msgs.append({"role": "assistant", "content": text or None, "tool_calls": [
            {"id": c["id"], "type": "function",
             "function": {"name": c["name"], "arguments": c["args"] or "{}"}} for c in calls]})
        for c in calls:
            args = parse_args(c["args"])
            yield "tool", (c["name"], args)
            result = tools(c["name"], args)
            msgs.append({"role": "tool", "tool_call_id": c["id"], "content": result})


def post(path, body, probe=False):
    req = urllib.request.Request(settings.api_url + path, json.dumps(body).encode(), headers={
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {settings.api_key}",
    })
    try:
        # на xhigh модель может долго думать молча, поэтому таймаут большой
        resp = urllib.request.urlopen(req, timeout=900)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        if probe and e.code in (400, 404, 422):
            raise ToolsRejected(error_text(text)) from None
        raise LLMError(error_text(text), e.code) from None
    except OSError as e:
        raise LLMError(f"нет связи с OpenCode: {e}") from None

    with resp:
        for _, data in sse(resp):
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except ValueError:
                continue


def responses_input(messages):
    items = []
    for m in messages:
        if m["role"] == "assistant":
            items.append({"role": "assistant",
                          "content": [{"type": "output_text", "text": m["text"]}]})
            continue
        content = [{"type": "input_text", "text": m["text"] or "…"}]
        for f in m.get("files", []):
            if f["mime"].startswith("image/"):
                content.append({"type": "input_image", "image_url": data_url(f)})
            else:
                content.append({"type": "input_file", "filename": f["name"],
                                "file_data": data_url(f)})
        items.append({"role": "user", "content": content})
    return items


def chat_messages(system, messages):
    msgs = [{"role": "system", "content": system}]
    for m in messages:
        images = [f for f in m.get("files", []) if f["mime"].startswith("image/")]
        if m["role"] == "assistant" or not images:
            msgs.append({"role": m["role"], "content": m["text"] or "…"})
            continue
        content = [{"type": "text", "text": m["text"] or "…"}]
        content += [{"type": "image_url", "image_url": {"url": data_url(f)}} for f in images]
        msgs.append({"role": "user", "content": content})
    return msgs


def sse(resp):
    name, data = None, []
    for raw in resp:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                yield name, "\n".join(data)
            name, data = None, []
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        yield name, "\n".join(data)


def parse_args(raw):
    try:
        args = json.loads(raw or "{}")
    except ValueError:
        return {}
    return args if isinstance(args, dict) else {}


def data_url(f):
    return f"data:{f['mime']};base64," + base64.b64encode(f["data"]).decode()


def error_text(body):
    try:
        err = json.loads(body).get("error") or {}
        return err.get("message") if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        return body[:300]


def human_error(e):
    # что показать пользователю вместо сырой ошибки
    code, text = getattr(e, "code", None), str(e)
    if "free tier" in text.lower():
        return "OpenCode не пускает к бесплатной модели без ключа — проверь OPENCODE_API_KEY"
    if code in (401, 403):
        return "OpenCode не принял ключ — проверь OPENCODE_API_KEY"
    if code == 402:
        return "на счету OpenCode не хватает денег для этой модели"
    if code == 429:
        return "модель сейчас перегружена или упёрлись в лимит, попробуй через минуту"
    if code and code >= 500:
        return "у OpenCode сбой, попробуй ещё раз чуть позже"
    return text
