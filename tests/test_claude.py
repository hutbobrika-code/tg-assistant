"""Model/effort picker, the Claude Code backend and premium emoji (no network)."""
import time
import unittest

from fakes import Reply, is_live, message, wait_for, wait_idle
from test_flows import FlowBase, job
import bot

OPUS = "claude/claude-opus-5-5"


def screens(tg):
    return [p["rich_message"]["markdown"] for _, p, _ in tg.sent("editMessageText") if not is_live(p)]


def buttons(params):
    return [b for row in params["reply_markup"]["inline_keyboard"] for b in row]


class PickerTest(FlowBase):
    claude_available = True

    def test_models_listed_claude_first(self):
        md, kb = bot.models_view(self.chat)
        names = [b["text"] for row in kb["inline_keyboard"] for b in row]
        self.assertTrue(names[0].startswith("✴️ Opus 5.5"))
        self.assertIn("🔹 Muse Spark", names)
        self.assertIn("по вашей подписке", md)

    def test_tap_model_then_level(self):
        lst = bot.models()
        i = next(k for k, m in enumerate(lst) if m["id"] == OPUS)
        self.assertIsNone(bot.on_button(self.chat, 50, f"m:{i}"))
        md = screens(self.tg)[-1]
        self.assertIn("## 🧠 ✴️ Claude Opus 5.5", md)
        kb = self.tg.sent("editMessageText")[-1][1]["reply_markup"]["inline_keyboard"]
        labels = [r[0]["text"] for r in kb[:-1]]
        self.assertEqual(labels[0], "▰▱▱▱▱ Низкий — быстро, для простого")
        self.assertEqual(labels[-1], "▰▰▰▰▰ Максимум — предельно тщательно, дольше всего")
        self.assertTrue(labels[3].startswith("✓ "))  # xhigh is the default
        note = bot.on_button(self.chat, 50, f"e:{i}:max")
        self.assertEqual(self.chat.st["model"], OPUS)
        self.assertEqual(bot.effort_for(self.chat.st), "max")
        self.assertIn("Opus 5.5 · Максимум", note)
        self.assertIn("свои диалоги", note)  # switched from the free engine
        self.assertIn("### ✴️ Claude Opus 5.5", screens(self.tg)[-1])

    def test_level_is_remembered_per_model(self):
        lst = bot.models()
        opus = next(k for k, m in enumerate(lst) if m["id"] == OPUS)
        muse = next(k for k, m in enumerate(lst) if m["id"] == "opencode/muse")
        bot.on_button(self.chat, 50, f"e:{opus}:low")
        bot.on_button(self.chat, 50, f"e:{muse}:high")
        self.assertEqual(bot.effort_for(self.chat.st, OPUS), "low")
        self.assertEqual(bot.effort_for(self.chat.st, "opencode/muse"), "high")

    def test_model_without_levels_applies_at_once(self):
        lst = bot.models()
        i = next(k for k, m in enumerate(lst) if m["id"] == "claude/claude-haiku-4-5")
        self.assertEqual(bot.on_button(self.chat, 50, f"m:{i}"), "Haiku 4.5\nУ Claude и бесплатных моделей свои "
                                                                 "диалоги — контекст не переносится")
        self.assertIsNone(bot.effort_for(self.chat.st))

    def test_reasoning_button_opens_levels_of_current_model(self):
        self.chat.st["model"] = OPUS
        bot.on_button(self.chat, 50, "reasoning")
        self.assertIn("Выберите глубину рассуждений", screens(self.tg)[-1])

    def test_claude_hidden_when_not_connected(self):
        self.cc.is_available = False
        self.assertFalse(any(m["id"].startswith("claude/") for m in bot.models()))
        md, _ = bot.models_view(self.chat)
        self.assertIn("не подключён", md)


