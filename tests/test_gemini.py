"""Tests for the Gemini Web2API integration."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

from gemini import GEMINI_MODELS, GeminiRouter, extract_prompt_and_images
import bot
from test_flows import FlowBase, job
from fakes import wait_idle


class MockGeminiHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress server logs

    def do_GET(self):
        if self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"object":"list","data":[{"id":"gemini-3.1-pro"}]}')
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()

            # Stream thinking delta
            chunk1 = {
                "choices": [{"delta": {"reasoning_content": "Думаю над ответом..."}}]
            }
            self.wfile.write(f"data: {json.dumps(chunk1)}\n\n".encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.05)

            # Stream text delta
            chunk2 = {
                "choices": [{"delta": {"content": "Привет! Это ответ от Gemini."}}]
            }
            self.wfile.write(f"data: {json.dumps(chunk2)}\n\n".encode("utf-8"))
            self.wfile.flush()

            # Stream done
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            self.send_response(404)
            self.end_headers()


class GeminiClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), MockGeminiHandler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_gemini_models_metadata(self):
        self.assertTrue(len(GEMINI_MODELS) >= 3)
        ids = [m["id"] for m in GEMINI_MODELS]
        self.assertIn("gemini/gemini-3.1-pro", ids)
        self.assertIn("gemini/gemini-3.6-flash", ids)
        self.assertIn("gemini/gemini-image", ids)

    def test_extract_prompt_and_images(self):
        parts = [
            {"type": "text", "text": "Опиши эту картинку"},
        ]
        text, imgs = extract_prompt_and_images(parts)
        self.assertEqual(text, "Опиши эту картинку")
        self.assertEqual(imgs, [])

    def test_gemini_streaming_turn(self):
        with tempfile.TemporaryDirectory() as td:
            router = GeminiRouter(
                base_url=f"http://127.0.0.1:{self.port}/v1",
                api_key="test-key",
                workdir=td,
            )
            self.assertTrue(router.available())

            changes = []
            turn = router.start(
                parts=[{"type": "text", "text": "Привет"}],
                model_id="gemini/gemini-3.1-pro",
                on_change=lambda: changes.append(time.time()),
            )
            self.assertTrue(turn.done.wait(timeout=5))
            parts = turn.snapshot()
            self.assertEqual(len(parts), 2)
            self.assertEqual(parts[0]["type"], "reasoning")
            self.assertIn("Думаю", parts[0]["text"])
            self.assertEqual(parts[1]["type"], "text")
            self.assertIn("Привет! Это ответ от Gemini.", parts[1]["text"])
            self.assertIsNone(turn.error())


class GeminiFlowTest(FlowBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server = HTTPServer(("127.0.0.1", 0), MockGeminiHandler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.old_gemini = bot.gemini
        bot.gemini = GeminiRouter(
            base_url=f"http://127.0.0.1:{self.port}/v1",
            api_key="test-key",
            workdir=bot.WORKDIR,
        )

    def tearDown(self):
        bot.gemini = self.old_gemini
        super().tearDown()

    def test_gemini_models_view(self):
        md, kb = bot.models_view(self.chat)
        self.assertIn("Gemini", md)
        labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
        self.assertTrue(any("Gemini 3.1 Pro" in lbl for lbl in labels))

    def test_gemini_chat_execution(self):
        self.chat.st["model"] = "gemini/gemini-3.1-pro"
        self.run_job("Напиши приветствие", mid=101)

        self.assertTrue(len(self.finals()) > 0)
        final_md = self.finals()[-1]
        self.assertIn("Привет! Это ответ от Gemini.", final_md)
        self.assertIn("Gemini 3.1 Pro", final_md)

        # Check that conversation was saved in state
        gem_st = self.chat.st.get("gemini", {})
        msgs = gem_st.get("messages", [])
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "Напиши приветствие")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["content"], "Привет! Это ответ от Gemini.")

