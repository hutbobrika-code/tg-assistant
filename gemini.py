"""Gemini Web2API client (via zexadev/gemini-web2api-go).

Provides an OpenAI-compatible streaming interface to Gemini models:
- gemini-3.1-pro (with thinking / reasoning_content)
- gemini-3.6-flash
- gemini-image (image generation via Nano Banana)
"""
import base64
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from pathlib import Path

from tg import log

IMAGE_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}

GEMINI_MODELS = [
    {
        "id": "gemini/gemini-3.1-pro",
        "name": "Gemini 3.1 Pro",
        "short": "Gemini 3.1 Pro",
        "variants": ["high"],
        "context": 1_000_000,
        "description": "Флагманская модель с рассуждениями (thinking)",
    },
    {
        "id": "gemini/gemini-3.6-flash",
        "name": "Gemini 3.6 Flash",
        "short": "Gemini 3.6 Flash",
        "variants": [],
        "context": 1_000_000,
        "description": "Быстрая универсальная модель",
    },
    {
        "id": "gemini/gemini-image",
        "name": "Gemini Image",
        "short": "Gemini Image",
        "variants": [],
        "context": 100_000,
        "description": "Генерация изображений (Nano Banana)",
    },
]


def extract_prompt_and_images(parts):
    """Extract text prompt and base64 encoded images from bot job parts."""
    texts = []
    images = []
    for p in parts:
        if p.get("type") == "text" and p.get("text"):
            texts.append(p["text"])
        elif p.get("type") == "file" and p.get("url", "").startswith("file://"):
            fpath = Path(p["url"][7:])
            mime = p.get("mime", "")
            if mime in IMAGE_MIME and fpath.is_file():
                try:
                    b64 = base64.b64encode(fpath.read_bytes()).decode("ascii")
                    images.append((mime, b64))
                except OSError as e:
                    log("gemini read image error:", e)
    prompt_text = "\n\n".join(texts).strip()
    return prompt_text, images


