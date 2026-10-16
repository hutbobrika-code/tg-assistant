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
            if body.get("model") in ("gemini-music", "gemini/gemini-music") and not body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                fake_b64 = "AAAA"
                res = {
                    "choices": [{
                        "message": {
                            "role": "assistant",
                            "content": f"data:audio/m4a;base64,{fake_b64}"
                        }
                    }]
                }
                self.wfile.write(json.dumps(res).encode("utf-8"))
                return

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

        # Test view renderers (fallback branch when no live network)
        md, kb = bot.limits_view(self.chat)
        self.assertIn("Лимиты на использование Gemini", md)
        self.assertTrue(len(kb["inline_keyboard"]) >= 3)

        # Test view with live/mocked web usage metrics
        old_fetch = gemini.fetch_gemini_web_usage
        gemini.fetch_gemini_web_usage = lambda **k: {
            "tier": "PRO",
            "current": {"percent": 97, "reset_time": "19:52"},
            "weekly": {"percent": 16, "reset_time": "20 окт. в 10:52"},
            "updated_at": "только что"
        }
        try:
            md_web, _ = bot.limits_view(self.chat)
            self.assertIn("Текущее использование", md_web)
            self.assertIn("97 %", md_web)
            self.assertIn("Недельный лимит", md_web)
            self.assertIn("16 %", md_web)
        finally:
            gemini.fetch_gemini_web_usage = old_fetch

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

    def test_followup_extraction_and_cleaning(self):
        sample_text = (
            "Вот подробный план тренировок для набора массы...\n\n"
            "<thought>Thinking about muscles...</thought>\n"
            '<FollowUp label="Советы по питанию" query="Какие советы по питанию помогут быстрее набрать массу?"/>\n'
            '<FollowUp label="Пример программы тренировок" query="Составь пример программы тренировок на 3 дня для набора массы."/>\n'
            '<FollowUp label="Ошибки новичков" query="Какие частые ошибки совершают новички при наборе массы?"/>'
        )
        cleaned, fus = gemini.clean_gemini_text_and_extract_followups(sample_text)
        self.assertEqual(len(fus), 3)
        self.assertEqual(fus[0]["label"], "Советы по питанию")
        self.assertEqual(fus[0]["query"], "Какие советы по питанию помогут быстрее набрать массу?")
        self.assertNotIn("<FollowUp", cleaned)
        self.assertNotIn("<thought>", cleaned)
        self.assertIn("Вот подробный план тренировок для набора массы...", cleaned)

    def test_followup_buttons_and_action(self):
        fus = [{"label": "Советы по питанию", "query": "Подробнее о питании"}]
        kb = bot.answer_keyboard([], followups=fus, chat=self.chat)
        found_fu_btn = any("Советы по питанию" in btn_item.get("text", "") for row in kb["inline_keyboard"] for btn_item in row)
        self.assertTrue(found_fu_btn)
        note = bot.on_button(self.chat, 999, "do_fu:0")
        self.assertIn("Запрос:", note)

    def test_reply_menu_and_pending_mode(self):
        self.chat.st.pop("pending_mode", None)
        self.assertTrue(bot.handle_menu_button(self.chat, "🎨 Фото", {"message_id": 10}))
        self.assertEqual(self.chat.st.get("pending_mode"), "image")

        enqueued = []
        old_enqueue = self.chat.enqueue
        self.chat.enqueue = lambda j: enqueued.append(j)
        try:
            bot.process_messages(self.chat, [{"message_id": 11, "text": "рыжий кот в очках"}])
            self.assertIsNone(self.chat.st.get("pending_mode"))
            self.assertEqual(self.chat.st.get("model"), "gemini/gemini-image")
            self.assertEqual(len(enqueued), 1)
        finally:
            self.chat.enqueue = old_enqueue

    def test_limits_view_no_water(self):
        md, _ = bot.limits_view(self.chat)
        self.assertNotIn("Параметры подключения", md)
        self.assertNotIn("Cookie Pool", md)
        self.assertNotIn("Квоты моделей", md)
        self.assertNotIn("Статистика бота за 24 часа", md)

    def test_format_gemini_error(self):
        # 1. Timeout
        err_to = gemini.format_gemini_error("Connection timed out", "")
        self.assertIn("Время ожидания ответа от Google Gemini истекло", err_to)
        self.assertIn("4 Argon", err_to)
        self.assertIn("3.8 Flash", err_to)

        # 2. Cookie / SNlM0e
        err_ck = gemini.format_gemini_error("502 Bad Gateway", "no SNlM0e in page (cookie expired)")
        self.assertIn("Сессия Google Gemini обновляется", err_ck)
        self.assertIn("GitHub Actions", err_ck)

        # 3. Artifact delay / Lyria
        err_art = gemini.format_gemini_error("media generation succeeded but artifact retrieval failed: hNvQHb", "")
        self.assertIn("Google Gemini создал медиафайл", err_art)

        # 4. Connection refused
        err_cr = gemini.format_gemini_error("[Errno 111] Connection refused", "")
        self.assertIn("Локальный шлюз Gemini Web2API временно недоступен", err_cr)

    def test_friendly_gemini_error(self):
        self.assertIn("Время ожидания", bot.friendly_gemini_error("Gemini connection error: timed out"))
        self.assertIn("Сессия Google Gemini обновляется", bot.friendly_gemini_error("upstream error: cookie expired"))
        self.assertIn("Медиафайл создаётся", bot.friendly_gemini_error("media generation succeeded but artifact retrieval failed"))

    def test_activity_md_media_and_thinking_progress(self):
        # Default reasoning
        parts_default = [{"type": "reasoning", "text": "Analyzing the theorem..."}]
        self.assertEqual(bot.activity_md(parts_default), "💭 **Размышляю**")

        # Video progress
        parts_vid = [{"type": "reasoning", "text": "🎬 Генерация видео нейросетью Veo 2... (42 сек)"}]
        self.assertIn("🎬", bot.activity_md(parts_vid))
        self.assertIn("Veo 2", bot.activity_md(parts_vid))

        # Music progress
        parts_mus = [{"type": "reasoning", "text": "🎵 Синтез музыки нейросетью Google Lyria... (15 сек)"}]
        self.assertIn("🎵", bot.activity_md(parts_mus))
        self.assertIn("Google Lyria", bot.activity_md(parts_mus))

        # Deep thinking retry progress
        parts_retry = [{"type": "reasoning", "text": "⏳ Флагманская модель 4 Argon завершает рассуждения..."}]
        self.assertIn("⏳", bot.activity_md(parts_retry))
        self.assertIn("4 Argon", bot.activity_md(parts_retry))

    def test_generate_music_direct_base64(self):
        with tempfile.TemporaryDirectory() as td:
            mus_path, desc = gemini.generate_music(
                "Upbeat synthwave track",
                base_url=f"http://127.0.0.1:{self.port}/v1",
                api_key="test-key",
                workdir=td,
            )
            self.assertTrue(mus_path.startswith("uploads/gemini_music_"))
            self.assertTrue(mus_path.endswith(".m4a"))
            created_file = Path(td) / mus_path
            self.assertTrue(created_file.is_file())
            self.assertEqual(created_file.read_bytes(), b"\x00\x00\x00")

    def test_generate_music_web_polling(self):
        with tempfile.TemporaryDirectory() as td:
            with tempfile.NamedTemporaryFile(suffix=".m4a") as tmp_audio:
                tmp_audio.write(b"fake audio data stream 1234567890" * 30)
                tmp_audio.flush()

                old_list = gemini.list_web_chats
                old_get = gemini.get_web_chat
                old_dl = gemini.download_web_media

                gemini.list_web_chats = lambda limit=3: [{"id": "c_mus", "title": "Музыка"}]
                gemini.get_web_chat = lambda cid: {
                    "id": cid,
                    "messages": [
                        {"role": "assistant", "content": "Трек в стиле рок",
                         "images": ["https://lh3.googleusercontent.com/gg/sample=m140"]}
                    ]
                }
                gemini.download_web_media = lambda url, dest=None: Path(tmp_audio.name)

                try:
                    # Point to 404 URL so router returns 404/error, triggering web chat polling
                    mus_path, desc = gemini.generate_music(
                        "Rock riff",
                        base_url="http://127.0.0.1:9999/v1",
                        api_key="test-key",
                        workdir=td,
                    )
                    self.assertTrue(mus_path.startswith("uploads/gemini_music_"))
                    self.assertIn("Трек в стиле рок", desc)
                finally:
                    gemini.list_web_chats = old_list
                    gemini.get_web_chat = old_get
                    gemini.download_web_media = old_dl

    def test_music_guide_view_layout(self):
        md, kb = bot.music_guide_view(self.chat)
        self.assertIn("Google Lyria", md)
        rows = kb["inline_keyboard"]
        # Clean minimalist layout: only cancel button, no preset buttons
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0]["callback_data"], "cancel_pending")

    def test_direct_prompt_intent_routing(self):
        enqueued = []
        old_enqueue = self.chat.enqueue
        self.chat.enqueue = lambda j: enqueued.append(j)
        try:
            # 1. Direct image intent: "нарисуй кота"
            self.chat.st.pop("pending_mode", None)
            bot.process_messages(self.chat, [{"message_id": 20, "text": "нарисуй рыжего кота в скафандре"}])
            self.assertEqual(self.chat.st.get("model"), "gemini/gemini-image")
            self.assertEqual(len(enqueued), 1)

            # 2. Direct video intent: "сделай видео как взлетает ракета"
            bot.process_messages(self.chat, [{"message_id": 21, "text": "сделай видео как взлетает ракета"}])
            self.assertEqual(self.chat.st.get("model"), "gemini/gemini-video")
            self.assertEqual(len(enqueued), 2)

            # 3. Direct music intent: "создай трек в стиле рок"
            bot.process_messages(self.chat, [{"message_id": 22, "text": "создай трек в стиле рок"}])
            self.assertEqual(self.chat.st.get("model"), "gemini/gemini-music")
            self.assertEqual(len(enqueued), 3)

            # 4. Direct deep research intent: "глубокое исследование квантовых процессоров"
            bot.process_messages(self.chat, [{"message_id": 23, "text": "глубокое исследование квантовых процессоров"}])
            self.assertEqual(self.chat.st.get("model"), "gemini/gemini-deep-research")
            self.assertEqual(len(enqueued), 4)
        finally:
            self.chat.enqueue = old_enqueue




