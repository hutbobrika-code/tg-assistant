import os
import tempfile
import unittest
from pathlib import Path

from assistant import llm, models
from assistant.config import settings
from assistant.markdown import (
    drop_images,
    prepare,
    split_long,
    thinking_title,
    web_images,
    without_followups,
)
from assistant.store import Store


class TestMarkdown(unittest.TestCase):
    def test_latex_delimiters(self):
        md, _, _ = prepare("формула \\(x^2\\) и\n\\[ E = mc^2 \\]")
        self.assertIn("$x^2$", md)
        self.assertIn("$$E = mc^2$$", md)

    def test_stray_lt_escaped_but_not_in_code(self):
        md, _, _ = prepare("a < b, `a<b`, $x<y$, <b>ok</b>\n```py\nif a < b: pass\n```")
        self.assertIn("a &lt; b", md)
        self.assertIn("`a<b`", md)
        self.assertIn("$x<y$", md)
        self.assertIn("<b>ok</b>", md)
        self.assertIn("if a < b: pass", md)

    def test_followups(self):
        text = "ответ\n\n```followups\n- Первый\n2. Второй\n* Третий\n- Четвёртый\n```"
        md, _, followups = prepare(text)
        self.assertEqual(md, "ответ")
        self.assertEqual(followups, ["Первый", "Второй", "Третий"])
        self.assertEqual(without_followups(text), "ответ")

    def test_unclosed_block_while_streaming(self):
        md, _, _ = prepare("текст\n```followups\n- недо", final=False)
        self.assertEqual(md, "текст")
        md, _, _ = prepare("схема:\n```svg\n<svg", final=False)
        self.assertIn("рисую схему", md)

    def test_svg_becomes_picture(self):
        svg = '<svg width="100" height="50"><rect width="100" height="50" fill="red"/></svg>'
        md, images, _ = prepare(f"вот\n```svg\n{svg}\n```")
        self.assertIn("tg://photo?id=img1", md)
        self.assertTrue(images[0][1].startswith(b"\x89PNG"))

    def test_split_long_keeps_code_blocks(self):
        text = "абзац\n" * 50 + "```\n" + "код\n" * 50 + "```\n" + "хвост\n" * 50
        parts = split_long(text, limit=200)
        for part in parts:
            self.assertEqual(part.count("```") % 2, 0)
        self.assertEqual("\n".join(parts), text)

    def test_web_images(self):
        md = ("текст\n\n<tg-collage>\n\n![](https://a.ru/1.jpg)\n"
              "![](https://b.ru/2.jpg \"подпись\")\n\n</tg-collage>\n\n![](tg://photo?id=img1)")
        self.assertEqual(web_images(md), ["https://a.ru/1.jpg", "https://b.ru/2.jpg"])
        one = drop_images(md, ["https://a.ru/1.jpg"])
        self.assertNotIn("a.ru", one)
        self.assertIn("b.ru", one)
        none = drop_images(md)
        self.assertNotIn("tg-collage", none)
        self.assertIn("tg://photo?id=img1", none)

    def test_thinking_title(self):
        self.assertEqual(thinking_title("**Читаю фото**\n\nтекст\n\n**Считаю площадь**\n\nещё"),
                         "Считаю площадь")


class TestModels(unittest.TestCase):
    def test_effort_fallback(self):
        ling = models.BY_ID["ling-3.0-flash-fin-free"]
        self.assertEqual(models.effort_for(ling, "xhigh"), "high")
        self.assertEqual(models.effort_for(ling, "minimal"), "low")
        self.assertIsNone(models.effort_for(models.BY_ID["big-pickle"], "high"))


class FakePost:
    # подменяет запросы к модели: на каждый раунд свой список событий
    def __init__(self, rounds, reject_tools=False):
        self.rounds = list(rounds)
        self.bodies = []
        self.reject_tools = reject_tools

    def __call__(self, path, body, probe=False):
        self.bodies.append(body)
        if self.reject_tools and "tools" in body:
            raise llm.ToolsRejected("tools not supported")
        yield from self.rounds.pop(0)


