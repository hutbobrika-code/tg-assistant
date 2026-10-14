"""Claude Code CLI in headless mode (`claude -p`) as a second agent backend.

A turn is one `claude -p --output-format stream-json` process. Its events are
translated into OpenCode-style parts (text / reasoning / tool) so the bot can
render both backends the same way. Every turn resumes the previous session
with --fork-session, so the state before a turn stays available for "regenerate".
"""
import base64
import json
import os
import signal
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

from tg import log

# Claude Code tool names -> the OpenCode names the bot knows how to describe
TOOL_NAMES = {
    "Bash": "bash", "Read": "read", "Write": "write", "Edit": "edit", "MultiEdit": "edit",
    "NotebookEdit": "edit", "Glob": "glob", "Grep": "grep", "LS": "list", "WebFetch": "webfetch",
    "WebSearch": "websearch", "TodoWrite": "todowrite", "Task": "task", "Agent": "task", "Skill": "skill",
}
IMAGE_MIME = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def tool_input(raw):
    """Claude Code tool input -> OpenCode-style keys."""
    inp = dict(raw or {})
    for src in ("file_path", "notebook_path"):
        if src in inp:
            inp["filePath"] = inp.pop(src)
    return inp


def content_blocks(parts):
    """Bot job parts (OpenCode format) -> Anthropic content blocks for stream-json input."""
    blocks = []
    for p in parts:
        if p.get("type") == "text":
            blocks.append({"type": "text", "text": p["text"]})
        elif p.get("type") == "file" and p.get("url", "").startswith("file://"):
            path = Path(p["url"][7:])
            mime = p.get("mime", "")
            try:
                data = base64.b64encode(path.read_bytes()).decode()
            except OSError:
                continue
            if mime in IMAGE_MIME:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
            elif mime == "application/pdf":
                blocks.append({"type": "document", "source": {"type": "base64", "media_type": mime, "data": data}})
            # other files are mentioned in the text; Claude reads them with its tools
    return blocks


