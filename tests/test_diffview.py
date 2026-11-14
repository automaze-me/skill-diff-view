"""Tests for scripts/diffview.py. Run: python3 -I -m unittest discover -s tests -v (from the skill dir)."""
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import os
os.environ.pop("DIFFVIEW_LANG", None)  # the tests expect the default: English pages, localhost server
os.environ.pop("DIFFVIEW_LAN", None)

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "diffview.py"
spec = importlib.util.spec_from_file_location("diffview", SCRIPT)
dv = importlib.util.module_from_spec(spec)
sys.modules["diffview"] = dv
spec.loader.exec_module(dv)


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class WordDiff(unittest.TestCase):
    def test_marks_changed_words_only(self):
        out = dv.word_diff("Die Uhr läuft heute", "Die Uhr steht heute")
        self.assertIn("<del>läuft</del>", out)
        self.assertIn("<ins>steht</ins>", out)
        self.assertTrue(out.startswith("Die Uhr "))

    def test_escapes_html(self):
        out = dv.word_diff("a <b> & c", "a <i> & c")
        self.assertNotIn("<b>", out)
        self.assertIn("&lt;b&gt;", out)
        self.assertIn("&amp;", out)

    def test_short_equal_between_changes_joins_the_change(self):
        out = dv.word_diff("eins und zwei", "drei und vier")
        self.assertIn("<del>eins und zwei</del>", out)
        self.assertIn("<ins>drei und vier</ins>", out)


class PairLines(unittest.TestCase):
    def test_pairs_similar_lines_and_keeps_rest_unpaired(self):
        pairs = dv.pair_lines(["alpha beta gamma delta", "x y z"], ["alpha beta gamma epsilon"])
        self.assertEqual(pairs, [("alpha beta gamma delta", "alpha beta gamma epsilon"), ("x y z", None)])

    def test_dissimilar_lines_are_not_paired(self):
        pairs = dv.pair_lines(["ganz anderer Satz hier"], ["völlig fremder Inhalt dort"])
        self.assertEqual(pairs, [("ganz anderer Satz hier", None), (None, "völlig fremder Inhalt dort")])