class ClaudeRunTest(FlowBase):
    claude_available = True

    def setUp(self):
        super().setUp()
        self.chat.st["model"] = OPUS
        self.chat.st["efforts"] = {OPUS: "max"}

    def test_answer_and_dialog_chain(self):
        self.cc.replies = [Reply(("think", 0.05, 5), ("text", "Первый ответ")), Reply(("text", "Второй ответ")),
                           Reply(("text", "Третий"))]
        self.run_job("Вопрос про звёзды", 1)
        s = self.cc.starts[0]
        self.assertEqual((s["model"], s["effort"], s["resume"], s["plan"]), ("claude-opus-5-5", "max", None, False))
        final = self.finals()[0]
        self.assertIn("Первый ответ", final)
        self.assertTrue(final.endswith("<footer>✴️ Opus 5.5 · максимум · 🧠 Думал 5 с</footer>"))
        cl = self.chat.st["claude"]
        self.assertEqual((cl["sid"], cl["prev"], cl["title"]), ("cs-001", None, "Вопрос про звёзды"))
        self.run_job("ещё", 2)
        self.assertEqual(self.cc.starts[1]["resume"], "cs-001")
        self.assertEqual((cl["sid"], cl["prev"]), ("cs-002", "cs-001"))
        self.run_job("и ещё", 3)
        self.assertEqual(self.cc.forgotten, ["cs-001"])  # two turns back is not needed for regenerate
        self.assertEqual(self.chat.context, 4321)
        self.assertEqual(bot.limits_text(), "24% за 5 ч · 16% за неделю")

    def test_live_message_shows_model(self):
        self.cc.replies = [Reply(("sleep", 2), ("text", "ок"))]
        self.run_job("вопрос")
        self.assertIn("<footer>✴️ Opus 5.5 · максимум</footer>", self.tg.live()[0])

    def test_regenerate_forks_from_same_point(self):
        self.cc.replies = [Reply(("text", "раз")), Reply(("text", "два")), Reply(("text", "два, другой вариант"))]
        self.run_job("первый", 1)
        self.run_job("второй", 2)
        self.assertEqual(bot.on_button(self.chat, self.chat.st["last_answer"], "regen"), "Генерирую заново")
        wait_idle(self.chat)
        self.assertEqual(self.cc.starts[2]["resume"], "cs-001")
        self.assertEqual(self.cc.starts[2]["parts"], self.cc.starts[1]["parts"])
        cl = self.chat.st["claude"]
        self.assertEqual((cl["sid"], cl["prev"]), ("cs-003", "cs-001"))
        self.assertIn("cs-002", self.cc.forgotten)  # the replaced answer
        self.assertIn("два, другой вариант", self.finals()[-1])

    def test_stop(self):
        self.cc.replies = [Reply(("text", "слово " * 300), delay=0.05, chunk=6)]
        self.chat.enqueue(job("эссе"))
        wait_for(lambda: self.tg.live_ids() and self.chat.run and self.chat.run.snapshot())
        self.assertEqual(bot.on_button(self.chat, self.tg.live_ids()[0], "stop"), "Останавливаю…")
        wait_idle(self.chat)
        self.assertEqual(self.cc.stops, 1)
        final = self.finals()[-1]
        self.assertIn("слово", final)
        self.assertIn("✋ *Остановлено*", final)

    def test_tools_render_like_opencode(self):
        self.cc.replies = [Reply(("tool", "bash", {"command": "ls", "description": "Смотрю папку"}),
                                 ("text", "Готово"))]
        self.run_job("что тут?")
        self.assertIn("- 💻 Смотрю папку", self.finals()[0])

    def test_subscription_ended(self):
        self.cc.replies = [Reply(("error", "OAuth token has expired. Please run /login"))]
        self.run_job("вопрос")
        final_params = [p for _, p, _ in self.tg.sent("sendRichMessage") if not is_live(p)][-1]
        self.assertIn("закончилась подписка", final_params["rich_message"]["markdown"])
        self.assertIn("fallback", [b["callback_data"] for b in buttons(final_params)])
        bot.on_button(self.chat, 9, "fallback")
        self.assertEqual(bot.engine_of(self.chat.st["model"]), "opencode")
        self.assertIn("Переключил на", self.finals()[-1])
        self.assertNotIn("sid", self.chat.st.get("claude", {}))  # the failed fork is not kept

    def test_not_connected(self):
        self.cc.is_available = False
        self.run_job("вопрос")
        self.assertIn("Claude не подключён", self.finals()[0])
        self.assertEqual(self.cc.starts, [])
        self.assertEqual(self.tg.reactions()[-1], (1, []))

    def test_plan_mode(self):
        self.chat.st["agent"] = "plan"
        self.run_job("вопрос")
        self.assertTrue(self.cc.starts[0]["plan"])