class TestLLM(unittest.TestCase):
    def setUp(self):
        self.real_post = llm.post
        llm.no_tools.clear()

    def tearDown(self):
        llm.post = self.real_post

    def run_stream(self, model_id, rounds, **kw):
        fake = FakePost(rounds, kw.pop("reject_tools", False))
        llm.post = fake
        calls = []

        def tools(name, args):
            calls.append((name, args))
            return "результат поиска"
        events = list(llm.stream(models.get(model_id), "sys",
                                 [{"role": "user", "text": "курс доллара?"}], "high", None, tools))
        return events, calls, fake.bodies

    def test_responses_tool_round(self):
        call = {"type": "function_call", "call_id": "c1", "name": "web_search",
                "arguments": '{"query": "курс"}'}
        rounds = [
            [{"type": "response.reasoning_summary_text.delta", "delta": "надо поискать"},
             {"type": "response.output_item.done", "item": call},
             {"type": "response.completed", "response": {"output": [call]}}],
            [{"type": "response.output_text.delta", "delta": "84 ₽"}],
        ]
        events, calls, bodies = self.run_stream("muse-spark-1.3-contributor-free", rounds)
        self.assertEqual(calls, [("web_search", {"query": "курс"})])
        self.assertIn(("text", "84 ₽"), events)
        self.assertEqual(bodies[0]["reasoning"], {"effort": "high", "summary": "auto"})
        self.assertEqual(bodies[1]["input"][-1],
                         {"type": "function_call_output", "call_id": "c1",
                          "output": "результат поиска"})

    def test_chat_tool_round(self):
        rounds = [
            [{"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "t1",
                 "function": {"name": "web_search", "arguments": '{"qu'}}]}}]},
             {"choices": [{"delta": {"tool_calls": [
                 {"index": 0, "function": {"arguments": 'ery": "x"}'}}]}}]}],
            [{"choices": [{"delta": {"reasoning_content": "хм", "content": "готово"}}]}],
        ]
        events, calls, bodies = self.run_stream("space-bunny-free", rounds)
        self.assertEqual(calls, [("web_search", {"query": "x"})])
        self.assertEqual(events[-2:], [("thinking", "хм"), ("text", "готово")])
        self.assertEqual(bodies[1]["messages"][-1]["role"], "tool")

    def test_falls_back_without_tools(self):
        rounds = [[{"type": "response.output_text.delta", "delta": "без интернета"}]]
        events, calls, bodies = self.run_stream("muse-spark-1.3-contributor-free", rounds,
                                                reject_tools=True)
        self.assertEqual(events, [("text", "без интернета")])
        self.assertNotIn("tools", bodies[-1])
        self.assertIn("muse-spark-1.3-contributor-free", llm.no_tools)

    def test_images_in_input(self):
        msgs = [{"role": "user", "text": "что тут?",
                 "files": [{"mime": "image/png", "name": "a.png", "data": b"x"}]},
                {"role": "assistant", "text": "картинка"}]
        items = llm.responses_input(msgs)
        self.assertEqual(items[0]["content"][1]["type"], "input_image")
        self.assertEqual(items[1]["content"][0]["type"], "output_text")
        chat = llm.chat_messages("sys", msgs)
        self.assertEqual(chat[1]["content"][1]["type"], "image_url")

    def test_human_errors(self):
        self.assertIn("ключ", llm.human_error(llm.LLMError("bad", 401)))
        self.assertIn("перегружена", llm.human_error(llm.LLMError("slow", 429)))


class TestStore(unittest.TestCase):
    def test_encrypted_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = settings.state_dir, settings.state_key
            settings.state_dir, settings.state_key = Path(tmp), "test-key"
            try:
                store = Store()
                store.remember(1, {"role": "user", "text": "секрет"})
                store.save()
                raw = (Path(tmp) / "state.enc").read_bytes()
                self.assertNotIn("секрет".encode(), raw)
                self.assertEqual(Store().chat(1)["history"][0]["text"], "секрет")
            finally:
                settings.state_dir, settings.state_key = old


if __name__ == "__main__":
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    unittest.main()
