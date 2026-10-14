"""Live tests against the real OpenCode server and model; Telegram is faked.

Run on the server:  OC_LIVE=1 python3 -m unittest test_live
"""
import os
import threading
import unittest

from fakes import fresh_chat, is_live, use_fake, wait_for, wait_idle
import bot

QUESTION_LIVE = ("Используй инструмент question, чтобы спросить меня, какой язык выбрать: Python или Go. "
                 "После ответа одним предложением назови выбранный язык.")

LIVE = os.environ.get("OC_LIVE") == "1"
_events = None


@unittest.skipUnless(LIVE, "set OC_LIVE=1 to run against the real OpenCode server")
class LiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global _events
        if _events is None:
            _events = threading.Thread(target=bot.event_loop, daemon=True)
            _events.start()
        cls.sessions_before = {s["id"] for s in bot.oc.sessions()}

    @classmethod
    def tearDownClass(cls):
        for s in bot.oc.sessions():
            if s["id"] not in cls.sessions_before:
                bot.oc.req("DELETE", f"/session/{s['id']}")

    def setUp(self):
        self.tg = use_fake()
        self.chat = fresh_chat()
        self.chat.st["effort"] = "low"

    def ask(self, text, timeout=180):
        self.chat.enqueue({"parts": [{"type": "text", "text": text}], "reply_to": 1})
        wait_idle(self.chat, timeout)
        return self.final()

    def final(self):
        return [c for c in self.tg.sent("sendRichMessage") if not is_live(c[1])][-1]

    def test_simple_answer(self):
        _, params, _ = self.ask("Ответь одним словом без точки: столица Франции?")
        self.assertIn("Париж", params["rich_message"]["markdown"])
        for d in self.tg.live():
            self.assertNotIn("столица Франции", d)
        self.assertEqual(self.tg.reactions()[-1], (1, []))
        self.assertEqual([p["message_id"] for _, p, _ in self.tg.sent("deleteMessage")], self.tg.live_ids())

    def test_formula_and_table(self):
        _, params, _ = self.ask("Дай формулу площади круга и таблицу площадей для r = 1 и r = 2. Коротко.")
        md = params["rich_message"]["markdown"]
        self.assertRegex(md, r"\$[^$]*\\pi[^$]*\$")
        self.assertIn("|", md)
        self.assertNotIn("```", md)

    def test_image_is_embedded(self):
        _, params, files = self.ask(
            "Создай на Python (Pillow) картинку red.png 64x64 красного цвета в рабочей папке и покажи её в ответе.")
        self.assertTrue(files, params["rich_message"]["markdown"])
        self.assertEqual(params["rich_message"]["media"][0]["media"]["type"], "photo")
        self.assertTrue(params["rich_message"]["markdown"].startswith("<details><summary>⚙️"))

    def test_stop(self):
        self.chat.enqueue({"parts": [{"type": "text", "text": "Напиши подробное эссе на 2000 слов об истории чисел."}],
                           "reply_to": 1})
        wait_for(lambda: any("Пишу ответ" in d and len(d) > 400 for d in self.tg.live()), timeout=120)
        bot.on_button(self.chat, self.tg.live_ids()[0], "stop")
        wait_idle(self.chat, 60)
        md = self.final()[1]["rich_message"]["markdown"]
        self.assertIn("✋ *Остановлено*", md)
        self.assertGreater(len(md), 200)

    def test_question(self):
        self.chat.enqueue({"parts": [{"type": "text", "text": QUESTION_LIVE}], "reply_to": 1})
        wait_for(lambda: self.chat.question and self.chat.question["msg"], timeout=120)
        labels = [o["label"] for o in self.chat.question["items"][0]["options"]]
        go = next(i for i, label in enumerate(labels) if "Go" in label)
        bot.on_button(self.chat, self.chat.question["msg"], f"q:{go}")
        wait_idle(self.chat, 120)
        self.assertIn("Go", self.final()[1]["rich_message"]["markdown"])

    def test_regenerate(self):
        self.ask("Назови случайное число от 1 до 1000000. Только число.")
        sid = self.chat.st["session"]
        n_before = len(bot.oc.messages(sid))
        self.assertEqual(bot.on_button(self.chat, self.chat.st["last_answer"], "regen"), "Генерирую заново")
        wait_idle(self.chat, 120)
        self.assertEqual(len(bot.oc.messages(sid)), n_before)


@unittest.skipUnless(LIVE and bot.claude.available(), "needs OC_LIVE=1 and CLAUDE_CODE_OAUTH_TOKEN")
class LiveClaudeTest(unittest.TestCase):
    """Uses the real subscription: the cheapest model and short prompts."""

    def setUp(self):
        self.tg = use_fake()
        self.chat = fresh_chat()
        self.chat.st["model"] = "claude/claude-haiku-4-5"

    def tearDown(self):
        for d in [self.chat.st.get("claude") or {}] + list(self.chat.st.get("claude_dialogs") or []):
            for sid in (d.get("sid"), d.get("prev")):
                if sid:
                    bot.claude.forget(sid)

    def final(self):
        return [p for _, p, _ in self.tg.sent("sendRichMessage") if not is_live(p)][-1]["rich_message"]["markdown"]

    def test_answer_and_context(self):
        self.chat.enqueue({"parts": [{"type": "text", "text": "Запомни слово: ананас. Ответь одним словом: ок"}],
                           "reply_to": 1})
        wait_idle(self.chat, 180)
        self.chat.enqueue({"parts": [{"type": "text", "text": "Какое слово я просил запомнить? Одно слово."}],
                           "reply_to": 2})
        wait_idle(self.chat, 180)
        self.assertIn("нанас", self.final())
        self.assertIn("✴️ Haiku 4.5", self.final())


if __name__ == "__main__":
    unittest.main()
