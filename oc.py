"""Client for the `opencode serve` HTTP API (v1 routes)."""
import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from tg import log


class OpenCodeError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class OpenCode:
    def __init__(self, base, password, directory):
        self.base = base.rstrip("/")
        self.directory = str(directory)
        self.headers = {"Content-Type": "application/json"}
        if password:
            token = base64.b64encode(f"opencode:{password}".encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"

    def _url(self, path, **query):
        query = {"directory": self.directory, **{k: v for k, v in query.items() if v is not None}}
        return f"{self.base}{path}?{urllib.parse.urlencode(query)}"

    def req(self, method, path, body=None, timeout=60, **query):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self._url(path, **query), data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise OpenCodeError(f"{method} {path}: HTTP {e.code} {detail}", e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise OpenCodeError(f"{method} {path}: {e}") from None
        return json.loads(raw) if raw else None

    # sessions
    def create_session(self):
        return self.req("POST", "/session", {})["id"]

    def session(self, sid):
        """Session info, or None if it no longer exists. Other errors propagate."""
        try:
            return self.req("GET", f"/session/{sid}")
        except OpenCodeError as e:
            if e.status in (400, 404):
                return None
            raise

    def sessions(self):
        return self.req("GET", "/session") or []

    def messages(self, sid):
        return self.req("GET", f"/session/{sid}/message", timeout=120) or []

    def status(self):
        return self.req("GET", "/session/status", timeout=15) or {}

    def prompt(self, sid, parts, model, variant=None, agent=None):
        provider, _, model_id = model.partition("/")
        body = {"parts": parts, "model": {"providerID": provider, "modelID": model_id}}
        if variant:
            body["variant"] = variant
        if agent:
            body["agent"] = agent
        self.req("POST", f"/session/{sid}/prompt_async", body)

    def abort(self, sid):
        try:
            self.req("POST", f"/session/{sid}/abort", {})
        except OpenCodeError as e:
            log("abort:", e)

    def revert(self, sid, message_id):
        self.req("POST", f"/session/{sid}/revert", {"messageID": message_id})

    # interactive requests from the agent
    def question_reply(self, rid, answers):
        self.req("POST", f"/question/{rid}/reply", {"answers": answers})

    def question_reject(self, rid):
        self.req("POST", f"/question/{rid}/reject", {})

    def permission_reply(self, rid, reply):
        self.req("POST", f"/permission/{rid}/reply", {"reply": reply})

    # models
    def models(self):
        """[{id: 'provider/model', name, variants: [...], context}]"""
        data = self.req("GET", "/config/providers")
        out = []
        for p in data.get("providers", []):
            for mid, m in p.get("models", {}).items():
                variants = m.get("variants") or {}
                out.append({
                    "id": f"{p['id']}/{mid}",
                    "name": m.get("name") or mid,
                    "variants": list(variants.keys()) if isinstance(variants, dict) else list(variants),
                    "context": (m.get("limit") or {}).get("context"),
                })
        return out

    def events(self):
        """Endless generator of server events; reconnects on failure."""
        while True:
            try:
                r = urllib.request.Request(self._url("/event"), headers=self.headers)
                with urllib.request.urlopen(r, timeout=90) as resp:
                    buf = []
                    for raw in resp:
                        line = raw.decode(errors="replace").rstrip("\r\n")
                        if line.startswith("data:"):
                            buf.append(line[5:].lstrip())
                        elif not line and buf:
                            try:
                                yield json.loads("\n".join(buf))
                            except ValueError:
                                pass
                            buf = []
            except Exception as e:
                log("event stream:", e)
            time.sleep(2)
