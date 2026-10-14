import tempfile
import unittest
from pathlib import Path

import fakes  # noqa: F401  (sets up sys.path)
from render import md_to_html, prepare, split_long


class PrepareTest(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        (self.ws / "out").mkdir()
        (self.ws / "out" / "chart.png").write_bytes(b"\x89PNG fake")
        (self.ws / "report.pdf").write_bytes(b"%PDF")
        (self.ws / "my file.png").write_bytes(b"png")

    def md(self, text, upload=True):
        return prepare(text, self.ws, upload)

    def test_currency_dollars_are_escaped_math_kept(self):
        md, _ = self.md("Цена $5 и $10, формула $x^2$ и $$E=mc^2$$")
        self.assertEqual(md, "Цена \\$5 и \\$10, формула $x^2$ и $$E=mc^2$$")

    def test_price_range_is_not_math(self):
        self.assertEqual(self.md("от $5-$10")[0], "от \\$5-\\$10")

    def test_latex_delimiters_converted(self):
        md, _ = self.md(r"где \(a\) — ускорение, \(v_0 t\) и \[\int_0^1 f\,dx\]")
        self.assertEqual(md, r"где $a$ — ускорение, $v_0 t$ и $$\int_0^1 f\,dx$$")

    def test_escaped_brackets_stay(self):
        self.assertEqual(self.md(r"\[скобки\]")[0], r"\[скобки\]")

    def test_unknown_tags_escaped_known_kept(self):
        md, _ = self.md("a < b, <div>x</div>, <details><summary>s</summary>t</details>, vector<int>")
        self.assertIn("a &lt; b", md)
        self.assertIn("&lt;div>x&lt;/div>", md)
        self.assertIn("<details><summary>s</summary>t</details>", md)
        self.assertIn("vector&lt;int>", md)

    def test_code_untouched(self):
        src = "Код `a < b && $x` и\n```python\nif a < b: print('$5')\n```\nпосле $y$"
        md, _ = self.md(src)
        self.assertIn("`a < b && $x`", md)
        self.assertIn("if a < b: print('$5')", md)
        self.assertTrue(md.endswith("после $y$"))

    def test_open_fence_closed(self):
        md, _ = self.md("Начало\n```python\nprint(1)")
        self.assertTrue(md.endswith("```"))
        self.assertEqual(md.count("```"), 2)

    def test_image_becomes_media_block(self):
        md, media = self.md("Вот:\n![Синус](out/chart.png)\nконец")
        self.assertEqual([(m, k, p.name) for m, k, p in media], [("m1", "photo", "chart.png")])
        self.assertIn('\n\n![](tg://photo?id=m1 "Синус")\n\n', md)
        self.assertNotIn("\n\n\n", md)

    def test_image_path_with_spaces(self):
        _, media = self.md("![x](my%20file.png)")
        self.assertEqual(media[0][2].name, "my file.png")

    def test_missing_and_outside_files(self):
        md, media = self.md("![a](nope.png) ![b](/etc/hostname)")
        self.assertEqual(media, [])
        self.assertIn("не найден", md)
        self.assertIn("вне рабочей папки", md)

    def test_link_to_local_file_attaches_document(self):
        md, media = self.md("Готово: [отчёт](report.pdf) и [сайт](https://ex.com)")
        self.assertEqual([(k, p.name) for _, k, p in media], [("document", "report.pdf")])
        self.assertIn("**отчёт**", md)
        self.assertIn("[сайт](https://ex.com)", md)
        self.assertTrue(md.endswith('![](tg://document?id=m1 "отчёт")'))

    def test_image_in_table_moves_below(self):
        md, media = self.md("| a | b |\n|---|---|\n| ![c](out/chart.png) | 1 |")
        self.assertIn("| 🖼 c | 1 |", md)
        self.assertTrue(md.endswith('![](tg://photo?id=m1 "c")'))

    def test_draft_mode_does_not_upload(self):
        md, media = self.md("![Синус](out/chart.png)", upload=False)
        self.assertEqual(media, [])
        self.assertEqual(md, "🖼 *Синус*")

    def test_same_file_twice_uploaded_once(self):
        _, media = self.md("![a](out/chart.png)\n\n![b](out/chart.png)")
        self.assertEqual(len(media), 1)


class SplitTest(unittest.TestCase):
    def test_short_text_untouched(self):
        self.assertEqual(split_long("abc", 100), ["abc"])

    def test_fences_reopened_across_chunks(self):
        text = "intro\n```sh\n" + "\n".join(f"echo {i}" for i in range(500)) + "\n```\nend"
        chunks = split_long(text, 1000)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertLessEqual(len(c), 1000)
            self.assertEqual(c.count("```") % 2, 0, c[:40])

    def test_very_long_line_is_cut(self):
        chunks = split_long("x" * 2500, 1000)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual("".join(chunks), "x" * 2500)


class HtmlFallbackTest(unittest.TestCase):
    def test_basic(self):
        out = md_to_html("# Title\n**b** and `x<y`\n```py\na<b\n```")
        self.assertIn("<b>Title</b>", out)
        self.assertIn("<code>x&lt;y</code>", out)
        self.assertIn('<pre><code class="language-py">a&lt;b</code></pre>', out)


if __name__ == "__main__":
    unittest.main()
