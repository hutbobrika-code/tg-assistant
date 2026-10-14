"""Minimal Telegram Bot API client (stdlib only)."""
import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


class Telegram:
    def __init__(self, token):
        self.token = token
        self.api = f"https://api.telegram.org/bot{token}/"

    def __call__(self, method, _timeout=30, _retries=3, **params):
        data = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        for attempt in range(_retries):
            req = urllib.request.Request(self.api + method, data=data,
                                         headers={"Content-Type": "application/json; charset=utf-8"})
            try:
                with urllib.request.urlopen(req, timeout=_timeout) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                try:
                    body = json.loads(e.read() or b"{}")
                except ValueError:
                    body = {"ok": False, "description": f"HTTP {e.code}"}
                if e.code == 429:
                    time.sleep(body.get("parameters", {}).get("retry_after", 3))
                    continue
                if e.code >= 500 and attempt + 1 < _retries:
                    time.sleep(1 + attempt)
                    continue
                return body
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt + 1 < _retries:
                    time.sleep(1 + attempt)
                    continue
                return {"ok": False, "description": str(e)}
        return {"ok": False, "description": "too many retries"}

    def upload(self, method, files, **params):
        """Multipart request. files: {field: Path}; non-string params are sent as JSON."""
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in params.items():
            if v is None:
                continue
            if not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False)
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        for field, path in files.items():
            path = Path(path)
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            name = path.name.replace('"', "'")
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
                         f'filename="{name}"\r\nContent-Type: {ctype}\r\n\r\n'.encode())
            parts.append(path.read_bytes() + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        for attempt in range(3):
            req = urllib.request.Request(self.api + method, data=body,
                                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            try:
                with urllib.request.urlopen(req, timeout=300) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                try:
                    res = json.loads(e.read() or b"{}")
                except ValueError:
                    res = {"ok": False, "description": f"HTTP {e.code}"}
                if e.code == 429:
                    time.sleep(res.get("parameters", {}).get("retry_after", 3))
                    continue
                return res
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < 2:
                    time.sleep(2)
                    continue
                return {"ok": False, "description": str(e)}
        return {"ok": False, "description": "too many retries"}

    def download(self, file_id, dest):
        info = self("getFile", file_id=file_id)
        if not info.get("ok"):
            raise RuntimeError(info.get("description", "getFile failed"))
        fp = info["result"]["file_path"]
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(f"https://api.telegram.org/file/bot{self.token}/{fp}", timeout=120) as r:
            dest.write_bytes(r.read())
        return dest
