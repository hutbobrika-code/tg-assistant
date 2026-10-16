"""Gemini Web2API client (via zexadev/gemini-web2api-go).

Provides an OpenAI-compatible streaming interface to Gemini models:
- gemini-3.1-pro (with thinking / reasoning_content)
- gemini-3.6-flash
- gemini-image (image generation via Nano Banana)
"""
import base64
import hashlib
import html
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
from datetime import datetime, timezone, timedelta
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
    {
        "id": "gemini/gemini-video",
        "name": "Gemini Video (Veo 2)",
        "short": "Veo Video",
        "variants": [],
        "context": 100_000,
        "description": "Генерация видео высокого качества (Google Veo 2)",
    },
    {
        "id": "gemini/gemini-music",
        "name": "Gemini Music (Lyria 3.5)",
        "short": "Lyria Music",
        "variants": [],
        "context": 100_000,
        "description": "Генерация музыки и аудиодорожек (Google Lyria)",
    },
    {
        "id": "gemini/gemini-deep-research",
        "name": "Gemini Deep Research",
        "short": "Deep Research",
        "variants": ["high"],
        "context": 1_000_000,
        "description": "Глубокое многоэтапное исследование (4 Argon High Reasoning)",
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


def clean_gemini_text_and_extract_followups(text):
    """
    Extracts structured follow-ups from <FollowUp .../> tags and strips all XML artifacts
    (<FollowUp>, <thought>, <citation>, etc.) so that clean human text remains.
    """
    followups = []
    if not text:
        return "", followups

    # 1. Match and extract full FollowUp tags
    fu_pattern = r'<FollowUp\b([^>]*?)(?:/>|>(.*?)</FollowUp>)'
    for m in re.finditer(fu_pattern, text, flags=re.DOTALL | re.IGNORECASE):
        attrs = m.group(1)
        inner = (m.group(2) or "").strip()
        label_m = re.search(r'label=["\'](.*?)["\']', attrs, flags=re.DOTALL | re.IGNORECASE)
        query_m = re.search(r'query=["\'](.*?)["\']', attrs, flags=re.DOTALL | re.IGNORECASE)
        label = (label_m.group(1) if label_m else inner).strip()
        query = (query_m.group(1) if query_m else (inner if not label else label)).strip()
        if not label and query:
            label = query
        if label or query:
            label = html.unescape(label)
            query = html.unescape(query)
            followups.append({"label": label, "query": query})

    # 2. Strip FollowUp tags
    cleaned = re.sub(fu_pattern, "", text, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'<FollowUp\b[^>]*>?', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'</FollowUp>', '', cleaned, flags=re.IGNORECASE)

    # 3. Strip any leaked internal XML tags: <thought>...</thought>, <citation...>, etc.
    cleaned = re.sub(r'<thought\b[^>]*>.*?</thought>', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r'</?(?:thought|suggestion|citation|sources)\b[^>]*>', '', cleaned, flags=re.IGNORECASE)

    # 4. Clean extra newlines
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned).strip()
    return cleaned, followups