class GeminiTurn:
    """One streaming request to Gemini Web2API (/v1/chat/completions)."""

    def __init__(self, base_url, api_key, model, messages, workdir, on_change=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.messages = messages
        self.workdir = Path(workdir)
        self.on_change = on_change or (lambda: None)
        self.parts = OrderedDict()
        self.lock = threading.Lock()
        self.done = threading.Event()
        self._error = None
        self._stopped = False
        self._req = None
        self._resp = None
        self.context = 0
        threading.Thread(target=self._run, daemon=True).start()

    def snapshot(self):
        with self.lock:
            return [json.loads(json.dumps(p)) for p in self.parts.values()]

    def error(self):
        return self._error

    def stop(self):
        self._stopped = True
        try:
            if self._resp:
                self._resp.close()
        except Exception:
            pass
        self.done.set()
        self.on_change()

    def _save_data_images(self, text):
        """Find data:image/...;base64,... in text, save to workdir/uploads, replace with relative link."""
        uploads = self.workdir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)

        def repl(match):
            mime = match.group(1)
            b64_data = match.group(2)
            ext = ".png"
            if "jpeg" in mime or "jpg" in mime:
                ext = ".jpg"
            elif "webp" in mime:
                ext = ".webp"
            elif "gif" in mime:
                ext = ".gif"
            filename = f"gemini_{int(time.time() * 1000)}{ext}"
            target = uploads / filename
            try:
                target.write_bytes(base64.b64decode(b64_data))
                rel_path = f"uploads/{filename}"
                return f"![Сгенерированное изображение]({rel_path})"
            except Exception as e:
                log("gemini save data image error:", e)
                return match.group(0)

        pattern = r"!\[[^\]]*\]\(data:(image\/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)\)"
        return re.sub(pattern, repl, text)

    def _run(self):
        now_ms = int(time.time() * 1000)
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        body = {
            "model": self.model,
            "messages": self.messages,
            "stream": True,
        }
        data = json.dumps(body).encode("utf-8")

        reasoning_text = ""
        content_text = ""
        r_part = None
        t_part = None

        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            self._req = req
            with urllib.request.urlopen(req, timeout=120) as resp:
                self._resp = resp
                for raw_line in resp:
                    if self._stopped:
                        break
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data: "):
                        payload = line[6:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except ValueError:
                            continue

                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}

                        # 1. Reasoning / thinking delta
                        r_delta = delta.get("reasoning_content") or delta.get("reasoning")
                        if r_delta:
                            reasoning_text += r_delta
                            with self.lock:
                                if "r1" not in self.parts:
                                    r_part = {
                                        "id": "r1",
                                        "type": "reasoning",
                                        "text": reasoning_text,
                                        "time": {"start": now_ms},
                                    }
                                    self.parts["r1"] = r_part
                                else:
                                    self.parts["r1"]["text"] = reasoning_text
                            self.on_change()

                        # 2. Content delta
                        c_delta = delta.get("content")
                        if c_delta:
                            with self.lock:
                                # Close reasoning part time if switching to text
                                if "r1" in self.parts and "end" not in self.parts["r1"]["time"]:
                                    self.parts["r1"]["time"]["end"] = int(time.time() * 1000)

                                content_text += c_delta
                                if "t1" not in self.parts:
                                    t_part = {
                                        "id": "t1",
                                        "type": "text",
                                        "text": content_text,
                                        "time": {"start": int(time.time() * 1000)},
                                    }
                                    self.parts["t1"] = t_part
                                else:
                                    self.parts["t1"]["text"] = content_text
                            self.on_change()

        except urllib.error.HTTPError as e:
            if not self._stopped:
                err_body = e.read().decode("utf-8", errors="replace")[:600]
                self._error = f"Gemini HTTP {e.code}: {err_body}"
                log("gemini http error:", e.code, err_body)
        except Exception as e:
            if not self._stopped:
                self._error = f"Gemini connection error: {e}"
                log("gemini error:", e)
        finally:
            now_final = int(time.time() * 1000)
            with self.lock:
                if "r1" in self.parts and "end" not in self.parts["r1"]["time"]:
                    self.parts["r1"]["time"]["end"] = now_final
                if "t1" in self.parts:
                    if "end" not in self.parts["t1"]["time"]:
                        self.parts["t1"]["time"]["end"] = now_final
                    # Post-process image tags
                    self.parts["t1"]["text"] = self._save_data_images(self.parts["t1"]["text"])
            self.done.set()
            self.on_change()


class GeminiRouter:
    """Manages connection to the local gemini-web2api-go instance."""

    def __init__(self, base_url="http://127.0.0.1:8083/v1", api_key="sk-gemini-internal", workdir="."):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.workdir = Path(workdir)

    def available(self):
        """Check if the Gemini Web2API router is running and healthy."""
        try:
            req = urllib.request.Request(f"{self.base_url}/models")
            if self.api_key:
                req.add_header("Authorization", f"Bearer {self.api_key}")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                return resp.status == 200
        except Exception:
            return False

    def start(self, parts, model_id, effort=None, dialog=None, on_change=None):
        prompt_text, images = extract_prompt_and_images(parts)

        # Build message history
        messages = list(dialog or [])

        # Build current user message
        if images:
            content_items = []
            if prompt_text:
                content_items.append({"type": "text", "text": prompt_text})
            for mime, b64 in images:
                content_items.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"}
                })
            messages.append({"role": "user", "content": content_items})
        else:
            messages.append({"role": "user", "content": prompt_text or "…" + ""})

        # Model mapping: if model_id starts with gemini/, strip it
        raw_model = model_id.split("/", 1)[1] if "/" in model_id else model_id
        return GeminiTurn(self.base_url, self.api_key, raw_model, messages, self.workdir, on_change)
