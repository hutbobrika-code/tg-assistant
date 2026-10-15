"""Gemini Web2API client (via zexadev/gemini-web2api-go).

Provides an OpenAI-compatible streaming interface to Gemini models:
- gemini-3.1-pro (with thinking / reasoning_content)
- gemini-3.6-flash
- gemini-image (image generation via Nano Banana)
"""
import base64
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from pathlib import Path

from tg import log

IMAGE_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}

GEMINI_EFFORTS = ["low", "medium", "high"]

GEMINI_MODELS = [
    {
        "id": "gemini/gemini-4-argon",
        "name": "Gemini 4 Argon",
        "short": "4 Argon",
        "variants": GEMINI_EFFORTS,
        "context": 1_000_000,
        "description": "Сложные задачи (флагман Google Ultra/Advanced)",
    },
    {
        "id": "gemini/gemini-3.1-pro",
        "name": "Gemini 3.1 Pro",
        "short": "3.1 Pro",
        "variants": GEMINI_EFFORTS,
        "context": 1_000_000,
        "description": "Расширенные возможности рассуждения",
    },
    {
        "id": "gemini/gemini-3.8-flash",
        "name": "Gemini 3.8 Flash",
        "short": "3.8 Flash",
        "variants": GEMINI_EFFORTS,
        "context": 1_000_000,
        "description": "All-around help (новейший быстрый Flash)",
    },
    {
        "id": "gemini/gemini-3.5-flash-lite",
        "name": "Gemini 3.5 Flash-Lite",
        "short": "3.5 Flash-Lite",
        "variants": GEMINI_EFFORTS,
        "context": 1_000_000,
        "description": "Самые быстрые ответы",
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


def reload_router_cookies(base_url, state_key=None):
    """Hot-reload fresh cookies from state/gemini.db.enc into gemini-web2api-go router."""
    try:
        subprocess.run(["git", "pull", "--rebase"], timeout=20, capture_output=True)
    except Exception as e:
        log("git pull during cookie reload failed:", e)

    enc_file = Path("state/gemini.db.enc")
    if not enc_file.is_file():
        return False

    key = state_key or os.environ.get("STATE_KEY")
    if not key:
        return False

    tmp_db = Path(f"/tmp/hotreload_gemini_{int(time.time() * 1000)}.db")
    try:
        cmd = ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt",
               "-pass", "env:STATE_KEY", "-in", str(enc_file), "-out", str(tmp_db)]
        res = subprocess.run(cmd, env=dict(os.environ, STATE_KEY=key), capture_output=True)
        if res.returncode != 0:
            log("decrypt gemini.db for reload failed:", res.stderr)
            return False

        con = sqlite3.connect(str(tmp_db))
        row = con.cursor().execute("SELECT cookie FROM accounts WHERE status='enabled' ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        if not row or not row[0]:
            return False
        cookie_str = row[0]

        admin_base = re.sub(r"/v1/?$", "", base_url)
        add_url = f"{admin_base}/admin/api/cookies"
        data = json.dumps({"cookie": cookie_str}).encode("utf-8")
        req = urllib.request.Request(
            add_url,
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log("gemini router hot-reloaded fresh cookie:", resp.status)
            return True
    except Exception as e:
        log("reload_router_cookies error:", e)
        return False
    finally:
        if tmp_db.exists():
            try:
                tmp_db.unlink()
            except OSError:
                pass


class GeminiTurn:
    """One streaming request to Gemini Web2API (/v1/chat/completions)."""

    def __init__(self, base_url, api_key, model, messages, workdir, on_change=None, effort=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.messages = messages
        self.workdir = Path(workdir)
        self.on_change = on_change or (lambda: None)
        self.effort = effort
        self.parts = OrderedDict()
        self.lock = threading.Lock()
        self.done = threading.Event()
        self._error = None
        self._stopped = False
        self._retried = False
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

        clean_messages = []
        for m in self.messages:
            if isinstance(m, dict):
                clean_messages.append({
                    "role": m.get("role", "user"),
                    "content": m.get("content", ""),
                })
            else:
                clean_messages.append(m)

        body = {
            "model": self.model,
            "messages": clean_messages,
            "stream": True,
        }
        if self.effort:
            body["reasoning_effort"] = self.effort
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
            err_body = e.read().decode("utf-8", errors="replace")[:600]
            log("gemini http error:", e.code, err_body)
            if not self._retried and e.code == 502 and ("cookie" in err_body or "SNlM0e" in err_body):
                self._retried = True
                log("gemini attempting hot-reload of fresh cookies from git/state...")
                if reload_router_cookies(self.base_url):
                    log("gemini retrying turn after hot-reload...")
                    self._run()
                    return
            if not self._stopped:
                if "cookie" in err_body or "SNlM0e" in err_body:
                    self._error = (
                        "⚠️ Сессия Google Gemini устарела. Откройте https://gemini.google.com в браузере Firefox "
                        "или запустите ~/sync_gemini.sh для синхронизации."
                    )
                else:
                    self._error = f"Gemini HTTP {e.code}: {err_body}"
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


_XSRF_CACHE = {}


def get_active_cookie(db_path="data/gemini.db"):
    """Read current active Google session cookie from data/gemini.db or decrypted state."""
    p = Path(db_path)
    enc_file = Path("state/gemini.db.enc")

    key = os.environ.get("STATE_KEY")
    if not key:
        key_file = Path(os.path.expanduser("~/.gemini_state_key"))
        if key_file.is_file():
            try:
                content = key_file.read_text().strip()
                if "STATE_KEY=" in content:
                    key = content.split("STATE_KEY=", 1)[1].strip()
                else:
                    key = content
            except Exception:
                pass

    if enc_file.is_file() and key:
        enc_mtime = enc_file.stat().st_mtime
        db_mtime = p.stat().st_mtime if p.is_file() else 0
        if enc_mtime > db_mtime or not p.is_file():
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                cmd = ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "100000", "-salt",
                       "-pass", f"pass:{key}", "-in", str(enc_file), "-out", str(p)]
                subprocess.run(cmd, capture_output=True)
            except Exception as e:
                log("auto-decrypt gemini.db error:", e)

    if p.is_file():
        try:
            con = sqlite3.connect(str(p))
            row = con.execute("SELECT cookie FROM accounts WHERE status='enabled' ORDER BY id DESC LIMIT 1").fetchone()
            con.close()
            if row and row[0]:
                return row[0]
        except Exception as e:
            log("get_active_cookie from db error:", e)

    return ""


def get_xsrf_token(cookie_str=None):
    """Retrieve or use cached SNlM0e token from gemini.google.com/app."""
    global _XSRF_CACHE
    cookie = cookie_str or get_active_cookie()
    if not cookie:
        raise RuntimeError("Нет сохранённых cookies для аккаунта Google Gemini")

    now = time.time()
    cached = _XSRF_CACHE.get(cookie[:20])
    if cached and (now - cached.get("time", 0)) < 900:  # 15 minutes TTL
        return cached["token"]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": cookie,
    }
    req = urllib.request.Request("https://gemini.google.com/app", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            m = re.search(r'\"SNlM0e\":\"([^\"]+)\"', html)
            if m:
                token = m.group(1)
                _XSRF_CACHE[cookie[:20]] = {"token": token, "time": now}
                return token
    except Exception as e:
        log("get_xsrf_token error:", e)

    if cached and cached.get("token"):
        return cached["token"]
    raise RuntimeError("Не удалось получить токен SNlM0e с gemini.google.com (возможно, сессия устарела)")


def make_sapisid_hash(cookie_str):
    m = re.search(r'(?:^|;\s*)SAPISID=([^;]+)', cookie_str)
    sapisid = m.group(1) if m else ""
    ts = int(time.time())
    sig = hashlib.sha1(f"{ts} {sapisid} https://gemini.google.com".encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{sig}"


def list_web_chats(cookie_str=None, limit=50):
    """Fetch list of user's web chats from gemini.google.com."""
    cookie = cookie_str or get_active_cookie()
    if not cookie:
        return []
    try:
        at = get_xsrf_token(cookie)
        auth_header = make_sapisid_hash(cookie)
        payload = json.dumps([[["MaZiqc", "[]", None, "generic"]]])
        form = urllib.parse.urlencode({"f.req": payload, "at": at}).encode("utf-8")
        req_id = int(time.time() * 1000) % 1000000
        url = f"https://gemini.google.com/_/BardChatUi/data/batchexecute?rpcids=MaZiqc&bl=boq_assistant-bard-web-server_20240901.00_p0&hl=ru&_reqid={req_id}&rt=c"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Cookie": cookie,
            "Authorization": auth_header,
            "X-Same-Domain": "1",
            "Origin": "https://gemini.google.com",
            "Referer": "https://gemini.google.com/app",
        }
        req = urllib.request.Request(url, data=form, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        out = []
        for line in raw.splitlines():
            line = line.strip()
            if "MaZiqc" in line and line.startswith("["):
                try:
                    for item in json.loads(line):
                        if isinstance(item, list) and len(item) >= 3 and item[1] == "MaZiqc":
                            inner = json.loads(item[2])
                            entries = inner[2] if len(inner) > 2 and isinstance(inner[2], list) else (inner[0] if isinstance(inner[0], list) else [])
                            for e in entries:
                                if isinstance(e, list) and len(e) >= 2 and e[0]:
                                    out.append({"id": e[0], "title": e[1] or "Без названия"})
                except Exception:
                    pass
        return out[:limit]
    except Exception as e:
        log("list_web_chats error:", e)
        return []


def _extract_gemini_images(data):
    urls = []
    seen = set()

    def _walk(obj):
        if isinstance(obj, str):
            if "lh3.googleusercontent.com/" in obj and obj.startswith("http"):
                if obj not in seen:
                    seen.add(obj)
                    urls.append(obj)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)

    _walk(data)
    return urls


def clean_gemini_text(text):
    if not text:
        return ""
    text = re.sub(r"```([a-zA-Z0-9_-]+)\?[^\n]*", r"```\1", text)
    text = re.sub(r"```\?[^\n]*", "```", text)
    text = re.sub(r"\[http://googleusercontent\.com/generated_image/\d+\]", "", text)
    text = re.sub(r"\[file-tag:[^\]]+\]", "", text)
    text = re.sub(r"\[cite:\s*\d+\]", "", text)
    return text.strip()


def download_web_image(url, cookie_str=None, dest=None):
    """Download an image from Gemini Web (lh3.googleusercontent.com) to disk."""
    cookie = cookie_str or get_active_cookie()
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Referer": "https://gemini.google.com/",
        "Cookie": cookie,
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            content_type = resp.headers.get("Content-Type", "")
            ext = ".png" if "png" in content_type else (".webp" if "webp" in content_type else ".jpg")
            if not dest:
                tmp_dir = Path("workspace/uploads")
                tmp_dir.mkdir(parents=True, exist_ok=True)
                dest = tmp_dir / f"gemini_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}{ext}"
            else:
                dest = Path(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.read())
            return dest
    except Exception as e:
        log("download_web_image error:", e)
        return None


def get_web_chat(conv_id, cookie_str=None):
    """Fetch all messages from a specific Gemini web chat."""
    cookie = cookie_str or get_active_cookie()
    if not cookie:
        return {"id": conv_id, "messages": []}
    try:
        at = get_xsrf_token(cookie)
        auth_header = make_sapisid_hash(cookie)
        inner_payload = json.dumps([conv_id, 30, None, 1, [0], [4], None, 1])
        payload = json.dumps([[["hNvQHb", inner_payload, None, "generic"]]])
        form = urllib.parse.urlencode({"f.req": payload, "at": at}).encode("utf-8")
        req_id = int(time.time() * 1000) % 1000000
        url = f"https://gemini.google.com/_/BardChatUi/data/batchexecute?rpcids=hNvQHb&bl=boq_assistant-bard-web-server_20240901.00_p0&hl=ru&_reqid={req_id}&rt=c"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Cookie": cookie,
            "Authorization": auth_header,
            "X-Same-Domain": "1",
            "Origin": "https://gemini.google.com",
            "Referer": "https://gemini.google.com/app",
        }
        req = urllib.request.Request(url, data=form, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        messages = []
        for line in raw.splitlines():
            line = line.strip()
            if "hNvQHb" in line and line.startswith("["):
                try:
                    for item in json.loads(line):
                        if isinstance(item, list) and len(item) >= 3 and item[1] == "hNvQHb":
                            inner = json.loads(item[2])
                            turns = inner[0] if isinstance(inner, list) and len(inner) > 0 and isinstance(inner[0], list) else []
                            for turn in turns:
                                u_text = ""
                                try:
                                    u_text = turn[2][0][0] or ""
                                except Exception:
                                    pass
                                u_imgs = _extract_gemini_images(turn[2]) if len(turn) > 2 else []
                                if u_text or u_imgs:
                                    msg = {"role": "user", "content": u_text}
                                    if u_imgs:
                                        msg["images"] = u_imgs
                                    messages.append(msg)

                                a_text = ""
                                try:
                                    cands = turn[3]
                                    if isinstance(cands, list) and len(cands) > 0 and isinstance(cands[0], list):
                                        cand0 = cands[0]
                                        if len(cand0) > 0 and isinstance(cand0[0], list) and len(cand0[0]) > 1:
                                            parts = cand0[0][1]
                                            if isinstance(parts, list):
                                                a_text = "".join(p for p in parts if isinstance(p, str))
                                except Exception:
                                    pass
                                a_imgs = [img for img in _extract_gemini_images(turn[3]) if img not in u_imgs] if len(turn) > 3 else []
                                a_text = clean_gemini_text(a_text)
                                if a_text or a_imgs:
                                    msg = {"role": "assistant", "content": a_text}
                                    if a_imgs:
                                        msg["images"] = a_imgs
                                    messages.append(msg)
                except Exception:
                    pass
        return {"id": conv_id, "messages": messages}
    except Exception as e:
        log("get_web_chat error:", e)
        return {"id": conv_id, "messages": []}


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

    def list_web_chats(self, limit=50):
        return list_web_chats(limit=limit)

    def get_web_chat(self, conv_id):
        return get_web_chat(conv_id)

    def download_web_image(self, url, dest=None):
        return download_web_image(url, dest=dest)

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
        if effort and effort in ("low", "medium", "high", "xhigh", "max"):
            thinking_model = f"{raw_model}-thinking"
            if not raw_model.endswith("-thinking") and thinking_model in (
                "gemini-4-argon-thinking",
                "gemini-3.1-pro-thinking",
                "gemini-3.8-flash-thinking",
                "gemini-3.5-flash-lite-thinking",
                "gemini-3.6-flash-thinking",
            ):
                raw_model = thinking_model
        return GeminiTurn(self.base_url, self.api_key, raw_model, messages, self.workdir, on_change, effort=effort)