class CollectGit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r = self.root = Path(self.tmp.name)
        git(r, "init", "-q", "-b", "main")
        git(r, "config", "user.email", "t@t")
        git(r, "config", "user.name", "t")
        (r / "a.md").write_text("eins\nzwei\n")
        (r / "gone.txt").write_text("weg\n")
        (r / "pic.bin").write_bytes(b"\x00\x01")
        git(r, "add", "-A")
        git(r, "commit", "-qm", "base")
        (r / "a.md").write_text("eins\ndrei\n")
        git(r, "rm", "-q", "gone.txt")
        (r / "pic.bin").write_bytes(b"\x00\x02")
        (r / "new.md").write_text("neu\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_lists_modified_deleted_binary_and_untracked(self):
        files = {f.path: f for f in dv.collect_git(self.root)}
        self.assertEqual(sorted(files), ["a.md", "gone.txt", "new.md", "pic.bin"])
        self.assertEqual(files["a.md"].old, "eins\nzwei\n")
        self.assertEqual(files["a.md"].new, "eins\ndrei\n")
        self.assertIsNone(files["gone.txt"].new)
        self.assertIsNone(files["new.md"].old)
        self.assertTrue(files["pic.bin"].binary)

    def test_paths_limit_the_files(self):
        files = dv.collect_git(self.root, paths=["a.md"])
        self.assertEqual([f.path for f in files], ["a.md"])

    def test_separate_git_dir_overlay(self):
        overlay = self.root / ".overlay.git"
        git(self.root, "init", "-q", "--bare", str(overlay))
        base = ["--git-dir", str(overlay), "--work-tree", str(self.root)]
        (self.root / "notes.md").write_text("alt\n")
        git(self.root, *base, "add", "notes.md")
        git(self.root, *base, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "o")
        (self.root / "notes.md").write_text("neu\n")
        files = dv.collect_git(self.root, git_dir=str(overlay), untracked=False)
        self.assertEqual([(f.path, f.old, f.new) for f in files], [("notes.md", "alt\n", "neu\n")])


class Render(unittest.TestCase):
    def test_collapses_long_unchanged_runs(self):
        old = "".join(f"Zeile {i}\n" for i in range(100))
        new = old.replace("Zeile 50\n", "Zeile fünfzig\n")
        page = dv.render([dv.FileDiff("doc.md", old, new)], title="T", subtitle="S", context=3)
        self.assertIn("<details", page)
        self.assertIn("unchanged lines", page)
        self.assertIn("<ins>fünfzig</ins>", page)

    def test_one_section_per_file_with_counts(self):
        files = [dv.FileDiff("a.md", "x\n", "y\n"), dv.FileDiff("b.md", None, "neu\n")]
        page = dv.render(files, title="T", subtitle="S", context=3)
        self.assertEqual(page.count('<section class="file'), 2)
        self.assertIn('href="#f1"', page)
        self.assertIn('<span class="badge add">new</span>', page)


class Sections(unittest.TestCase):
    def test_markdown_change_is_labelled_with_its_heading(self):
        old = "# Titel\n\n## Abschnitt A\n\neins\n\n## Abschnitt B\n\nzwei drei\n"
        new = old.replace("zwei drei", "zwei vier")
        page = dv.render([dv.FileDiff("doc.md", old, new)], title="T", subtitle="S", context=1)
        self.assertIn('<div class="hd">Abschnitt B</div>', page)
        self.assertNotIn('<div class="hd">Abschnitt A</div>', page)

    def test_code_files_get_no_heading_labels(self):
        page = dv.render([dv.FileDiff("x.py", "# Kommentar\na = 1\n", "# Kommentar\na = 2\n")], title="T", subtitle="S")
        self.assertNotIn('class="hd"', page)


class Addresses(unittest.TestCase):
    SAMPLE = (
        "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever\n"
        "2: enp3s0    inet 192.168.0.10/24 brd 192.168.0.255 scope global enp3s0\n"
        "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\n"
        "4: br-1a2b    inet 172.18.0.1/16 scope global br-1a2b\n"
        "5: tailscale0    inet 100.64.0.7/32 scope global tailscale0\n"
    )

    def test_keeps_lan_and_tailscale_only(self):
        self.assertEqual(dv.parse_addresses(self.SAMPLE), [("enp3s0", "192.168.0.10"), ("tailscale0", "100.64.0.7")])


class Cli(unittest.TestCase):
    def test_two_files_mode_writes_page(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a.txt", Path(d) / "b.txt"
            a.write_text("Der Wert ist alt hier\n")
            b.write_text("Der Wert ist neu hier\n")
            out = Path(d) / "out"
            res = subprocess.run([sys.executable, "-I", str(SCRIPT), "--files", str(a), str(b), "--out-dir", str(out), "--name", "t"],
                                 capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            page = (out / "t.html").read_text()
            self.assertIn("<del>alt</del>", page)
            self.assertIn("<ins>neu</ins>", page)


class Server(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "out"
        self.out.mkdir()
        (self.out / "t.html").write_text("<p>page</p>")
        port = dv._free_port(8790)
        self.proc = subprocess.Popen([sys.executable, "-I", str(SCRIPT), "serve", "--port", str(port), "--out-dir", str(self.out)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.port = None
        for _ in range(50):
            self.port = dv.running_server(self.out)
            if self.port:
                break
            subprocess.run(["sleep", "0.1"])
        self.assertIsNotNone(self.port, "server did not start")

    def tearDown(self):
        self.proc.terminate()
        self.proc.communicate(timeout=10)
        self.tmp.cleanup()

    def fetch(self, path):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/{path}", timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_serves_pages_but_not_hidden_files(self):
        self.assertEqual(self.fetch("t.html"), 200)
        self.assertEqual(self.fetch(".server.json"), 404)

    def test_status_prints_pid_and_page_urls(self):
        res = subprocess.run([sys.executable, "-I", str(SCRIPT), "status", "--out-dir", str(self.out)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"pid {self.proc.pid}", res.stdout)
        self.assertIn(f":{self.port}/t.html", res.stdout)

    def test_serve_prints_newest_page_url(self):
        self.proc.terminate()
        out, _ = self.proc.communicate(timeout=10)
        self.assertIn(f":{self.port}/t.html", out)

    def test_default_server_listens_on_localhost_only(self):
        self.assertFalse(json.loads((self.out / ".server.json").read_text())["lan"])
        self.assertFalse(dv.server_lan(self.out))
        res = subprocess.run([sys.executable, "-I", str(SCRIPT), "status", "--out-dir", str(self.out)], capture_output=True, text=True)
        self.assertIn(f"http://localhost:{self.port}/t.html", res.stdout)
        self.assertNotIn("100.", res.stdout)


class LanServer(unittest.TestCase):
    def test_lan_flag_is_recorded_for_status(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            out.mkdir()
            port = dv._free_port(8820, "0.0.0.0")
            proc = subprocess.Popen([sys.executable, "-I", str(SCRIPT), "serve", "--lan", "--port", str(port), "--out-dir", str(out)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                for _ in range(50):
                    if dv.running_server(out):
                        break
                    subprocess.run(["sleep", "0.1"])
                self.assertTrue(dv.server_lan(out))
            finally:
                proc.terminate()
                proc.communicate(timeout=10)


class Language(unittest.TestCase):
    def page(self, **kw):
        return dv.render([dv.FileDiff("a.md", "Der Wert ist alt.\n", "Der Wert ist neu.\n"), dv.FileDiff("b.md", None, "x\n")],
                         "T", "S", **kw)

    def tearDown(self):
        dv.set_lang("en")

    def test_english_is_the_default(self):
        page = self.page()
        self.assertIn('<html lang="en">', page)
        self.assertIn(">Side by side</button>", page)
        self.assertIn(">Reading view</button>", page)
        self.assertIn(">Files</button>", page)
        for german in ("Nebeneinander", "Leseansicht", "Dateien", "aufklappen", "erstellt"):
            self.assertNotIn(german, page)

    def test_german_on_request(self):
        page = self.page(lang="de")
        self.assertIn('<html lang="de">', page)
        self.assertIn(">Nebeneinander</button>", page)
        self.assertIn(">Leseansicht</button>", page)
        self.assertIn(">Alles aufklappen</button>", page)

    def test_page_script_gets_the_strings_of_its_language(self):
        m = re.search(r"<script>window\.DVT=(\{.*?\});</script>", self.page(lang="de"))
        self.assertIsNotNone(m)
        self.assertEqual(json.loads(m.group(1))["sections"], "Geänderte Abschnitte")

    def test_environment_variable_picks_the_language_and_unknown_values_fall_back(self):
        os.environ["DIFFVIEW_LANG"] = "de"
        try:
            self.assertEqual(dv.lang_name(), "de")
            self.assertEqual(dv.lang_name("fr"), "en")
        finally:
            os.environ.pop("DIFFVIEW_LANG")
        self.assertEqual(dv.lang_name(), "en")

    def test_cli_lang_option(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a.txt", Path(d) / "b.txt"
            a.write_text("alt\n")
            b.write_text("neu\n")
            res = subprocess.run([sys.executable, "-I", str(SCRIPT), "--files", str(a), str(b), "--out-dir", str(Path(d) / "out"),
                                  "--name", "t", "--lang", "de"], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("Nebeneinander", (Path(d) / "out" / "t.html").read_text())


class Urls(unittest.TestCase):
    def test_without_lan_only_localhost(self):
        self.assertEqual(dv.urls(8770, "x.html"), ["http://localhost:8770/x.html  (this machine only)"])

    def test_lan_from_environment(self):
        for value, expected in (("1", True), ("yes", True), ("0", False), ("", False)):
            os.environ["DIFFVIEW_LAN"] = value
            self.assertEqual(dv.env_lan(), expected, value)
        os.environ.pop("DIFFVIEW_LAN")


class BuildHint(unittest.TestCase):
    def test_hint_names_serve_without_out_dir_for_default_folder(self):
        with tempfile.TemporaryDirectory() as d:
            git(d, "init", "-q", "-b", "main")
            (Path(d) / "x.md").write_text("neu\n")
            res = subprocess.run([sys.executable, "-I", str(SCRIPT)], cwd=d, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("diffview.py serve", res.stdout)
            self.assertNotIn("--out-dir", res.stdout)


if __name__ == "__main__":
    unittest.main()