class DialogsTest(FlowBase):
    claude_available = True

    def test_both_engines_listed_and_switch(self):
        self.run_job("бесплатный вопрос", 1)  # an OpenCode dialog
        oc_sid = self.chat.st["session"]
        self.chat.st["model"] = OPUS
        self.run_job("вопрос для Claude", 2)
        md, kb = bot.sessions_view(self.chat)
        labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
        self.assertTrue(any(t.startswith("✓ ✴️ вопрос для Claude") for t in labels))
        self.assertTrue(any(t.startswith("🔹 ") for t in labels))
        bot.on_button(self.chat, 60, f"s:{oc_sid}")
        self.assertEqual(bot.engine_of(self.chat.st["model"]), "opencode")
        self.assertIn("🔹", screens(self.tg)[-1])
        bot.on_button(self.chat, 60, "cd:cur")
        self.assertEqual(self.chat.st["model"], OPUS)

    def test_new_dialog_archives_claude(self):
        self.chat.st["model"] = OPUS
        self.run_job("первый диалог")
        bot.new_dialog(self.chat)
        self.assertEqual(self.chat.st["claude"], {})
        self.assertEqual(self.chat.st["claude_dialogs"][0]["title"], "первый диалог")
        self.run_job("второй диалог", 2)
        self.assertIsNone(self.cc.starts[1]["resume"])
        bot.on_button(self.chat, 60, "cd:0")
        self.assertEqual(self.chat.st["claude"]["title"], "первый диалог")
        self.assertEqual(self.chat.st["claude_dialogs"][0]["title"], "второй диалог")


class PremiumTest(FlowBase):
    def test_owner_without_premium_gets_plain_emoji(self):
        bot.handle_message(message("привет", mid=3))
        wait_idle(self.chat)
        self.assertFalse(bot.PREMIUM["on"])
        self.assertTrue(all("<tg-emoji" not in d for d in self.tg.live() + self.finals()))

    def test_stripped_custom_emoji_are_switched_off(self):
        bot.handle_message(message("привет", mid=3, **{"from": {"id": 42, "is_premium": True}}))
        wait_idle(self.chat)
        self.assertTrue(bot.PREMIUM["on"])
        self.assertTrue(bot.PREMIUM["broken"])  # the fake echoes no custom_emoji entities
        self.assertEqual(bot.premiumize("💭"), "💭")  # plain until the retry window passes
        bot.PREMIUM["broken"] = time.time() - bot.PREMIUM_RETRY - 1
        self.assertIn("<tg-emoji", bot.premiumize("💭"))
        self.assertIn("<tg-emoji", self.tg.live()[0])
        self.assertNotIn("<tg-emoji", self.finals()[-1])

    def test_custom_emoji_kept_when_telegram_allows(self):
        self.tg.echo = {"rich_message": {"blocks": [{"type": "paragraph", "text": [{"type": "custom_emoji"}]}]}}
        bot.handle_message(message("привет", mid=3, **{"from": {"id": 42, "is_premium": True}}))
        wait_idle(self.chat)
        self.assertFalse(bot.PREMIUM["broken"])
        self.assertIn('<tg-emoji emoji-id="5535034915403333642">💭</tg-emoji>', self.tg.live()[0])


if __name__ == "__main__":
    unittest.main()
