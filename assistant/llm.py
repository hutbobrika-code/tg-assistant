import base64
import json
import urllib.error
import urllib.request

from .config import settings


class LLMError(Exception):
    pass


def stream(model, system, messages, effort=None, stop=None):
    # отдаёт события ("thinking", кусок) и ("text", кусок) по мере генерации
    if model.api == "responses":
        path, parse = "/responses", parse_responses
        body = responses_body(model, system, messages, effort)
    else:
        path, parse = "/chat/completions", parse_chat
        body = chat_body(model, system, messages, effort)

    req = urllib.request.Request(settings.api_url + path, json.dumps(body).encode(), headers={
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {settings.api_key}",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=300)
    except urllib.error.HTTPError as e:
        raise LLMError(error_text(e.read().decode("utf-8", "replace"), e.code)) from None
    except OSError as e:
        raise LLMError(f"нет связи с OpenCode: {e}") from None

    with resp:
        for name, data in sse(resp):
            if stop is not None and stop.is_set():
                return
            yield from parse(name, data)


def responses_body(model, system, messages, effort):
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

    body = {"model": model.id, "instructions": system, "input": items,
            "stream": True, "store": False}
    if effort:
        body["reasoning"] = {"effort": effort, "summary": "auto"}
    return body


def chat_body(model, system, messages, effort):
    msgs = [{"role": "system", "content": system}]
    for m in messages:
        images = [f for f in m.get("files", []) if f["mime"].startswith("image/")]
        if m["role"] == "assistant" or not images:
            msgs.append({"role": m["role"], "content": m["text"] or "…"})
            continue
        content = [{"type": "text", "text": m["text"] or "…"}]
        content += [{"type": "image_url", "image_url": {"url": data_url(f)}} for f in images]
        msgs.append({"role": "user", "content": content})

    body = {"model": model.id, "messages": msgs, "stream": True}
    if effort:
        body["reasoning_effort"] = effort
    return body


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


def parse_responses(name, data):
    if data == "[DONE]":
        return
    try:
        ev = json.loads(data)
    except ValueError:
        return
    kind = ev.get("type") or name
    if kind == "response.output_text.delta":
        yield "text", ev.get("delta", "")
    elif kind in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
        yield "thinking", ev.get("delta", "")
    elif kind == "response.reasoning_summary_part.added":
        yield "thinking", "\n\n"
    elif kind in ("response.failed", "error"):
        err = ev.get("error") or (ev.get("response") or {}).get("error") or ev
        raise LLMError(error_text(json.dumps({"error": err})))


def parse_chat(name, data):
    if data == "[DONE]":
        return
    try:
        ev = json.loads(data)
    except ValueError:
        return
    if ev.get("error"):
        raise LLMError(error_text(data))
    for choice in ev.get("choices", []):
        delta = choice.get("delta") or {}
        thinking = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(thinking, str) and thinking:
            yield "thinking", thinking
        if delta.get("content"):
            yield "text", delta["content"]


def data_url(f):
    return f"data:{f['mime']};base64," + base64.b64encode(f["data"]).decode()


def error_text(body, code=None):
    try:
        err = json.loads(body).get("error") or {}
        msg = err.get("message") if isinstance(err, dict) else str(err)
    except (ValueError, AttributeError):
        msg = body[:300]
    return f"{code}: {msg}" if code else str(msg)
