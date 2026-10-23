"""End-to-end bot flows against scripted Telegram and Gemini fakes (no network)."""
import time
import unittest

from fakes import (USER, Reply, fresh_chat, message, use_fake, use_fake_claude, use_fake_gemini, wait_for,
                   wait_idle)
import bot
import fakes


def job(text, mid=1):
    return {"parts": [{"type": "text", "text": text}], "reply_to": mid}


class FlowBase(unittest.TestCase):
    live_delay = 0.0

    claude_available = False

    def setUp(self):
        self.tg = use_fake(self.live_delay)
        self.gem = use_fake_gemini()
        self.cc = use_fake_claude(self.claude_available)
        bot.PREMIUM.update(on=False, broken=0)
        bot.REACTIONS.clear()  # workers from earlier tests may still be stuck in a hung fake call
        bot.CLAUDE_LIMITS.clear()
        self.chat = fresh_chat()

    def run_job(self, text, mid=1, timeout=10):
        self.chat.enqueue(job(text, mid))
        wait_idle(self.chat, timeout)

    def finals(self):
        return self.tg.finals()

    def live(self):
        return self.tg.live()

    def slow_start(self, seconds=0.5):
        """The router check before a turn hangs, so the job sits between the queue and the model."""
        self.gem.available = lambda: time.sleep(seconds) or True

