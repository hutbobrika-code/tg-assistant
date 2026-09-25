import os
import tempfile
import unittest
from pathlib import Path

from assistant import llm, models
from assistant.config import settings
from assistant.markdown import prepare, split_long, thinking_title, without_followups
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

    def test_thinking_title(self):
        self.assertEqual(thinking_title("**Читаю фото**\n\nтекст\n\n**Считаю площадь**\n\nещё"),
                         "Считаю площадь")


class TestModels(unittest.TestCase):
    def test_effort_fallback(self):
        ling = models.BY_ID["ling-3.0-flash-fin-free"]
        self.assertEqual(models.effort_for(ling, "xhigh"), "high")
        self.assertEqual(models.effort_for(ling, "minimal"), "low")
        self.assertIsNone(models.effort_for(models.BY_ID["big-pickle"], "high"))


class TestLLM(unittest.TestCase):
    def test_responses_body(self):
        muse = models.get("muse-spark-1.3-contributor-free")
        msgs = [{"role": "user", "text": "что тут?",
                 "files": [{"mime": "image/png", "name": "a.png", "data": b"x"}]},
                {"role": "assistant", "text": "картинка"}]
        body = llm.responses_body(muse, "sys", msgs, "xhigh")
        self.assertEqual(body["reasoning"], {"effort": "xhigh", "summary": "auto"})
        self.assertEqual(body["input"][0]["content"][1]["type"], "input_image")
        self.assertEqual(body["input"][1]["content"][0]["type"], "output_text")

    def test_parse_streams(self):
        delta = '{"type":"response.output_text.delta","delta":"hi"}'
        self.assertEqual(list(llm.parse_responses(None, delta)), [("text", "hi")])
        chunk = '{"choices":[{"delta":{"reasoning_content":"хм","content":"да"}}]}'
        self.assertEqual(list(llm.parse_chat(None, chunk)), [("thinking", "хм"), ("text", "да")])
        with self.assertRaises(llm.LLMError):
            list(llm.parse_responses(None, '{"type":"error","error":{"message":"плохо"}}'))


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
