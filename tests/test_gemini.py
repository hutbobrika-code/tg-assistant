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

import gemini
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
        self.assertTrue(len(GEMINI_MODELS) >= 5)
        ids = [m["id"] for m in GEMINI_MODELS]
        self.assertIn("gemini/gemini-4-argon", ids)
        self.assertIn("gemini/gemini-3.1-pro", ids)
        self.assertIn("gemini/gemini-3.8-flash", ids)
        self.assertIn("gemini/gemini-3.5-flash-lite", ids)
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
        self.assertTrue(any("4 Argon" in lbl for lbl in labels))
        self.assertTrue(any("3.8 Flash" in lbl for lbl in labels))
        self.assertTrue(any("3.1 Pro" in lbl for lbl in labels))
        self.assertTrue(any("3.5 Flash-Lite" in lbl for lbl in labels))

    def test_gemini_chat_execution(self):
        self.chat.st["model"] = "gemini/gemini-4-argon"
        self.run_job("Напиши приветствие", mid=101)

        self.assertTrue(len(self.finals()) > 0)
        final_md = self.finals()[-1]
        self.assertIn("Привет! Это ответ от Gemini.", final_md)
        self.assertIn("4 Argon", final_md)

        # Check that conversation was saved in state
        gem_st = self.chat.st.get("gemini", {})
        msgs = gem_st.get("messages", [])
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "Напиши приветствие")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["content"], "Привет! Это ответ от Gemini.")

    def test_gemini_chats_view(self):
        old_list = bot.gemini.list_web_chats
        bot.gemini.list_web_chats = lambda limit=50: [
            {"id": "c_1", "title": "Чат про математику"},
            {"id": "c_2", "title": "Тестовый диалог"},
        ]
        try:
            md, kb = bot.gemini_chats_view(self.chat, page=0)
            self.assertIn("Чаты Gemini Web", md)
            self.assertIn("artur.hodzhojan@gmail.com", md)
            labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
            self.assertTrue(any("Чат про математику" in lbl for lbl in labels))
            self.assertTrue(any("Тестовый диалог" in lbl for lbl in labels))
        finally:
            bot.gemini.list_web_chats = old_list

    def test_gemini_open_web_chat_button(self):
        old_list = bot.gemini.list_web_chats
        old_get = bot.gemini.get_web_chat
        bot.gemini.list_web_chats = lambda limit=50: [{"id": "c_geom", "title": "Геометрия"}]
        bot.gemini.get_web_chat = lambda cid: {
            "id": cid,
            "messages": [
                {"role": "user", "content": "Чему равна гипотенуза?"},
                {"role": "assistant", "content": "По теореме Пифагора c = sqrt(a^2 + b^2)."}
            ]
        }
        try:
            toast = bot.on_button(self.chat, 501, "gw:o:c_geom")
            self.assertIn("Загружен", toast)
            st = self.chat.st
            self.assertTrue(bot.engine_of(st["model"]) == "gemini")
            gem_st = st.get("gemini", {})
            self.assertEqual(gem_st["cid"], "c_geom")
            self.assertEqual(gem_st["title"], "Геометрия")
            self.assertEqual(len(gem_st["messages"]), 2)
        finally:
            bot.gemini.list_web_chats = old_list
            bot.gemini.get_web_chat = old_get

    def test_gemini_chat_continuation_with_history(self):
        self.chat.st["model"] = "gemini/gemini-4-argon"
        self.chat.st["gemini"] = {
            "cid": "c_geom",
            "title": "Геометрия",
            "messages": [
                {"role": "user", "content": "Чему равна гипотенуза?"},
                {"role": "assistant", "content": "По теореме Пифагора c = sqrt(a^2 + b^2)."}
            ]
        }
        self.run_job("А если катеты 3 и 4?", mid=102)
        gem_st = self.chat.st.get("gemini", {})
        msgs = gem_st.get("messages", [])
        self.assertEqual(len(msgs), 4)
        self.assertEqual(msgs[0]["content"], "Чему равна гипотенуза?")
        self.assertEqual(msgs[2]["content"], "А если катеты 3 и 4?")
        self.assertEqual(msgs[3]["role"], "assistant")
        self.assertIn("Привет! Это ответ от Gemini.", msgs[3]["content"])

    def test_gemini_sessions_view_has_web_chats_button(self):
        self.chat.st["gemini"] = {
            "cid": "c_geom",
            "title": "Геометрия",
            "messages": [{"role": "user", "content": "q"}]
        }
        md, kb = bot.sessions_view(self.chat)
        all_datas = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        self.assertIn("gw:list", all_datas)
        self.assertIn("gw:cur", all_datas)

    def test_clean_gemini_text(self):
        import gemini
        raw = (
            "```python?code_reference&code_event_index=1\nimport math\n```\n"
            "[http://googleusercontent.com/generated_image/861]\n"
            "Рисунок к задаче[cite: 1][file-tag: code-0-123]."
        )
        cleaned = gemini.clean_gemini_text(raw)
        self.assertIn("```python\nimport math\n```", cleaned)
        self.assertNotIn("?code_reference", cleaned)
        self.assertNotIn("generated_image", cleaned)
        self.assertNotIn("[cite: 1]", cleaned)
        self.assertNotIn("[file-tag:", cleaned)

    def test_gemini_open_web_chat_with_images(self):
        old_list = bot.gemini.list_web_chats
        old_get = bot.gemini.get_web_chat
        old_dl = bot.gemini.download_web_image

        with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp_img:
            tmp_img.write(b"fake image data")
            tmp_img.flush()

            bot.gemini.list_web_chats = lambda limit=50: [{"id": "c_stereo", "title": "Стереометрия"}]
            bot.gemini.get_web_chat = lambda cid: {
                "id": cid,
                "messages": [
                    {
                        "role": "user",
                        "content": "Реши задачу с рисунка",
                        "images": ["https://lh3.googleusercontent.com/gg/fake_user_img"]
                    },
                    {
                        "role": "assistant",
                        "content": "Ответ: S = 12",
                        "images": ["https://lh3.googleusercontent.com/gg/fake_asst_img"]
                    }
                ]
            }
            bot.gemini.download_web_image = lambda url, dest=None: Path(tmp_img.name)

            try:
                toast = bot.on_button(self.chat, 502, "gw:o:c_stereo")
                self.assertIn("Загружен", toast)
                st = self.chat.st
                gem_st = st.get("gemini", {})
                self.assertEqual(gem_st["cid"], "c_stereo")
                self.assertEqual(len(gem_st["messages"]), 2)

                # Verify that photos were uploaded to Telegram
                uploads = [call for call in self.tg.calls if call[1] == "sendPhoto"]
                self.assertTrue(len(uploads) >= 2)
            finally:
                bot.gemini.list_web_chats = old_list
                bot.gemini.get_web_chat = old_get
                bot.gemini.download_web_image = old_dl

    def test_save_data_images(self):
        with tempfile.TemporaryDirectory() as td:
            turn = gemini.GeminiTurn("http://127.0.0.1:9999", "k", "m", [], td)
            b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            raw = f"Вот картинка: data:image/png;base64,{b64}"
            res = turn._save_data_images(raw)
            self.assertIn("![Сгенерированное изображение](uploads/gemini_", res)
            self.assertNotIn("data:image/png", res)
            # Verify file exists
            files = list((Path(td) / "uploads").glob("gemini_*.png"))
            self.assertEqual(len(files), 1)

    def test_resolve_google_images(self):
        with tempfile.TemporaryDirectory() as td:
            turn = gemini.GeminiTurn("http://127.0.0.1:9999", "k", "m", [], td)
            old_list = gemini.list_web_chats
            old_get = gemini.get_web_chat
            old_dl = gemini.download_web_image

            with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp_img:
                tmp_img.write(b"fake cat image")
                tmp_img.flush()

                gemini.list_web_chats = lambda limit=3: [{"id": "c_cat", "title": "Кот"}]
                gemini.get_web_chat = lambda cid: {
                    "id": cid,
                    "messages": [
                        {"role": "assistant", "images": ["https://lh3.googleusercontent.com/gg/fake_cat"]}
                    ]
                }
                gemini.download_web_image = lambda url, dest=None: Path(tmp_img.name)

                try:
                    raw = "Вот сгенерированный кот:\n\nhttp://googleusercontent.com/image_generation_content/0_851"
                    res = turn._resolve_google_images(raw)
                    self.assertIn("![Сгенерированное изображение](uploads/", res)
                    self.assertNotIn("image_generation_content", res)
                finally:
                    gemini.list_web_chats = old_list
                    gemini.get_web_chat = old_get
                    gemini.download_web_image = old_dl

    def test_save_data_audio_and_video(self):
        with tempfile.TemporaryDirectory() as td:
            turn = gemini.GeminiTurn("http://127.0.0.1:9999", "k", "m", [], td)
            b64_audio = "AAAA"
            b64_video = "VVVV"
            raw = f"Вот музыка: data:audio/mp3;base64,{b64_audio} и видео: data:video/mp4;base64,{b64_video}"
            res = turn._save_data_media(raw)
            self.assertIn("![Сгенерированная музыка](uploads/gemini_audio_", res)
            self.assertIn("![Сгенерированное видео](uploads/gemini_video_", res)
            self.assertNotIn("data:audio/mp3", res)
            self.assertNotIn("data:video/mp4", res)

    def test_resolve_google_media_audio(self):
        with tempfile.TemporaryDirectory() as td:
            turn = gemini.GeminiTurn("http://127.0.0.1:9999", "k", "m", [], td)
            old_list = gemini.list_web_chats
            old_get = gemini.get_web_chat
            old_dl = gemini.download_web_image

            with tempfile.NamedTemporaryFile(suffix=".m4a") as tmp_audio:
                tmp_audio.write(b"fake audio stream")
                tmp_audio.flush()

                gemini.list_web_chats = lambda *a, **k: [{"id": "c_mus", "title": "Музыка"}]
                gemini.get_web_chat = lambda *a, **k: {
                    "id": "c_mus",
                    "messages": [
                        {"role": "assistant", "images": ["https://lh3.googleusercontent.com/gg/fake_music=m140"]}
                    ]
                }
                gemini.download_web_image = lambda *a, **k: Path(tmp_audio.name)

                try:
                    raw = "Вот музыка:\n\nhttp://googleusercontent.com/generated_music_content/0"
                    res = turn._resolve_google_media(raw)
                    self.assertIn("![Сгенерированная музыка](uploads/", res)
                    self.assertNotIn("generated_music_content", res)
                finally:
                    gemini.list_web_chats = old_list
                    gemini.get_web_chat = old_get
                    gemini.download_web_image = old_dl

    def test_limits_info(self):
        info = gemini.get_gemini_limits_info()
        self.assertIn("email", info)
        self.assertIn("plan", info)
        self.assertIn("quotas", info)
        self.assertTrue(len(info["quotas"]) >= 5)

    def test_new_commands_and_views(self):
        # Test command handlers
        self.assertTrue(bot.handle_command(self.chat, {"message_id": 10}, "/limits"))
        self.assertTrue(bot.handle_command(self.chat, {"message_id": 11}, "/video"))
        self.assertTrue(bot.handle_command(self.chat, {"message_id": 12}, "/music"))
        self.assertTrue(bot.handle_command(self.chat, {"message_id": 13}, "/image"))
        self.assertTrue(bot.handle_command(self.chat, {"message_id": 14}, "/deepresearch"))

        # Test view renderers
        md, kb = bot.limits_view(self.chat)
        self.assertIn("Лимиты на использование Gemini", md)
        self.assertIn("Текущее использование", md)
        self.assertTrue(len(kb["inline_keyboard"]) >= 3)

        # Test progress bar
        bar_97 = bot.make_progress_bar(97, 12)
        self.assertTrue(bar_97.startswith("██████████"))
        bar_16 = bot.make_progress_bar(16, 12)
        self.assertTrue("░" in bar_16)

        md, kb = bot.video_guide_view(self.chat)
        self.assertIn("Google Veo 2", md)

        md, kb = bot.music_guide_view(self.chat)
        self.assertIn("Google Lyria", md)

        md, kb = bot.deepresearch_guide_view(self.chat)
        self.assertIn("Deep Research", md)