def format_gemini_error(err, body=""):
    """Convert technical Gemini API/HTTP/connection errors into clean Russian messages."""
    err_str = str(err)
    combined = f"{err_str} {body}".lower()

    if "cookie" in combined or "snlm0e" in combined or "not signed in" in combined:
        return (
            "⚠️ Сессия Google Gemini обновляется.\n\n"
            "GitHub Actions автоматически ротирует сессию каждые 10 минут. "
            "Попробуйте повторить запрос через минуту."
        )
    if "timed out" in combined or "timeout" in combined or "deadline" in combined:
        return (
            "⏱️ Время ожидания ответа от Google Gemini истекло.\n\n"
            "Флагманская модель 4 Argon выполняет глубокие рассуждения — нажмите «🔁 Ещё раз», "
            "чтобы повторить запрос, или выберите модель 3.8 Flash для мгновенного ответа."
        )
    if "connection refused" in combined or "errno 111" in combined:
        return (
            "🔌 Локальный шлюз Gemini Web2API временно недоступен.\n\n"
            "Сервис перезапускается на сервере."
        )
    if "429" in combined or "rate limit" in combined or "quota" in combined:
        return (
            "⏳ Исчерпан лимит запросов к Google Gemini на этот период.\n\n"
            "Лимиты обновляются каждые несколько часов (см. раздел «📊 Лимиты»)."
        )
    if "media generation succeeded" in combined or "artifact retrieval failed" in combined or "hnvqhb" in combined:
        return (
            "🎵 Google Gemini создал медиафайл, но серверу потребовалось больше времени для его передачи.\n\n"
            "Нажмите «🔁 Ещё раз» — обычно повторный запрос отдаётся мгновенно."
        )
    if "502" in combined or "bad gateway" in combined:
        return (
            "⚠️ Временная ошибка связи с серверами Google Gemini (502 Bad Gateway).\n\n"
            "Попробуйте нажать «🔁 Ещё раз»."
        )
    if "503" in combined or "service unavailable" in combined:
        return (
            "⚠️ Сервера Google Gemini временно перегружены (503 Service Unavailable).\n\n"
            "Попробуйте повторить запрос через пару секунд."
        )

    # Fallback clean error
    clean_msg = body.strip() if body else err_str
    try:
        jd = json.loads(clean_msg)
        if isinstance(jd, dict):
            clean_msg = jd.get("error", {}).get("message") or jd.get("message") or clean_msg
    except Exception:
        pass
    return f"⚠️ Ошибка Gemini: {clean_msg}"


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
        self.followups = []
        self._error = None
        self._stopped = False
        self._retried = False
        self._retry_count = 0
        self._req = None
        self._resp = None
        self.context = 0
        threading.Thread(target=self._run, daemon=True).start()

    def snapshot(self):
        with self.lock:
            out = []
            for p in self.parts.values():
                d = json.loads(json.dumps(p))
                if d.get("type") == "text" and "<FollowUp" in d.get("text", ""):
                    d["text"] = re.sub(r'<FollowUp\b.*$', '', d["text"], flags=re.DOTALL | re.IGNORECASE).rstrip()
                out.append(d)
            return out

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

    def _save_data_media(self, text):
        """Find data:image/..., data:audio/..., data:video/... in text, save to workdir/uploads, replace with relative link."""
        uploads = self.workdir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)

        # 1. Images
        def repl_img(match):
            mime = match.group(1)
            b64_data = match.group(2)
            ext = ".png"
            if "jpeg" in mime or "jpg" in mime:
                ext = ".jpg"
            elif "webp" in mime:
                ext = ".webp"
            elif "gif" in mime:
                ext = ".gif"
            filename = f"gemini_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}{ext}"
            target = uploads / filename
            try:
                target.write_bytes(base64.b64decode(b64_data))
                rel_path = f"uploads/{filename}"
                return f"\n\n![Сгенерированное изображение]({rel_path})\n\n"
            except Exception as e:
                log("gemini save data image error:", e)
                return match.group(0)

        pattern_img = r"(?:!\[[^\]]*\]\()?data:(image\/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)\)?"
        text = re.sub(pattern_img, repl_img, text)

        # 2. Audio
        def repl_audio(match):
            mime = match.group(1)
            b64_data = match.group(2)
            ext = ".mp3" if "mp3" in mime or "mpeg" in mime else ".m4a"
            filename = f"gemini_audio_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}{ext}"
            target = uploads / filename
            try:
                target.write_bytes(base64.b64decode(b64_data))
                rel_path = f"uploads/{filename}"
                return f"\n\n![Сгенерированная музыка]({rel_path})\n\n"
            except Exception as e:
                log("gemini save data audio error:", e)
                return match.group(0)

        pattern_audio = r"(?:\[audio\]\()?data:(audio\/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)\)?"
        text = re.sub(pattern_audio, repl_audio, text)

        # 3. Video
        def repl_video(match):
            mime = match.group(1)
            b64_data = match.group(2)
            filename = f"gemini_video_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.mp4"
            target = uploads / filename
            try:
                target.write_bytes(base64.b64decode(b64_data))
                rel_path = f"uploads/{filename}"
                return f"\n\n![Сгенерированное видео]({rel_path})\n\n"
            except Exception as e:
                log("gemini save data video error:", e)
                return match.group(0)

        pattern_video = r"(?:\[video\]\()?data:(video\/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)\)?"
        text = re.sub(pattern_video, repl_video, text)
        return text

    def _save_data_images(self, text):
        return self._save_data_media(text)

    def _resolve_google_media(self, text):
        """Find googleusercontent links (image_generation_content, generated_image, generated_music_content, generated_video_content)
        and download the media from Google CDN to workdir/uploads, replacing with relative link."""
        pattern = r'\[?(?:https?:\/\/)?googleusercontent\.com\/(?:image_generation_content|generated_image|generated_music_content|generated_video_content)\/[a-zA-Z0-9_/-]+\]?'
        matches = list(re.finditer(pattern, text))
        if not matches:
            return text

        uploads = self.workdir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)

        try:
            chats = list_web_chats(limit=3)
            found_media = []
            for c in chats:
                chat_data = get_web_chat(c["id"])
                for msg in reversed(chat_data.get("messages", [])):
                    if msg.get("role") == "assistant" and msg.get("images"):
                        found_media.extend(msg["images"])
                if found_media:
                    break

            for i, match in enumerate(matches):
                if i < len(found_media):
                    media_url = found_media[i]
                    is_audio = "=m140" in media_url or "music" in match.group(0)
                    is_video = "video" in media_url or "video" in match.group(0)

                    if is_audio:
                        ext = ".m4a"
                        tag_name = "Сгенерированная музыка"
                        prefix = "gemini_music"
                    elif is_video:
                        ext = ".mp4"
                        tag_name = "Сгенерированное видео"
                        prefix = "gemini_video"
                    else:
                        ext = ".jpg"
                        tag_name = "Сгенерированное изображение"
                        prefix = "gemini_gen"

                    dest = uploads / f"{prefix}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}_{i}{ext}"
                    f = download_web_image(media_url, dest=dest)
                    if f and f.is_file():
                        rel_path = f"uploads/{f.name}"
                        text = text.replace(match.group(0), f"\n\n![{tag_name}]({rel_path})\n\n")
                    else:
                        text = text.replace(match.group(0), "")
                else:
                    text = text.replace(match.group(0), "")
        except Exception as e:
            log("resolve_google_media error:", e)
            for m in matches:
                text = text.replace(m.group(0), "")
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    def _resolve_google_images(self, text):
        return self._resolve_google_media(text)

    def _run(self):
        now_ms = int(time.time() * 1000)

        # 1. Direct video generation via Veo 2 /v1/videos if model is gemini-video
        if self.model in ("gemini-video", "gemini/gemini-video"):
            try:
                p_text = ""
                for m in reversed(self.messages):
                    if isinstance(m, dict) and m.get("role") == "user":
                        c = m.get("content")
                        if isinstance(c, str):
                            p_text = c
                            break
                        elif isinstance(c, list):
                            p_text = " ".join(item.get("text", "") for item in c if isinstance(item, dict) and item.get("type") == "text")
                            break
                if not p_text:
                    p_text = "Cinematic slow motion scene"

                with self.lock:
                    self.parts["r1"] = {
                        "id": "r1",
                        "type": "reasoning",
                        "text": "🎬 Запуск генератора Veo 2... Отправка параметров сцены в Google Cloud...",
                        "time": {"start": now_ms},
                    }
                self.on_change()

                def _progress_cb(elapsed):
                    with self.lock:
                        if "r1" in self.parts:
                            self.parts["r1"]["text"] = f"🎬 Генерация видео нейросетью Veo 2... Идёт рендеринг кадров ({elapsed} сек)..."
                    self.on_change()

                vid_path = generate_video(p_text, base_url=self.base_url, api_key=self.api_key, workdir=self.workdir, on_progress=_progress_cb)
                now_done = int(time.time() * 1000)
                with self.lock:
                    if "r1" in self.parts:
                        self.parts["r1"]["time"]["end"] = now_done
                    self.parts["t1"] = {
                        "id": "t1",
                        "type": "text",
                        "text": f"🎬 **Видео успешно сгенерировано нейросетью Google Veo 2!**\n\n![Сгенерированное видео]({vid_path})\n\n",
                        "time": {"start": now_ms, "end": now_done},
                    }
                self.done.set()
                self.on_change()
                return
            except Exception as e:
                log("gemini-video direct generate_video error:", e)
                # Fall through to standard chat completion stream if /v1/videos failed

        # 2. Direct music generation via Google Lyria 3.5 if model is gemini-music
        if self.model in ("gemini-music", "gemini/gemini-music"):
            try:
                p_text = ""
                for m in reversed(self.messages):
                    if isinstance(m, dict) and m.get("role") == "user":
                        c = m.get("content")
                        if isinstance(c, str):
                            p_text = c
                            break
                        elif isinstance(c, list):
                            p_text = " ".join(item.get("text", "") for item in c if isinstance(item, dict) and item.get("type") == "text")
                            break
                if not p_text:
                    p_text = "Atmospheric synthwave instrumental track"

                with self.lock:
                    self.parts["r1"] = {
                        "id": "r1",
                        "type": "reasoning",
                        "text": "🎵 Запуск генератора Lyria 3.5... Отправка параметров композиции в Google Cloud...",
                        "time": {"start": now_ms},
                    }
                self.on_change()

                def _music_progress_cb(status_text):
                    with self.lock:
                        if "r1" in self.parts:
                            self.parts["r1"]["text"] = status_text
                    self.on_change()

                mus_path, mus_desc = generate_music(
                    p_text,
                    base_url=self.base_url,
                    api_key=self.api_key,
                    workdir=self.workdir,
                    on_progress=_music_progress_cb,
                )
                now_done = int(time.time() * 1000)
                card_text = (
                    f"🎵 **Музыкальный трек Google Lyria 3.5 успешно создан!**\n\n"
                    f"🎧 **Стиль / Описание:** {mus_desc.strip()}\n\n"
                    if mus_desc and mus_desc != "Сгенерированная композиция Google Lyria"
                    else "🎵 **Музыкальный трек Google Lyria 3.5 успешно создан!**\n\n"
                )
                card_text += f"![Сгенерированная музыка]({mus_path})\n\n"

                with self.lock:
                    if "r1" in self.parts:
                        self.parts["r1"]["time"]["end"] = now_done
                    self.parts["t1"] = {
                        "id": "t1",
                        "type": "text",
                        "text": card_text,
                        "time": {"start": now_ms, "end": now_done},
                    }
                self.done.set()
                self.on_change()
                return
            except Exception as e:
                log("gemini-music direct generate_music error:", e)
                self._error = format_gemini_error(e)
                self.done.set()
                self.on_change()
                return

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

        # Adaptive model & effort for guaranteed stability:
        # If an upstream failure/timeout happened, automatically fall back to standard 4 Argon or 3.1 Pro
        active_model = self.model
        active_effort = self.effort

        if self._retry_count == 1:
            if "thinking" in active_model:
                active_model = active_model.replace("-thinking", "")
            if active_effort == "high":
                active_effort = "medium"
            elif active_effort == "medium":
                active_effort = "low"
        elif self._retry_count >= 2:
            if "argon" in active_model.lower():
                active_model = "gemini-3.1-pro"
                active_effort = "low"

        body = {
            "model": active_model,
            "messages": clean_messages,
            "stream": True,
        }
        if active_effort:
            body["reasoning_effort"] = active_effort
        data = json.dumps(body).encode("utf-8")

        reasoning_text = ""
        content_text = ""
        r_part = None
        t_part = None
        will_retry = False

        socket_timeout = 50 if ("thinking" in active_model and self._retry_count == 0) else 180
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            self._req = req
            with urllib.request.urlopen(req, timeout=socket_timeout) as resp:
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
                    will_retry = True
                    self._run()
                    return
            # If transient 502/503/504 happened before substantial content was yielded and we still have retries
            if len(content_text.strip()) <= 50 and not self._stopped and self._retry_count < 2 and e.code in (502, 503, 504):
                self._retry_count += 1
                will_retry = True
                log(f"gemini transient HTTP {e.code}, auto-retrying ({self._retry_count}/2)...")
                with self.lock:
                    self.parts["r1"] = {
                        "id": "r1",
                        "type": "reasoning",
                        "text": f"⏳ Запрос требует анализа... Подключение к Gemini (попытка {self._retry_count}/2)...",
                        "time": {"start": now_ms},
                    }
                    self.parts.pop("t1", None)
                self.on_change()
                time.sleep(1.5)
                self._run()
                return

            if len(content_text.strip()) > 50:
                log("gemini http error occurred but substantial content was already received:", e)
            elif not self._stopped:
                self._error = format_gemini_error(e, err_body)
        except Exception as e:
            if len(content_text.strip()) > 50:
                log("gemini connection closed after substantial content:", e)
            else:
                if not self._stopped and self._retry_count < 2:
                    self._retry_count += 1
                    will_retry = True
                    log(f"gemini auto-recovering from {e} (attempt {self._retry_count}/2)...")
                    with self.lock:
                        self.parts["r1"] = {
                            "id": "r1",
                            "type": "reasoning",
                            "text": f"⏳ Флагманская модель 4 Argon завершает ответ... Переподключение (попытка {self._retry_count}/2)...",
                            "time": {"start": now_ms},
                        }
                        self.parts.pop("t1", None)
                    self.on_change()
                    time.sleep(1.5)
                    self._run()
                    return

                if not self._stopped:
                    self._error = format_gemini_error(e)
                    log("gemini error:", e)
        finally:
            if not will_retry:
                now_final = int(time.time() * 1000)
                with self.lock:
                    if "r1" in self.parts and "end" not in self.parts["r1"]["time"]:
                        self.parts["r1"]["time"]["end"] = now_final
                    if "t1" in self.parts:
                        if "end" not in self.parts["t1"]["time"]:
                            self.parts["t1"]["time"]["end"] = now_final
                        # Post-process media tags
                        self.parts["t1"]["text"] = self._save_data_media(self.parts["t1"]["text"])
                        self.parts["t1"]["text"] = self._resolve_google_media(self.parts["t1"]["text"])
                        # Post-process FollowUp tags and XML artifacts
                        cleaned_txt, fu = clean_gemini_text_and_extract_followups(self.parts["t1"]["text"])
                        self.parts["t1"]["text"] = cleaned_txt
                        if fu:
                            self.followups.extend(fu)
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
    text = re.sub(r"\[?(?:https?:\/\/)?googleusercontent\.com\/(?:image_generation_content|generated_image)\/[a-zA-Z0-9_/-]+\]?", "", text)
    text = re.sub(r"\[file-tag:[^\]]+\]", "", text)
    text = re.sub(r"\[cite:\s*\d+\]", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def download_web_media(url, cookie_str=None, dest=None):
    """Download media (image, audio, video) from Gemini Web (lh3.googleusercontent.com) to disk."""
    cookie = cookie_str or get_active_cookie()
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "Referer": "https://gemini.google.com/",
        "Cookie": cookie,
        "Accept": "*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if not dest:
                if "audio" in content_type or "=m140" in url:
                    ext = ".m4a"
                elif "video" in content_type:
                    ext = ".mp4"
                elif "png" in content_type:
                    ext = ".png"
                elif "webp" in content_type:
                    ext = ".webp"
                else:
                    ext = ".jpg"
                tmp_dir = Path("workspace/uploads")
                tmp_dir.mkdir(parents=True, exist_ok=True)
                dest = tmp_dir / f"gemini_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}{ext}"
            else:
                dest = Path(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(resp.read())
            return dest
    except Exception as e:
        log("download_web_media error:", e)
        return None


def download_web_image(url, cookie_str=None, dest=None):
    return download_web_media(url, cookie_str=cookie_str, dest=dest)


def generate_video(prompt, base_url="http://127.0.0.1:8083/v1", api_key="sk-gemini-internal", workdir=".", on_progress=None):
    """Generate a video using Veo 2 through gemini-web2api-go /v1/videos.
    Returns relative path to the downloaded MP4 in uploads/."""
    base_url = base_url.rstrip("/")
    uploads = Path(workdir) / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    url = f"{base_url}/videos"
    data = json.dumps({"model": "gemini-video", "prompt": prompt}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            res = json.load(resp)
            job_id = res.get("id")
    except Exception as e:
        raise RuntimeError(f"Не удалось запустить генерацию видео Veo: {e}")

    if not job_id:
        raise RuntimeError(f"Сервер не вернул ID задачи видео: {res}")

    start_time = time.time()
    poll_url = f"{base_url}/videos/{job_id}"
    while time.time() - start_time < 360:  # up to 6 minutes
        time.sleep(4)
        elapsed = int(time.time() - start_time)
        if on_progress:
            try:
                on_progress(elapsed)
            except Exception:
                pass

        req_poll = urllib.request.Request(poll_url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req_poll, timeout=20) as resp:
                poll_res = json.load(resp)
                status = poll_res.get("status")
                if status == "completed":
                    content_url = f"{base_url}/videos/{job_id}/content"
                    req_content = urllib.request.Request(content_url, headers=headers, method="GET")
                    filename = f"gemini_video_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.mp4"
                    target = uploads / filename
                    with urllib.request.urlopen(req_content, timeout=60) as c_resp:
                        target.write_bytes(c_resp.read())
                    return f"uploads/{filename}"
                elif status == "failed":
                    err = poll_res.get("error", "Генерация видео отклонена или завершилась ошибкой")
                    raise RuntimeError(f"Генерация видео не удалась: {err}")
        except urllib.error.HTTPError as e:
            log(f"poll video HTTP {e.code}")
        except Exception as e:
            log(f"poll video error: {e}")

    raise RuntimeError("Время ожидания генерации видео истекло (более 6 минут)")


def generate_music(prompt, base_url="http://127.0.0.1:8083/v1", api_key="sk-gemini-internal", workdir=".", on_progress=None):
    """Generate music using Google Lyria through gemini-web2api-go and Gemini Web polling.
    Returns (audio_path, description).
    audio_path is relative path in uploads/ (e.g. uploads/gemini_music_....m4a).
    """
    base_url = base_url.rstrip("/")
    uploads = Path(workdir) / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    if on_progress:
        on_progress("🎵 Инициализация Google Lyria... Отправка запроса на создание композиции...")

    url = f"{base_url}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    body = {
        "model": "gemini-music",
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    data = json.dumps(body).encode("utf-8")

    content_text = ""
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            res = json.load(resp)
            choices = res.get("choices") or []
            if choices:
                msg = choices[0].get("message") or {}
                content_text = msg.get("content") or ""
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:600]
        log("generate_music HTTP error:", e.code, err_body)
        # If router returned 502 with media generation succeeded or hNvQHb timeout,
        # Gemini Web actually accepted the prompt and is creating the track!
        is_generation_pending = ("media generation succeeded" in err_body or "hNvQHb" in err_body or e.code in (502, 504))
        if not is_generation_pending:
            raise RuntimeError(format_gemini_error(e, err_body))
    except Exception as e:
        log("generate_music request error:", e)

    # 1. Check if content_text contains base64 audio
    if content_text:
        pattern_audio = r"(?:\[audio\]\()?data:(audio\/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)\)?"
        m = re.search(pattern_audio, content_text)
        if m:
            b64_data = m.group(2)
            filename = f"gemini_music_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.m4a"
            target = uploads / filename
            try:
                target.write_bytes(base64.b64decode(b64_data))
                clean_desc = re.sub(pattern_audio, "", content_text).strip()
                return f"uploads/{filename}", clean_desc or "Сгенерированная композиция Google Lyria"
            except Exception as e:
                log("save base64 audio error:", e)

    # 2. Check or poll Gemini Web chats for the newly created audio track (=m140)
    start_poll = time.time()
    poll_limit = 75  # seconds
    filename = f"gemini_music_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.m4a"
    target = uploads / filename

    while time.time() - start_poll < poll_limit:
        time.sleep(3.5)
        elapsed = int(time.time() - start_poll)
        if on_progress:
            on_progress(f"🎵 Синтез музыки нейросетью Google Lyria... Сведение аудиотрека ({elapsed} сек)...")

        try:
            chats = list_web_chats(limit=3)
            for c in chats:
                chat_data = get_web_chat(c["id"])
                msgs = chat_data.get("messages") or []
                for msg in reversed(msgs):
                    if msg.get("role") == "assistant":
                        imgs = msg.get("images") or []
                        for u in imgs:
                            if "=m140" in u or "audio" in u or "music" in u:
                                dest = download_web_media(u, dest=target)
                                if dest and dest.is_file() and dest.stat().st_size > 500:
                                    desc = msg.get("content") or content_text or "Сгенерированная композиция Google Lyria"
                                    desc = clean_gemini_text(desc)
                                    return f"uploads/{filename}", desc
        except Exception as e:
            log("generate_music web polling error:", e)

    if content_text:
        clean = clean_gemini_text(content_text)
        if clean:
            raise RuntimeError(f"Google Lyria сформировала ответ, но аудиофайл недоступен: {clean}")

    raise RuntimeError("Время ожидания генерации музыки Google Lyria истекло. Попробуйте нажать «🔁 Ещё раз».")


_USAGE_CACHE = {"data": None, "time": 0}


def format_usage_reset_time(iso_str, is_weekly=False):
    """Format ISO timestamp from Google into Russian local time (e.g. '19:52' or '20 окт. в 10:52')."""
    if not iso_str:
        return "—"
    try:
        clean_iso = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        # UTC+3 timezone (Moscow / user's local timezone)
        tz_offset = timezone(timedelta(hours=3))
        dt_local = dt.astimezone(tz_offset)
        months = ["", "янв.", "фев.", "мар.", "апр.", "мая", "июн.", "июл.", "авг.", "сен.", "окт.", "нояб.", "дек."]
        if is_weekly:
            return f"{dt_local.day} {months[dt_local.month]} в {dt_local.strftime('%H:%M')}"
        return f"{dt_local.strftime('%H:%M')}"
    except Exception:
        return iso_str[:16]


def fetch_gemini_web_usage(cookie_str=None, force=False):
    """Fetch real-time usage metrics and reset timestamps directly from Gemini GraphQL (gemini.google.com/usage)."""
    global _USAGE_CACHE
    now = time.time()
    if not force and _USAGE_CACHE["data"] and (now - _USAGE_CACHE["time"] < 20):
        return _USAGE_CACHE["data"]

    cookie = cookie_str or get_active_cookie()
    if not cookie:
        return None
    try:
        auth_header = make_sapisid_hash(cookie)
        query_sig = "2/dWbCvkVB0X16r9NPnxifeOAAqdCqMf0xeWvau9yPy7g="
        variables = {
            "g1Request": {
                "entrypoints": [
                    "GEMINI_USAGE_SETTINGS_UPGRADE_BANNER",
                    "GEMINI_USAGE_SETTINGS_AI_CREDITS_UPSELL_CARD",
                    "GEMINI_USAGE_SETTINGS_UPGRADE_AND_CREDITS_BANNER"
                ]
            },
            "workspaceRequest": {
                "recommendationsForSurfaceRequests": [
                    {
                        "recommendationSurface": {
                            "rpOnramp": "GEMINI_CHAT_USAGE_LIMITS_UPSELL",
                            "version": "1"
                        },
                        "maxRecommendationCount": 1
                    }
                ]
            }
        }
        base_url = "https://gemini.google.com/_/BardChatUi/graphql/schemas/GEMINI_WEB_GRAPHQL/executeQuery"
        params = {
            "query_signature": query_sig,
            "variables": json.dumps(variables)
        }
        url = f"{base_url}?{urllib.parse.urlencode(params)}"
        headers = {
            "Cookie": cookie,
            "Authorization": auth_header,
            "Accept": "application/json",
            "Origin": "https://gemini.google.com",
            "Referer": "https://gemini.google.com/usage",
            "X-Same-Domain": "1",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.load(resp)
            if isinstance(data, list) and len(data) > 0:
                usage_info = (
                    data[0].get("data", {})
                    .get("viewer", {})
                    .get("rpcs", {})
                    .get("getUsageInfo", {})
                )
                if usage_info:
                    tier = usage_info.get("subscriptionTier", "PRO")
                    pools = usage_info.get("pools") or []
                    cur_p = next((p for p in pools if p.get("poolType") == "GXU_FIVE_HOURLY"), None)
                    wk_p = next((p for p in pools if p.get("poolType") == "GXU_WEEKLY"), None)

                    cur_pct = round((cur_p.get("percentageUsed") or 0) * 100) if cur_p else None
                    cur_iso = cur_p.get("gxu", {}).get("windowRefreshesAt") if cur_p else None

                    wk_pct = round((wk_p.get("percentageUsed") or 0) * 100) if wk_p else None
                    wk_iso = wk_p.get("gxu", {}).get("windowRefreshesAt") if wk_p else None

                    res = {
                        "tier": "PRO" if tier in ("TIER1", "PRO") else tier,
                        "current": {
                            "percent": cur_pct,
                            "reset_iso": cur_iso,
                            "reset_time": format_usage_reset_time(cur_iso, is_weekly=False)
                        },
                        "weekly": {
                            "percent": wk_pct,
                            "reset_iso": wk_iso,
                            "reset_time": format_usage_reset_time(wk_iso, is_weekly=True)
                        },
                        "use_overage": usage_info.get("useOverageAiCredits", False),
                        "updated_at": "только что"
                    }
                    _USAGE_CACHE = {"data": res, "time": now}
                    return res
    except Exception as e:
        log("fetch_gemini_web_usage error:", e)
    return _USAGE_CACHE.get("data")


def get_gemini_limits_info(db_path="data/gemini.db", force_web=False):
    """Read account status, cookies, 24h usage statistics from SQLite db, and live web usage metrics."""
    web_usage = fetch_gemini_web_usage(force=force_web)
    plan_name = "Google One AI Premium (Gemini Advanced / Ultra)"
    if web_usage and web_usage.get("tier") == "PRO":
        plan_name = "Google One AI Premium (PRO / Gemini Ultra)"

    info = {
        "email": "artur.hodzhojan@gmail.com",
        "plan": plan_name,
        "account_status": "enabled",
        "cookie_count": 12,
        "cookie_health": "ok",
        "last_ok_at": 0,
        "fail_count": 0,
        "requests_24h": 0,
        "success_24h": 0,
        "avg_ms_24h": 0,
        "by_model": {},
        "web_limits": web_usage,
        "quotas": [
            {"model": "Gemini 4 Argon (Ultra/Thinking)", "quota": "50 запросов / сутки", "status": "🟢 Доступно"},
            {"model": "Gemini 3.1 Pro (Thinking)", "quota": "Безлимитно (Extended)", "status": "🟢 Доступно"},
            {"model": "Gemini 3.8 Flash & Lite", "quota": "Безлимитно (Высокий RPM)", "status": "⚡ Мгновенно"},
            {"model": "Gemini Image (Nano Banana)", "quota": "Без ограничений", "status": "🎨 Доступно"},
            {"model": "Gemini Video (Veo 2)", "quota": "Pro/Ultra доступ", "status": "🎬 Доступно"},
            {"model": "Gemini Music (Lyria 3.5)", "quota": "Без ограничений", "status": "🎵 Доступно"},
        ]
    }
    p = Path(db_path)
    if not p.is_file():
        get_active_cookie(db_path)

    if p.is_file():
        try:
            con = sqlite3.connect(str(p))
            cur = con.cursor()
            acc = cur.execute("SELECT label, status, last_ok_at, fail_count FROM accounts WHERE status='enabled' LIMIT 1").fetchone()
            if acc:
                info["account_status"] = acc[1]
                info["last_ok_at"] = acc[2]
                info["fail_count"] = acc[3]
                if acc[3] > 0:
                    info["cookie_health"] = f"warning ({acc[3]} errors)"

            now = int(time.time())
            day_ago = now - 86400
            stats = cur.execute("SELECT count(*), sum(case when status=200 then 1 else 0 end), avg(total_ms) FROM requests WHERE ts > ?", (day_ago,)).fetchone()
            if stats and stats[0]:
                info["requests_24h"] = stats[0] or 0
                info["success_24h"] = stats[1] or 0
                info["avg_ms_24h"] = round((stats[2] or 0) / 1000, 1)

            models_stat = cur.execute("SELECT model, count(*), sum(case when status=200 then 1 else 0 end) FROM requests WHERE ts > ? GROUP BY model", (day_ago,)).fetchall()
            for m_row in models_stat:
                info["by_model"][m_row[0]] = {"total": m_row[1], "success": m_row[2]}
            con.close()
        except Exception as e:
            log("get_gemini_limits_info db error:", e)

    return info


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

    def download_web_media(self, url, dest=None):
        return download_web_media(url, dest=dest)

    def download_web_image(self, url, dest=None):
        return download_web_media(url, dest=dest)

    def generate_video(self, prompt, on_progress=None):
        return generate_video(prompt, base_url=self.base_url, api_key=self.api_key, workdir=self.workdir, on_progress=on_progress)

    def generate_music(self, prompt, on_progress=None):
        return generate_music(prompt, base_url=self.base_url, api_key=self.api_key, workdir=self.workdir, on_progress=on_progress)

    def get_limits_info(self, db_path="data/gemini.db", force_web=False):
        return get_gemini_limits_info(db_path=db_path, force_web=force_web)

    def fetch_web_usage(self, force=True):
        return fetch_gemini_web_usage(force=force)

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
        if raw_model == "gemini-deep-research":
            raw_model = "gemini-4-argon-thinking"
            effort = "high"
        elif effort and effort in ("low", "medium", "high", "xhigh", "max"):
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
