"""Tests for the rendered Markdown view ("Reading view"): scripts/mdredline.py, scripts/mdview.js and the page.

Run: python3 -I -m unittest discover -s tests -v (from the skill dir). The mdview tests need node, the browser
test needs google-chrome; both are skipped when missing.
"""
import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.pop("DIFFVIEW_LANG", None)  # the tests expect English pages

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
SCRIPT = SCRIPTS / "diffview.py"


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dv = load("diffview", "diffview.py")
md = load("diffview_mdredline", "mdredline.py")
NODE = shutil.which("node")
CHROME = shutil.which("google-chrome") or shutil.which("google-chrome-stable")


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Redline(unittest.TestCase):
    def test_unchanged_text_has_no_marks(self):
        src = "# Titel\n\nEin Absatz mit **Fett**.\n\n- eins\n- zwei\n"
        self.assertEqual(md.redline(src, src), src)

    def test_word_change_in_paragraph(self):
        out = md.redline("Die Uhr läuft heute.\n", "Die Uhr steht heute.\n")
        self.assertIn("Die Uhr <del>läuft</del><ins>steht</ins> heute.", out)

    def test_bold_markers_stay_outside_the_marks(self):
        out = md.redline("**Alt** bleibt\n", "**Neu** bleibt\n")
        self.assertIn("**<del>Alt</del><ins>Neu</ins>** bleibt", out)

    def test_new_heading_is_marked_whole(self):
        out = md.redline("# A\n\nText.\n", "# A\n\n## Neu dazu\n\nText.\n")
        self.assertIn("## <ins>Neu dazu</ins>", out)

    def test_removed_list_item_keeps_its_bullet(self):
        out = md.redline("- eins\n- zwei\n- drei\n", "- eins\n- drei\n")
        self.assertIn("- <del>zwei</del>", out)

    def test_table_cell_change_marks_only_that_cell(self):
        old = "| a | b |\n|---|---|\n| x | alt |\n"
        new = "| a | b |\n|---|---|\n| x | neu |\n"
        self.assertIn("| x | <del>alt</del><ins>neu</ins> |", md.redline(old, new))

    def test_realigned_table_counts_as_unchanged(self):
        old = "| a | b |\n|---|---|\n| x | y |\n"
        new = "| a   | b   |\n| --- | --- |\n| x   | y   |\n"
        out = md.redline(old, new)
        self.assertNotIn("<ins>", out)
        self.assertNotIn("<del>", out)

    def test_changed_code_block_marks_lines_and_escapes_html(self):
        out = md.redline("```\neins <b>\nzwei\n```\n", "```\neins <b>\ndrei\n```\n")
        self.assertIn("<pre><code>", out)
        self.assertIn("eins &lt;b&gt;", out)
        self.assertIn("<del>zwei</del>", out)
        self.assertIn("<ins>drei</ins>", out)

    def test_new_and_deleted_documents_are_unmarked(self):
        self.assertEqual(md.redline(None, "# T\n\nText\n"), "# T\n\nText\n")
        self.assertEqual(md.redline("# T\n", None), "# T\n")

    def test_front_matter_change_is_shown_as_code(self):
        out = md.redline("---\nversion: 1\n---\n# T\n", "---\nversion: 2\n---\n# T\n")
        self.assertIn("<del>version: 1</del>", out)
        self.assertIn("<ins>version: 2</ins>", out)