class ClaudeTurn:
    """One `claude -p` process. Thread-safe `parts`, `session_id`, `result` fields."""

    def __init__(self, cmd, env, cwd, blocks, on_change=None):
        self.parts = OrderedDict()
        self.lock = threading.Lock()
        self.on_change = on_change or (lambda: None)
        self.session_id = None
        self.result = None
        self.rate_limit = None
        self.context = None
        self.stderr = []
        self.done = threading.Event()
        self._msg = None
        self._blocks = {}  # (message id, index) -> part
        self._json = {}    # tool part id -> partial input json
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, start_new_session=True, bufsize=1)
        threading.Thread(target=self._read_stderr, daemon=True).start()
        threading.Thread(target=self._read, daemon=True).start()
        msg = {"type": "user", "message": {"role": "user", "content": blocks}}
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.close()
        except OSError as e:
            log("claude stdin:", e)

    # process I/O
    def _read_stderr(self):
        for line in self.proc.stderr:
            self.stderr.append(line)
            del self.stderr[:-50]

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                try:
                    self.handle(ev)
                except Exception as e:  # never let one odd event kill the stream
                    log("claude event:", type(e).__name__, e)
        finally:
            self.proc.wait()
            self.done.set()
            self.on_change()

    def stop(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            threading.Timer(5, self._kill).start()

    def _kill(self):
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # stream-json -> parts
    def handle(self, ev):
        t = ev.get("type")
        if ev.get("session_id") and t in ("system", "result"):
            self.session_id = ev["session_id"]
        if t == "stream_event":
            with self.lock:
                self._stream(ev.get("event") or {})
        elif t == "assistant":
            with self.lock:
                self._assistant(ev.get("message") or {})
        elif t == "user":
            with self.lock:
                self._tool_results(ev.get("message") or {})
        elif t == "rate_limit_event":
            self.rate_limit = ev.get("rate_limit_info")
        elif t == "result":
            self.result = ev
        else:
            return
        self.on_change()

    def _stream(self, se):
        kind = se.get("type")
        now = time.time() * 1000
        if kind == "message_start":
            self._msg = (se.get("message") or {}).get("id") or f"m{len(self.parts)}"
        elif kind == "content_block_start":
            block = se.get("content_block") or {}
            key = (self._msg, se.get("index"))
            pid = f"{self._msg}:{se.get('index')}"
            btype = block.get("type")
            if btype == "text":
                part = {"id": pid, "type": "text", "text": block.get("text", ""), "time": {"start": now}}
            elif btype in ("thinking", "redacted_thinking"):
                part = {"id": pid, "type": "reasoning", "text": "", "time": {"start": now}}
            elif btype in ("tool_use", "server_tool_use"):
                pid = block.get("id") or pid
                name = block.get("name", "tool")
                part = {"id": pid, "type": "tool", "tool": TOOL_NAMES.get(name, name.lower()),
                        "state": {"status": "pending", "input": {}}}
                self._json[pid] = ""
            else:
                return
            self._blocks[key] = part
            self.parts[part["id"]] = part
        elif kind == "content_block_delta":
            part = self._blocks.get((self._msg, se.get("index")))
            delta = se.get("delta") or {}
            if part is None:
                return
            if delta.get("type") == "text_delta":
                part["text"] = part.get("text", "") + delta.get("text", "")
            elif delta.get("type") == "input_json_delta":
                self._json[part["id"]] = self._json.get(part["id"], "") + delta.get("partial_json", "")
        elif kind == "content_block_stop":
            part = self._blocks.get((self._msg, se.get("index")))
            if part is None:
                return
            if part["type"] == "tool":
                try:
                    part["state"]["input"] = tool_input(json.loads(self._json.get(part["id"]) or "{}"))
                except ValueError:
                    pass
                part["state"]["status"] = "running"
            else:
                part["time"]["end"] = now

    def _assistant(self, msg):
        """Complete blocks: authoritative tool inputs, and text if no stream events were seen."""
        usage = msg.get("usage") or {}
        if usage.get("input_tokens") is not None:
            self.context = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0) + \
                usage.get("cache_creation_input_tokens", 0)
        for i, block in enumerate(msg.get("content") or []):
            if block.get("type") in ("tool_use", "server_tool_use"):
                part = self.parts.get(block.get("id"))
                if part is None:
                    name = block.get("name", "tool")
                    part = self.parts[block["id"]] = {"id": block["id"], "type": "tool",
                                                      "tool": TOOL_NAMES.get(name, name.lower()),
                                                      "state": {"status": "running", "input": {}}}
                part["state"]["input"] = tool_input(block.get("input"))
                if part["state"]["status"] == "pending":
                    part["state"]["status"] = "running"
            elif block.get("type") == "text" and block.get("text"):
                mid = msg.get("id")
                if not any(p["id"].startswith(f"{mid}:") and p["type"] == "text" for p in self.parts.values()):
                    pid = f"{mid}:t{i}"
                    self.parts[pid] = {"id": pid, "type": "text", "text": block["text"],
                                       "time": {"start": 1, "end": 2}}

    def _tool_results(self, msg):
        content = msg.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if block.get("type") == "tool_result":
                part = self.parts.get(block.get("tool_use_id"))
                if part is not None:
                    part["state"]["status"] = "error" if block.get("is_error") else "completed"

    # results
    def snapshot(self):
        with self.lock:
            return [json.loads(json.dumps(p)) for p in self.parts.values()]

    def error(self):
        """Human-readable failure, or None."""
        r = self.result
        if r and not r.get("is_error") and r.get("subtype") == "success":
            return None
        if r:
            text = r.get("result") or "; ".join(r.get("errors") or []) or r.get("subtype", "ошибка")
            return str(text)
        tail = "".join(self.stderr).strip()
        return tail[-600:] if tail else None


class ClaudeCode:
    def __init__(self, binary, workdir, token):
        self.binary = binary
        self.workdir = str(workdir)
        self.token = token

    def available(self):
        return bool(self.token) and Path(self.binary).exists()

    def start(self, parts, model, effort=None, resume=None, plan=False, on_change=None):
        cmd = [self.binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
               "--include-partial-messages", "--verbose", "--model", model,
               "--permission-mode", "plan" if plan else "bypassPermissions",
               "--disallowedTools", "AskUserQuestion"]
        if effort:
            cmd += ["--effort", effort]
        if resume:
            cmd += ["--resume", resume, "--fork-session"]
        env = dict(os.environ, DISABLE_AUTOUPDATER="1")
        env.pop("ANTHROPIC_API_KEY", None)
        if self.token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = self.token
        return ClaudeTurn(cmd, env, self.workdir, content_blocks(parts), on_change)

    def session_file(self, sid):
        """Where Claude Code keeps a session transcript for our working directory."""
        project = self.workdir.replace("/", "-").replace(".", "-")
        return Path.home() / ".claude" / "projects" / project / f"{sid}.jsonl"

    def forget(self, sid):
        try:
            self.session_file(sid).unlink()
        except (OSError, ValueError):
            pass