class AnswerTest(FlowBase):
    def test_simple_answer(self):
        self.gem.replies = [Reply(("text", "Париж — столица Франции."))]
        self.run_job("Столица Франции?")
        self.assertEqual(len(self.finals()), 1)
        self.assertIn("Париж — столица Франции.", self.finals()[0])
        self.assertNotIn("<details>", self.finals()[0])
        # reaction: 👀 while working, cleared afterwards
        self.assertEqual(self.tg.reactions(), [(1, ["👀"]), (1, [])])

    def test_prompt_never_shown_as_answer_in_progress(self):
        self.gem.replies = [Reply(("sleep", 1.5), ("text", "Ответ модели"))]
        self.run_job("СЕКРЕТНЫЙ ВОПРОС ПОЛЬЗОВАТЕЛЯ")
        self.assertTrue(self.live())
        for d in self.live():
            self.assertNotIn("СЕКРЕТНЫЙ", d)
        self.assertNotIn("СЕКРЕТНЫЙ", self.finals()[0])

    def test_progress_shows_status_then_text(self):
        self.gem.replies = [Reply(("sleep", 1.2), ("text", "Длинный ответ " * 60), delay=0.05)]
        self.run_job("вопрос")
        live = self.live()
        self.assertRegex(live[0], r"^[◐◓◑◒] 💭 \*\*Думаю\*\* · \d+ с\n\n<footer>♊ 4 Argon · средний")
        self.assertTrue(any("✍️ **Пишу ответ**" in d and "Длинный ответ" in d for d in live))

    def test_progress_message_lifecycle(self):
        self.gem.replies = [Reply(("sleep", 2), ("text", "ответ"))]
        self.run_job("вопрос")
        first = self.tg.sent("sendRichMessage")[0][1]
        self.assertTrue(first["disable_notification"])
        self.assertEqual(first["reply_markup"], bot.STOP_KB)
        self.assertGreaterEqual(len(self.live()), 2)  # at least one edit
        live_id = self.tg.live_ids()[0]
        final_at = self.tg.call_index("sendRichMessage", lambda p: not fakes.is_live(p))[0]
        deleted_at = self.tg.call_index("deleteMessage", lambda p: p["message_id"] == live_id)
        self.assertEqual(len(deleted_at), 1)
        self.assertGreater(deleted_at[0], final_at)  # removed only after the answer is out
        self.assertEqual(self.tg.sent("sendRichMessageDraft"), [])

    def test_steps_quote_newest_first(self):
        self.gem.replies = [Reply(("tool", "bash", {"command": "ls"}), ("sleep", 0.3),
                                 ("tool", "read", {"filePath": str(bot.WORKDIR / "a.txt")}), ("sleep", 2),
                                 ("text", "ок"))]
        self.run_job("вопрос")
        quoted = [d for d in self.live() if "<blockquote expandable>" in d][-1]
        body = quoted.split("<blockquote expandable>")[1]
        self.assertLess(body.index("📖 Читаю <code>a.txt</code>"), body.index("💻 Выполняю <code>ls</code>"))
        self.assertIn("· 2 шага", quoted)

    def test_thinking_time_shown(self):
        self.gem.replies = [Reply(("think", 0.1, 12), ("text", "ответ без инструментов"))]
        self.run_job("вопрос")
        self.assertTrue(self.finals()[0].endswith(" · 🧠 Думал 12 с</footer>"))
        self.gem.replies = [Reply(("think", 0.1, 6), ("tool", "bash", {"command": "ls"}), ("text", "готово"))]
        self.run_job("ещё", mid=2)
        final = self.finals()[-1]
        self.assertIn("- 🧠 Думал 6 с", final)
        self.assertNotIn("Думал 6 с</footer>", final)

    def test_short_thinking_not_shown(self):
        self.gem.replies = [Reply(("think", 0.1, 1), ("text", "быстро"))]
        self.run_job("вопрос")
        self.assertNotIn("Думал", self.finals()[0])

    def test_user_deleted_progress_message(self):
        self.tg.edit_error = "Bad Request: message to edit not found"
        self.gem.replies = [Reply(("sleep", 4), ("text", "ответ"))]
        self.run_job("вопрос")
        self.assertEqual(len(self.tg.sent("editMessageText")), 1)  # gave up after the first failure
        self.assertIn("ответ", self.finals()[0])

    def test_tools_folded_into_details(self):
        self.gem.replies = [Reply(("text", "Сейчас посмотрю файлы"),
                                 ("tool", "bash", {"command": "ls -la", "description": "Смотрю файлы"}),
                                 ("tool", "read", {"filePath": str(bot.WORKDIR / "a.txt")}),
                                 ("text", "Готово: 3 файла"))]
        self.run_job("что в папке?")
        final = self.finals()[0]
        self.assertTrue(final.startswith("<details><summary>⚙️ 2 шага · "))
        self.assertIn("- 💭 _Сейчас посмотрю файлы_", final)
        self.assertIn("- 💻 Смотрю файлы", final)
        self.assertIn("- 📖 Читаю `a.txt`", final)
        self.assertIn("Готово: 3 файла\n\n<footer>", final)

    def test_model_error_shown(self):
        self.gem.replies = [Reply(("text", "Начало"), ("error", "Rate limit exceeded"))]
        self.run_job("вопрос")
        self.assertIn("⚠️ Rate limit exceeded", self.finals()[0])
        self.assertIn("Начало", self.finals()[0])

    def test_answer_has_buttons_and_old_ones_are_removed(self):
        self.run_job("раз", mid=1)
        self.run_job("два", mid=2)
        rich = self.tg.sent("sendRichMessage")
        kb = rich[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertEqual([b["callback_data"] for b in kb[0]], ["regen", "new"])
        first_id = self.tg.final_ids()[0]
        edits = [p for _, p, _ in self.tg.sent("editMessageReplyMarkup") if p["message_id"] == first_id]
        self.assertEqual(edits[-1]["reply_markup"], {"inline_keyboard": []})

    def test_dialog_is_recorded(self):
        self.gem.replies = [Reply(("text", "первый ответ")), Reply(("text", "второй ответ"))]
        self.run_job("раз")
        self.run_job("два", mid=2)
        self.assertEqual(self.gem.starts[1]["dialog"], [{"role": "user", "content": "раз"},
                                                        {"role": "assistant", "content": "первый ответ"}])
        self.assertEqual(len(self.chat.st["gemini"]["messages"]), 4)


class SlowProgressTest(FlowBase):
    live_delay = 30.0  # a Telegram request for the progress message hangs

    def test_hung_progress_does_not_delay_answer(self):
        self.gem.replies = [Reply(("text", "быстрый ответ"))]
        t = time.time()
        self.run_job("вопрос", timeout=8)
        self.assertLess(time.time() - t, 3)
        self.assertIn("быстрый ответ", self.finals()[0])

    def test_late_progress_message_is_deleted(self):
        self.gem.replies = [Reply(("text", "ответ"))]
        self.tg.live_delay = 1.0
        self.run_job("вопрос")
        wait_for(lambda: self.tg.sent("deleteMessage"), timeout=4)
        deleted = [p["message_id"] for _, p, _ in self.tg.sent("deleteMessage")]
        self.assertEqual(deleted, self.tg.live_ids())


class HungTelegramCallsTest(FlowBase):
    def test_hung_reaction_does_not_delay_answer(self):
        self.tg.delays["setMessageReaction"] = 30
        self.gem.replies = [Reply(("text", "ответ"))]
        t = time.time()
        self.chat.enqueue(job("вопрос"))
        wait_for(lambda: self.finals(), timeout=5)
        self.assertLess(time.time() - t, 3)

    def test_hung_cleanup_calls_do_not_block_next_job(self):
        self.tg.delays.update(deleteMessage=30, editMessageReplyMarkup=30)
        t = time.time()
        self.chat.enqueue(job("раз", 1))
        self.chat.enqueue(job("два", 2))
        wait_for(lambda: len(self.finals()) == 2, timeout=6)
        self.assertLess(time.time() - t, 4)

    def test_reactions_keep_their_order(self):
        self.tg.delays["setMessageReaction"] = 0.3
        self.run_job("вопрос")
        self.assertEqual(self.tg.reactions()[-1], (1, []))  # 👀 was set, then cleared


class StopTest(FlowBase):
    def test_stop_keeps_partial_text(self):
        self.gem.replies = [Reply(("text", "слово " * 400), delay=0.05, chunk=6)]
        self.chat.enqueue(job("эссе"))
        wait_for(lambda: any("слово" in d for d in self.live()))
        running, dropped = self.chat.stop()
        self.assertTrue(running)
        wait_idle(self.chat)
        final = self.finals()[0]
        self.assertIn("слово", final)
        self.assertIn("✋ *Остановлено*", final)
        self.assertEqual(self.gem.stops, 1)

    def test_stop_button(self):
        self.gem.replies = [Reply(("text", "слово " * 400), delay=0.05, chunk=6)]
        self.chat.enqueue(job("эссе"))
        wait_for(lambda: self.tg.live_ids())
        live_id = self.tg.live_ids()[0]
        self.assertEqual(bot.on_button(self.chat, live_id, "stop"), "Останавливаю…")
        wait_idle(self.chat)
        self.assertIn("Остановлено", self.finals()[0])
        self.assertEqual(bot.on_button(self.chat, live_id, "stop"), "Уже готово")

    def test_stop_before_model_started(self):
        self.slow_start()
        self.chat.enqueue(job("вопрос"))
        time.sleep(0.2)
        self.assertTrue(self.chat.busy())
        self.assertEqual(self.chat.stop(), (True, 0))
        wait_idle(self.chat)
        self.assertEqual(self.gem.starts, [])
        self.assertEqual(self.tg.reactions()[-1], (1, []))

    def test_message_during_startup_is_queued(self):
        self.slow_start()
        self.chat.enqueue(job("первый", 1))
        time.sleep(0.2)
        self.chat.enqueue(job("второй", 2))
        wait_idle(self.chat)
        self.assertIn((2, ["👌"]), self.tg.reactions())
        self.assertEqual(len(self.gem.starts), 2)

    def test_stop_drops_queue(self):
        self.gem.replies = [Reply(("sleep", 5)), Reply(("text", "второй"))]
        self.chat.enqueue(job("первый", 1))
        self.chat.enqueue(job("второй", 2))
        wait_for(lambda: self.chat.run is not None)
        running, dropped = self.chat.stop()
        self.assertEqual((running, dropped), (True, 1))
        wait_idle(self.chat)
        self.assertEqual(len(self.gem.starts), 1)
        self.assertIn((2, []), self.tg.reactions())  # queued mark removed


class QueueTest(FlowBase):
    def test_messages_answered_in_order(self):
        self.gem.replies = [Reply(("text", "ответ А"), ("sleep", 0.5)), Reply(("text", "ответ Б"))]
        self.chat.enqueue(job("А", 1))
        self.chat.enqueue(job("Б", 2))
        wait_idle(self.chat)
        finals = self.finals()
        self.assertIn("ответ А", finals[0])
        self.assertIn("ответ Б", finals[1])
        self.assertIn((2, ["👌"]), self.tg.reactions())
        # second answer quotes its question because another message came in between
        rich = [p for m, p, _ in self.tg.sent("sendRichMessage") if not fakes.is_live(p)]
        self.assertEqual(rich[1]["reply_parameters"]["message_id"], 2)


class RegenAndDialogTest(FlowBase):
    def test_regenerate_drops_last_turn_and_resends(self):
        self.gem.replies = [Reply(("text", "первый вариант")), Reply(("text", "второй вариант"))]
        self.run_job("вопрос")
        note = bot.on_button(self.chat, self.chat.st["last_answer"], "regen")
        self.assertEqual(note, "Генерирую заново")
        wait_idle(self.chat)
        self.assertEqual(self.gem.starts[1]["parts"], self.gem.starts[0]["parts"])
        self.assertEqual(self.gem.starts[1]["dialog"], [])
        self.assertIn("второй вариант", self.finals()[-1])
        self.assertEqual([m["content"] for m in self.chat.st["gemini"]["messages"]], ["вопрос", "второй вариант"])

    def test_regenerate_only_latest(self):
        self.run_job("раз")
        self.assertEqual(bot.on_button(self.chat, 5, "regen"), "Повторить можно только последний ответ")

    def test_regenerate_refused_after_new_dialog(self):
        self.run_job("раз")
        last = self.chat.st["last_answer"]
        bot.new_dialog(self.chat)
        self.assertEqual(bot.on_button(self.chat, last, "regen"), "Повторить можно только последний ответ")

    def test_new_dialog(self):
        self.run_job("раз")
        self.assertEqual(len(self.chat.st["gemini"]["messages"]), 2)
        bot.on_button(self.chat, self.chat.st["last_answer"], "new")
        self.assertEqual(self.chat.st["gemini"]["messages"], [])
        self.assertIn("Новый диалог", self.finals()[-1])
        self.run_job("два", mid=3)
        self.assertEqual(self.gem.starts[-1]["dialog"], [])

    def test_old_opencode_model_moves_to_gemini(self):
        self.chat.st["model"] = "opencode/muse-spark-1.3-contributor-free"
        self.run_job("раз")
        self.assertEqual(self.gem.starts[0]["model"], bot.DEFAULT_MODEL)


class FailureTest(FlowBase):
    def test_gemini_down(self):
        self.gem.is_available = False
        self.run_job("вопрос")
        self.assertIn("Gemini роутер недоступен", self.finals()[0])
        self.assertEqual(self.gem.starts, [])
        self.assertEqual(self.tg.reactions()[-1], (1, []))

    def test_rich_failure_falls_back_to_html(self):
        self.tg.fail.add("sendRichMessage")
        self.gem.replies = [Reply(("text", "**жирный** ответ"))]
        self.run_job("вопрос")
        html = [p for _, p, _ in self.tg.sent("sendMessage")]
        self.assertEqual(html[0]["parse_mode"], "HTML")
        self.assertIn("<b>жирный</b>", html[0]["text"])
        self.assertEqual(self.tg.reactions()[-1], (1, []))

    def test_callback_always_answered(self):
        orig = bot.on_button
        bot.on_button = lambda *a: 1 / 0
        try:
            bot.handle_callback({"id": "cb1", "from": {"id": USER}, "data": "settings",
                                 "message": {"message_id": 5, "chat": {"id": USER, "type": "private"}}})
        finally:
            bot.on_button = orig
        answers = self.tg.sent("answerCallbackQuery")
        self.assertEqual(answers[-1][1]["callback_query_id"], "cb1")


class FilesTest(FlowBase):
    def test_created_files_get_buttons(self):
        self.gem.replies = [Reply(("file", "report.csv", b"a,b\n1,2"), ("text", "Сохранил отчёт"))]
        self.run_job("сделай отчёт")
        kb = self.tg.sent("sendRichMessage")[-1][1]["reply_markup"]["inline_keyboard"]
        self.assertEqual(kb[1][0]["text"], "📎 report.csv")
        bot.on_button(self.chat, 1, kb[1][0]["callback_data"])
        docs = self.tg.sent("sendDocument")
        self.assertEqual(docs[-1][2]["document"].name, "report.csv")

    def test_image_embedded_not_duplicated_as_button(self):
        self.gem.replies = [Reply(("file", "plot.png", b"\x89PNG"), ("text", "Вот:\n![График](plot.png)"))]
        self.run_job("график")
        method, params, files = self.tg.sent("sendRichMessage")[-1]
        self.assertEqual(files, {"m1": bot.WORKDIR / "plot.png"})
        self.assertEqual(params["rich_message"]["media"][0]["media"]["type"], "photo")
        names = [b["text"] for row in params["reply_markup"]["inline_keyboard"] for b in row]
        self.assertNotIn("📎 plot.png", names)


class IncomingTest(FlowBase):
    def test_voice_is_declined(self):
        bot.process_messages(self.chat, [message(mid=3, voice={"file_id": "v1", "mime_type": "audio/ogg"})])
        self.assertIn("Голосовые пока не понимаю", self.finals()[-1])
        self.assertEqual(self.gem.starts, [])

    def test_sticker_sent_as_emoji(self):
        bot.process_messages(self.chat, [message(mid=3, sticker={"emoji": "😂", "file_id": "s"})])
        wait_idle(self.chat)
        self.assertEqual(self.gem.starts[0]["parts"][0]["text"], "😂")

    def test_photo_becomes_image_part(self):
        bot.process_messages(self.chat, [message(mid=3, caption="что тут?",
                                                 photo=[{"file_id": "p1", "file_unique_id": "abcdef1"}])])
        wait_idle(self.chat)
        parts = self.gem.starts[0]["parts"]
        self.assertTrue(parts[0]["text"].startswith("что тут?"))
        self.assertEqual(parts[1]["mime"], "image/jpeg")
        self.assertTrue(parts[1]["url"].startswith("file://" + str(bot.UPLOADS)))

    def test_pdf_and_zip(self):
        bot.process_messages(self.chat, [message(mid=3, document={"file_id": "d1", "file_name": "a.pdf",
                                                                  "mime_type": "application/pdf"})])
        bot.process_messages(self.chat, [message(mid=4, caption="распакуй",
                                                 document={"file_id": "d2", "file_name": "b.zip",
                                                           "mime_type": "application/zip"})])
        wait_idle(self.chat)
        pdf, zipjob = self.gem.starts
        self.assertEqual(pdf["parts"][1]["mime"], "application/pdf")
        self.assertEqual(pdf["parts"][0]["text"].split("\n")[0], "Посмотри вложение.")
        self.assertEqual(len(zipjob["parts"]), 1)
        self.assertIn("b.zip", zipjob["parts"][0]["text"])

    def test_album_is_one_prompt(self):
        for i in range(3):
            bot.handle_message(message(mid=10 + i, media_group_id="g1", caption="сравни" if i == 0 else None,
                                       photo=[{"file_id": f"p{i}", "file_unique_id": f"u{i}xxxxx"}]))
        wait_for(lambda: self.gem.starts, timeout=5)
        wait_idle(self.chat)
        self.assertEqual(len(self.gem.starts), 1)
        self.assertEqual(len(self.gem.starts[0]["parts"]), 4)

    def test_reply_quote_included(self):
        bot.process_messages(self.chat, [message("а это что?", mid=3, quote={"text": "строка из ответа"},
                                                 reply_to_message={"message_id": 2, "text": "весь ответ"})])
        wait_idle(self.chat)
        self.assertTrue(self.gem.starts[0]["parts"][0]["text"].startswith("> строка из ответа\n\nа это что?"))

    def test_stranger_denied(self):
        m = message("привет", mid=3)
        m["from"] = {"id": 999}
        bot.handle_message(m)
        self.assertIn("Нет доступа", self.tg.texts("sendMessage")[-1])
        self.assertEqual(self.gem.starts, [])


class SettingsTest(FlowBase):
    def test_start_and_examples(self):
        bot.process_messages(self.chat, [message("/start")])
        md = self.finals()[-1]
        self.assertIn("# 👋 Привет!", md)
        self.assertIn("4 Argon", md)
        bot.on_button(self.chat, 1, "x:1")
        wait_idle(self.chat)
        self.assertIn("Сравни Python, Go и Rust", self.gem.starts[0]["parts"][0]["text"])

    def test_effort_and_model(self):
        bot.on_button(self.chat, 50, "e:low")
        self.run_job("раз")
        s = self.gem.starts[-1]
        self.assertEqual((s["model"], s["effort"]), (bot.DEFAULT_MODEL, "low"))
        plain = next(i for i, m in enumerate(bot.models()) if not m["variants"])
        bot.on_button(self.chat, 50, f"m:{plain}")
        self.run_job("два", mid=2)
        s = self.gem.starts[-1]
        self.assertEqual((s["model"], s["effort"]), (bot.models()[plain]["id"], None))
        screen = [p["rich_message"]["markdown"] for _, p, _ in self.tg.sent("editMessageText")][-1]
        self.assertIn(f"### ♊ {bot.models()[plain]['name']}", screen)

    def test_gemini_effort_defaults_to_medium(self):
        self.chat.st["effort"] = "max"
        self.assertEqual(bot.effort_for(self.chat.st), "medium")

    def test_only_gemini_models_offered(self):
        self.assertTrue(all(m["id"].startswith("gemini/") for m in bot.models()))
        md, _ = bot.models_view(self.chat)
        self.assertNotIn("OpenCode", md)

    def test_settings_screen_buttons(self):
        md, kb = bot.settings_view(self.chat)
        self.assertIn("**Рассуждения:** ▰▰▰▱▱ Средний", md)
        self.assertNotIn("Режим", md)
        datas = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
        for d in ("models", "reasoning", "sessions", "files", "new", "close"):
            self.assertIn(d, datas)
        self.assertNotIn("a:plan", datas)

    def test_stop_command_when_idle(self):
        bot.process_messages(self.chat, [message("/stop")])
        self.assertIn("ничего не выполняется", self.finals()[-1])


if __name__ == "__main__":
    unittest.main()