@unittest.skipUnless(NODE, "node not installed")
class MdView(unittest.TestCase):
    def render(self, src):
        js = ("globalThis.marked=require(process.argv[1]);require(process.argv[2]);"
              "process.stdout.write(globalThis.DVMarkdown.render(require('fs').readFileSync(0,'utf8')));")
        res = subprocess.run([NODE, "-e", js, str(SCRIPTS / "vendor" / "marked.min.js"), str(SCRIPTS / "mdview.js")],
                             input=src, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res.stdout

    def test_keeps_ins_and_del(self):
        out = self.render("Die Uhr <del>läuft</del><ins>steht</ins> heute.\n")
        self.assertIn("<del>läuft</del><ins>steht</ins>", out)

    def test_escapes_script_tags(self):
        out = self.render("Text <script>alert(1)</script>\n\n<script>alert(2)</script>\n")
        self.assertNotRegex(out.lower(), r"<script")
        self.assertIn("&lt;script&gt;", out)

    def test_drops_attributes_except_class(self):
        out = self.render('<div class="rl-block rl-neu" onclick="x()">\n\nA\n\n</div>\n')
        self.assertIn('<div class="rl-block rl-neu">', out)
        self.assertNotIn("onclick", out)

    def test_escapes_image_tags_with_handlers(self):
        out = self.render('<img src=x onerror="alert(1)">\n')
        self.assertNotIn("<img", out)

    def test_javascript_links_are_no_links(self):
        out = self.render("[klick](javascript:alert(1))\n")
        self.assertNotIn("href", out)
        self.assertIn("klick", out)

    def test_web_links_open_in_a_new_tab(self):
        out = self.render("[Seite](https://example.org/a)\n")
        self.assertIn('href="https://example.org/a"', out)
        self.assertIn('rel="noopener noreferrer"', out)

    def test_gfm_table(self):
        out = self.render("| a | b |\n|---|---|\n| 1 | 2 |\n")
        self.assertIn("<table>", out)
        self.assertIn("<td>1</td>", out)

    def test_code_block_inside_list_item(self):
        out = self.render("- Schritt\n\n  ```bash\n  make test\n  ```\n- weiter\n")
        li, pre, end = out.find("<li>"), out.find("<pre>"), out.find("</li>")
        self.assertTrue(0 <= li < pre < end, out)

    def test_images_become_placeholders(self):
        out = self.render("![Logo](a.png)\n")
        self.assertIn("md-img", out)
        self.assertNotIn("<img", out)


def scripts(page):
    """Decoded sources of the data: URL scripts of a page."""
    return [base64.b64decode(m).decode("utf-8") for m in re.findall(r'src="data:text/javascript;base64,([A-Za-z0-9+/=]+)"', page)]


class Page(unittest.TestCase):
    def payload(self, page, fid="f1"):
        m = re.search(rf'<script type="application/json" id="{fid}-md">(.*?)</script>', page, re.S)
        self.assertIsNotNone(m, "markdown payload missing")
        return json.loads(m.group(1))

    def test_markdown_file_gets_the_rendered_view(self):
        page = dv.render([dv.FileDiff("a.md", "Der Wert ist alt.\n", "Der Wert ist neu.\n")], "t", "s")
        self.assertIn('class="view md"', page)
        self.assertIn('<button id="m-md" aria-pressed="false">Reading view</button>', page)
        sources = scripts(page)
        self.assertTrue(any("marked v15" in src for src in sources))
        self.assertTrue(any("DVMarkdown" in src for src in sources))
        self.assertIn("Der Wert ist <del>alt</del><ins>neu</ins>.", self.payload(page))

    def test_payload_cannot_close_its_script_element(self):
        src = "x </script><script>alert(1)</script>\n"
        page = dv.render([dv.FileDiff("a.md", None, src)], "t", "s")
        self.assertEqual(self.payload(page), src)

    def test_code_only_page_has_no_markdown_assets(self):
        page = dv.render([dv.FileDiff("a.py", "a = 1\n", "a = 2\n")], "t", "s")
        self.assertEqual(scripts(page), [])
        self.assertNotIn('id="m-md"', page)

    def test_new_documents_open_rendered(self):
        page = dv.render([dv.FileDiff("plan.md", None, "# Plan\n\nText\n")], "t", "s")
        self.assertIn('<body data-default-mode="md">', page)

    def test_changed_documents_keep_the_chosen_mode(self):
        page = dv.render([dv.FileDiff("a.md", "a\n", "b\n")], "t", "s")
        self.assertIn("<body>", page)

    def test_deleted_document_says_so(self):
        page = dv.render([dv.FileDiff("a.md", "# Weg\n", None)], "t", "s")
        self.assertIn("File deleted", page)
        self.assertEqual(self.payload(page), "# Weg\n")


class Cli(unittest.TestCase):
    def run_dv(self, cwd, *args):
        res = subprocess.run([sys.executable, "-I", str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def test_new_option_shows_a_new_document(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "plan.md").write_text("# Plan\n\nText\n")
            self.run_dv(d, "--new", "plan.md", "--out-dir", "out", "--name", "t")
            page = (Path(d) / "out" / "t.html").read_text()
            self.assertIn('class="badge add"', page)
            self.assertIn('data-default-mode="md"', page)

    def test_overlay_file_named_explicitly_shows_as_new_when_untracked(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            git(root, "init", "-q", "-b", "main")
            overlay = root / ".overlay.git"
            git(root, "init", "-q", "--bare", str(overlay))
            base = ["--git-dir", str(overlay), "--work-tree", str(root)]
            (root / "notes.md").write_text("alt\n")
            git(root, *base, "add", "notes.md")
            git(root, *base, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "o")
            (root / "notes.md").write_text("neu\n")
            (root / "plan.md").write_text("# Plan\n")
            (root / "other.md").write_text("# Nicht genannt\n")
            self.run_dv(root, "--git-dir", str(overlay), "notes.md", "plan.md", "--out-dir", "out", "--name", "t")
            page = (root / "out" / "t.html").read_text()
            self.assertIn(">plan.md<", page)
            self.assertIn(">notes.md<", page)
            self.assertNotIn("other.md", page)


@unittest.skipUnless(CHROME, "google-chrome not installed")
class Browser(unittest.TestCase):
    def test_rendered_view_marks_changed_blocks(self):
        old = "# Plan\n\n## Teil A\n\nDie Uhr läuft.\n\n## Teil B\n\n| a | b |\n|---|---|\n| x | alt |\n"
        new = "# Plan\n\n## Teil A\n\nDie Uhr steht.\n\n## Teil B\n\n| a | b |\n|---|---|\n| x | neu |\n"
        with tempfile.TemporaryDirectory() as d:
            page = Path(d) / "p.html"
            page.write_text(dv.render([dv.FileDiff("plan.md", old, new)], "t", "s"), encoding="utf-8")
            res = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                                  f"--user-data-dir={d}/profile", "--virtual-time-budget=3000", "--dump-dom",
                                  page.as_uri() + "?md"], capture_output=True, text=True, timeout=60)
            dom = res.stdout
        self.assertIn("<table>", dom)
        self.assertIn("md-mode", dom)
        self.assertGreaterEqual(dom.count("md-chg"), 2, dom[-2000:])
        self.assertIn("Changed sections", dom)
        self.assertIn("Teil A", dom[dom.find("Changed sections"):])

    def test_changed_sections_list_the_new_heading_only(self):
        old = "# Plan\n\n## 5. Risks\n\nalt\n"
        new = "# Plan\n\n## 6. Risks\n\nneu\n"
        with tempfile.TemporaryDirectory() as d:
            page = Path(d) / "p.html"
            page.write_text(dv.render([dv.FileDiff("plan.md", old, new)], "t", "s"), encoding="utf-8")
            res = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                                  f"--user-data-dir={d}/profile", "--virtual-time-budget=3000", "--dump-dom",
                                  page.as_uri() + "?md"], capture_output=True, text=True, timeout=60)
            toc = res.stdout[res.stdout.find("Changed sections"):]
        self.assertIn(">6. Risks</button>", toc)
        self.assertNotIn("56. Risks", toc)


if __name__ == "__main__":
    unittest.main()
