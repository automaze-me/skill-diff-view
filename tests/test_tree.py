"""Tests for the file tree beside the diff: folders, order, change kinds, filter.

Run: python3 -I -m unittest discover -s tests -v (from the skill dir). The browser tests need google-chrome
and are skipped when it is missing.
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.pop("DIFFVIEW_LANG", None)  # the tests expect English pages

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "diffview.py"
spec = importlib.util.spec_from_file_location("diffview", SCRIPT)
dv = importlib.util.module_from_spec(spec)
sys.modules["diffview"] = dv
spec.loader.exec_module(dv)
CHROME = shutil.which("google-chrome") or shutil.which("google-chrome-stable")


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def three_kinds():
    return [
        dv.FileDiff("src/app.py", "a = 1\n", "a = 2\n"),
        dv.FileDiff("src/new.py", None, "b = 1\n"),
        dv.FileDiff("old/gone.py", "c = 1\n", None),
    ]


def tag(page, pattern):
    m = re.search(pattern, page)
    if not m:
        raise AssertionError(f"{pattern} not found")
    return m.group(0)


class Order(unittest.TestCase):
    def test_folders_first_then_files_case_insensitive(self):
        paths = ["b.txt", "src/z.py", "src/A.py", "Makefile", "docs/x.md"]
        order = [paths[i] for i in dv.tree_order(dv.file_tree(paths))]
        self.assertEqual(order, ["docs/x.md", "src/A.py", "src/z.py", "b.txt", "Makefile"])

    def test_same_name_twice_keeps_both(self):
        self.assertEqual(sorted(dv.tree_order(dv.file_tree(["x.md", "x.md"]))), [0, 1])

    def test_sections_follow_the_tree(self):
        files = [dv.FileDiff("Makefile", "a\n", "b\n"), dv.FileDiff("src/a.py", "a\n", "b\n")]
        page = dv.render(files, "T", "S")
        self.assertIn('id="f1" data-kind="mod"><h2><span class="path">src/a.py', page)
        self.assertIn('id="f2" data-kind="mod"><h2><span class="path">Makefile', page)


class Tree(unittest.TestCase):
    def test_single_child_folders_merge_into_one_row(self):
        files = [dv.FileDiff("system/lib/Acme/A.php", "a\n", "b\n"), dv.FileDiff("system/lib/Acme/B/C.php", "a\n", "b\n")]
        page = dv.render(files, "T", "S")
        self.assertIn('<span class="dn">system/lib/Acme</span>', page)
        self.assertIn('<span class="dn">B</span>', page)
        self.assertNotIn('<span class="dn">system</span>', page)

    def test_folders_are_open_details(self):
        page = dv.render(three_kinds(), "T", "S")
        self.assertEqual(page.count('<li class="dir"><details open>'), 2)

    def test_leaf_names_the_file_its_kind_and_links_to_its_section(self):
        page = dv.render(three_kinds(), "T", "S")
        leaf = tag(page, r'<li class="leaf" data-kind="del">.*?</li>')
        self.assertIn('href="#f1"', leaf)
        self.assertIn('<span class="fn">gone.py</span>', leaf)
        self.assertIn('title="old/gone.py · deleted"', leaf)
        self.assertIn('<span class="minus">−1</span>', leaf)
        self.assertNotIn('class="plus"', leaf)

    def test_sections_carry_their_kind(self):
        page = dv.render(three_kinds(), "T", "S")
        self.assertIn('data-kind="add"><h2><span class="path">src/new.py', page)
        self.assertIn('data-kind="del"><h2><span class="path">old/gone.py', page)

    def test_single_file_page_has_no_tree(self):
        page = dv.render([dv.FileDiff("a.py", "a\n", "b\n")], "T", "S")
        self.assertNotIn('<nav class="tree"', page)
        self.assertNotIn('id="tree-btn"', page)
        self.assertIn("<body>", page)

    def test_page_with_tree_gets_the_toggle(self):
        page = dv.render(three_kinds(), "T", "S")
        self.assertIn('<body class="has-tree">', page)
        self.assertIn('<button id="tree-btn" aria-pressed="true">Files</button>', page)


class Filter(unittest.TestCase):
    def test_one_button_per_present_kind_with_count(self):
        files = three_kinds() + [dv.FileDiff("src/more.py", None, "x\n")]
        page = dv.render(files, "T", "S")
        self.assertIn('<button class="flt add" data-kind="add" aria-pressed="true">+ new <b>2</b></button>', page)
        self.assertIn('<button class="flt mod" data-kind="mod" aria-pressed="true">~ changed <b>1</b></button>', page)
        self.assertIn('<button class="flt del" data-kind="del" aria-pressed="true">− deleted <b>1</b></button>', page)

    def test_no_filter_when_all_files_are_of_one_kind(self):
        files = [dv.FileDiff("a.py", "a\n", "b\n"), dv.FileDiff("b.py", "a\n", "b\n")]
        page = dv.render(files, "T", "S")
        self.assertIn('<nav class="tree"', page)
        self.assertNotIn('class="flt', page)


class Kinds(unittest.TestCase):
    def test_deleted_and_added_binaries_keep_their_kind(self):
        with tempfile.TemporaryDirectory() as d:
            r = Path(d)
            git(r, "init", "-q", "-b", "main")
            (r / "old.jpg").write_bytes(b"\x00\x01")
            git(r, "add", "-A")
            git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
            git(r, "rm", "-q", "old.jpg")
            (r / "new.jpg").write_bytes(b"\x00\x02")
            files = {f.path: f for f in dv.collect_git(r)}
        self.assertEqual(files["old.jpg"].kind, "del")
        self.assertEqual(files["new.jpg"].kind, "add")
        page = dv.render(list(files.values()), "T", "S")
        self.assertIn('<li class="leaf" data-kind="del">', page)
        self.assertIn('<span class="badge del">deleted</span> <span class="badge">binary</span></h2>'
                      '<p class="note">Binary file deleted.</p>', page)
        self.assertIn('<span class="badge add">new</span> <span class="badge">binary</span></h2>'
                      '<p class="note">New binary file, no text comparison.</p>', page)

    def test_kind_without_git_follows_the_sides(self):
        self.assertEqual(dv.FileDiff("a", None, "x").kind, "add")
        self.assertEqual(dv.FileDiff("a", "x", None).kind, "del")
        self.assertEqual(dv.FileDiff("a", "x", "y").kind, "mod")
        self.assertEqual(dv.FileDiff("a", None, None, binary=True).kind, "mod")


@unittest.skipUnless(CHROME, "google-chrome not installed")
class Browser(unittest.TestCase):
    def dom(self, query="", width=1400, then=""):
        with tempfile.TemporaryDirectory() as d:
            page = Path(d) / "p.html"
            html = dv.render(three_kinds(), "t", "s")
            page.write_text(html.replace("</body>", f"<script>{then}</script></body>"), encoding="utf-8")
            res = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                                  f"--user-data-dir={d}/profile", f"--window-size={width},900", "--virtual-time-budget=3000",
                                  "--dump-dom", page.as_uri() + query], capture_output=True, text=True, timeout=60)
        self.assertIn("</body>", res.stdout, res.stderr[-2000:])
        return res.stdout

    def test_hide_parameter_hides_deleted_files_and_their_empty_folder(self):
        dom = self.dom("?hide=del")
        self.assertIn("hidden", tag(dom, r'<section class="file[^>]*data-kind="del"[^>]*>'))
        self.assertNotIn("hidden", tag(dom, r'<section class="file[^>]*data-kind="mod"[^>]*>'))
        self.assertIn("hidden", tag(dom, r'<li class="leaf" data-kind="del"[^>]*>'))
        self.assertIn("hidden", tag(dom, r'<li class="dir"[^>]*><details open=""><summary[^>]*><span class="dn">old<'))
        self.assertNotIn("hidden", tag(dom, r'<li class="dir"[^>]*><details open=""><summary[^>]*><span class="dn">src<'))
        self.assertIn('aria-pressed="false">− deleted', dom)
        self.assertIn("2 of 3 files", dom)

    def test_without_filter_everything_shows(self):
        dom = self.dom()
        self.assertNotIn('hidden=""', tag(dom, r'<section class="file[^>]*data-kind="del"[^>]*>'))
        self.assertNotIn("of 3 files", dom)

    def test_tree_starts_closed_on_a_phone_and_open_on_a_wide_screen(self):
        self.assertIn("tree-off", tag(self.dom(width=400), r"<body[^>]*>"))
        self.assertNotIn("tree-off", tag(self.dom(width=1400), r"<body[^>]*>"))

    def test_opened_tree_on_a_phone_starts_below_the_bar(self):
        probe = ("document.getElementById('tree-btn').click();"
                 "document.body.setAttribute('data-bar-bottom',Math.round(document.querySelector('.bar').getBoundingClientRect().bottom));"
                 "document.body.setAttribute('data-nav-top',Math.round(document.querySelector('nav.tree').getBoundingClientRect().top))")
        body = tag(self.dom(width=520, then=probe), r"<body[^>]*>")
        self.assertNotIn("tree-off", body)
        bar_bottom = int(re.search(r'data-bar-bottom="(\d+)"', body).group(1))
        nav_top = int(re.search(r'data-nav-top="(\d+)"', body).group(1))
        self.assertGreater(bar_bottom, 60)
        self.assertEqual(nav_top, bar_bottom)


if __name__ == "__main__":
    unittest.main()
