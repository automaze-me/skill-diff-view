#!/usr/bin/env python3
"""diffview: build a readable HTML diff page and serve it to a browser.

The page is self-contained (no external assets): word-level highlights, an
inline and a side-by-side view, unchanged runs folded, one section per file,
next/previous-change navigation (keys n / p), dark mode, phone-friendly.
With more than one file, a folder tree beside the diff (button "Files")
links to every file and filters by kind of change (new / changed / deleted);
?hide=del (comma list of add, mod, del) opens the page with those kinds hidden.

Build (writes <root>/.diffview/<name>.html; <root> = git top level or cwd):
  diffview.py [PATH ...]                 working tree (+ untracked) vs HEAD
  diffview.py [PATH ...] --from REV      working tree vs REV
  diffview.py [PATH ...] --from A --to B two revisions
  diffview.py [PATH ...] --git-dir DIR   separate git dir (overlay repo) over the root
  diffview.py --files OLD NEW            two arbitrary files
  diffview.py --new FILE [FILE ...]      files as new documents (no old version)
  With --git-dir, a file named explicitly that the overlay does not track shows as new.
  Markdown files also get the "Reading view": the document typeset, changes marked
  (URL parameter ?md); a page of new Markdown documents opens in that view.
  options: --name NAME  --title TEXT  --context N (3)  --out-dir DIR  --no-untracked  --lang en|de

Serve (blocking; start it in the background, stop it by PID):
  diffview.py serve [--port 8770] [--out-dir DIR] [--lan]
  Without --lan the server listens on localhost only. --lan (or DIFFVIEW_LAN=1)
  listens on every interface and prints LAN and Tailscale URLs.
Status (PID, port and page URLs of the running server; waits up to 5 s for it):
  diffview.py status [--out-dir DIR]

If a server for the same directory is already running, `build` prints the URLs
of the new page directly; otherwise it prints the serve command.

Page language: English, or German with --lang de or DIFFVIEW_LANG=de.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import base64
import functools
import html
import importlib.util
import http.server
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8770
OUT_DIRNAME = ".diffview"
PROSE_SUFFIXES = {".md", ".markdown", ".txt", ".rst", ".adoc", ".tex", ".org"}
MD_SUFFIXES = {".md", ".markdown"}
HERE = Path(__file__).resolve().parent
TOKEN = re.compile(r"</?[A-Za-z][\w:-]*(?:\s[^<>]*)?/?>|\s+|\w+|[^\w\s]")
SKIP_IFACES = re.compile(r"^(lo|docker|br-|veth|virbr|cni|flannel|podman|vmnet|lxc)")
MAX_PAIR_CELLS = 40000
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's empty tree: the old side before the first commit
LANG_ENV = "DIFFVIEW_LANG"
LAN_ENV = "DIFFVIEW_LAN"

STRINGS = {
    "en": {
        "html_lang": "en", "stamp": "%Y-%m-%d %H:%M",
        "add": "new", "mod": "changed", "del": "deleted", "binary": "binary",
        "bin_add": "New binary file, no text comparison.", "bin_mod": "Binary file changed, no text comparison.",
        "bin_del": "Binary file deleted.",
        "folded": "{n} unchanged lines", "files": "Files", "fold_dirs": "Collapse folders", "open_dirs": "Expand folders",
        "md_deleted": "File deleted; this is how it last looked.", "no_text_change": "No difference in the text.",
        "no_changes": "No changes found.", "no_match": "No file matches the filter.",
        "inline": "Inline", "side": "Side by side", "reading": "Reading view",
        "open_all": "Expand all", "close_all": "Collapse unchanged",
        "prev": "◀ Previous", "prev_title": "Previous change (p)", "next": "Next ▶", "next_title": "Next change (n)",
        "created": "created", "nfiles": "{n} file(s)",
        "sections": "Changed sections", "top": "Top", "shown": "{shown} of {total} files", "image": "Image",
        "no_pages": "No diffs yet.", "new_docs": "new documents", "worktree": "working tree",
        "title_new_one": "New: {path}", "title_new_many": "New documents in {root}",
        "title_one": "Changes: {path}", "title_many": "Changes in {root}", "default_name": "changes",
    },
    "de": {
        "html_lang": "de", "stamp": "%d.%m.%Y %H:%M",
        "add": "neu", "mod": "geändert", "del": "gelöscht", "binary": "binär",
        "bin_add": "Neue Binärdatei, kein Textvergleich.", "bin_mod": "Binärdatei geändert, kein Textvergleich.",
        "bin_del": "Binärdatei gelöscht.",
        "folded": "{n} unveränderte Zeilen", "files": "Dateien", "fold_dirs": "Ordner zuklappen", "open_dirs": "Ordner aufklappen",
        "md_deleted": "Datei gelöscht; so sah sie zuletzt aus.", "no_text_change": "Kein Unterschied im Text.",
        "no_changes": "Keine Änderungen gefunden.", "no_match": "Keine Datei passt zum Filter.",
        "inline": "Inline", "side": "Nebeneinander", "reading": "Leseansicht",
        "open_all": "Alles aufklappen", "close_all": "Unveränderte zuklappen",
        "prev": "◀ Vorige", "prev_title": "Vorige Änderung (p)", "next": "Nächste ▶", "next_title": "Nächste Änderung (n)",
        "created": "erstellt", "nfiles": "{n} Datei(en)",
        "sections": "Geänderte Abschnitte", "top": "Anfang", "shown": "{shown} von {total} Dateien", "image": "Bild",
        "no_pages": "Noch keine Diffs.", "new_docs": "neue Dokumente", "worktree": "Arbeitsstand",
        "title_new_one": "Neu: {path}", "title_new_many": "Neue Dokumente in {root}",
        "title_one": "Änderungen: {path}", "title_many": "Änderungen in {root}", "default_name": "aenderungen",
    },
}
JS_STRINGS = ("fold_dirs", "open_dirs", "open_all", "close_all", "sections", "top", "shown", "image")


def lang_name(value: str | None = None) -> str:
    """The page language: the given value, else DIFFVIEW_LANG, else English."""
    v = (value or os.environ.get(LANG_ENV) or "en").strip().lower()[:2]
    return v if v in STRINGS else "en"


def set_lang(value: str | None = None) -> dict:
    global T
    T = STRINGS[lang_name(value)]
    return T


T = STRINGS[lang_name()]


# ---------------------------------------------------------------- word diff

def _ops(old: str, new: str):
    a, b = TOKEN.findall(old), TOKEN.findall(new)
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    # A short equal piece between two changes (a space, "und", a comma) joins
    # the change, so the change does not fall apart into snippets.
    for k in range(1, len(ops) - 1):
        tag, i1, i2, j1, j2 = ops[k]
        if tag == "equal" and ops[k - 1][0] != "equal" and ops[k + 1][0] != "equal":
            if len("".join(a[i1:i2]).strip()) <= 3:
                ops[k] = ("replace", i1, i2, j1, j2)
    merged: list[list] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag != "equal" and merged and merged[-1][0] != "equal":
            merged[-1][2], merged[-1][4] = i2, j2
        else:
            merged.append([tag, i1, i2, j1, j2])
    return a, b, merged


def _mark(tokens: list[str], tag: str) -> str:
    text = "".join(tokens)
    if not text.strip():
        return html.escape(text) if tag == "ins" else ""
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return f"{html.escape(lead)}<{tag}>{html.escape(text.strip())}</{tag}>{html.escape(trail)}"


def word_diff(old: str, new: str) -> str:
    """One line of HTML: unchanged text plain, removed words in <del>, added in <ins>."""
    a, b, ops = _ops(old, new)
    out = []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            out.append(html.escape("".join(b[j1:j2])))
        else:
            out.append(_mark(a[i1:i2], "del") + _mark(b[j1:j2], "ins"))
    return "".join(out)


def word_sides(old: str, new: str) -> tuple[str, str]:
    """The old line with <del> marks and the new line with <ins> marks."""
    a, b, ops = _ops(old, new)
    left, right = [], []
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            left.append(html.escape("".join(a[i1:i2])))
            right.append(html.escape("".join(b[j1:j2])))
        else:
            left.append(_mark(a[i1:i2], "del"))
            right.append(_mark(b[j1:j2], "ins"))
    return "".join(left), "".join(right)


def _ratio(old: str, new: str) -> float:
    a = [x for x in TOKEN.findall(old) if not x.isspace()]
    b = [x for x in TOKEN.findall(new) if not x.isspace()]
    if not a or not b:
        return 1.0 if a == b else 0.0
    m = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return m.ratio() if m.quick_ratio() >= 0.4 else 0.0


def pair_lines(olds: list[str], news: list[str]) -> list[tuple[str | None, str | None]]:
    """Pair the most similar old and new lines of a changed block, keeping order."""
    if not olds or not news or len(olds) * len(news) > MAX_PAIR_CELLS:
        return [(o, None) for o in olds] + [(None, n) for n in news]
    best, bi, bj = 0.0, -1, -1
    for i, o in enumerate(olds):
        for j, n in enumerate(news):
            r = _ratio(o, n)
            if r > best:
                best, bi, bj = r, i, j
    if best < 0.45:
        return [(o, None) for o in olds] + [(None, n) for n in news]
    return pair_lines(olds[:bi], news[:bj]) + [(olds[bi], news[bj])] + pair_lines(olds[bi + 1:], news[bj + 1:])


# ---------------------------------------------------------------- collecting

KINDS = {"add": "+", "mod": "~", "del": "−"}  # mark per kind of change; the label is T[kind]


@dataclass
class FileDiff:
    path: str
    old: str | None
    new: str | None
    binary: bool = False
    kind: str = ""  # add, mod, del; from git when known (a binary has no text sides to tell)

    def __post_init__(self):
        if not self.kind:
            gone = (self.old is None, self.new is None)
            self.kind = "mod" if self.binary else {(True, False): "add", (False, True): "del"}.get(gone, "mod")


def _git(root: Path, git_dir: str | None, *args: str, check: bool = True) -> bytes:
    cmd = ["git", "-C", str(root)]
    if git_dir:
        cmd += ["--git-dir", str(Path(git_dir).expanduser().resolve()), "--work-tree", str(root)]
    res = subprocess.run(cmd + list(args), capture_output=True)
    if check and res.returncode != 0:
        sys.exit(f"git {' '.join(args)} failed: {res.stderr.decode(errors='replace').strip()}")
    return res.stdout


def _decode(data: bytes | None) -> tuple[str | None, bool]:
    if data is None:
        return None, False
    if b"\x00" in data[:8000]:
        return None, True
    return data.decode("utf-8", errors="replace"), False


def collect_git(root: Path, rev_from: str = "HEAD", rev_to: str | None = None, paths=(),
                untracked: bool = True, git_dir: str | None = None) -> list[FileDiff]:
    root = Path(root)
    if rev_from == "HEAD" and not _git(root, git_dir, "rev-parse", "--verify", "-q", "HEAD", check=False).strip():
        rev_from = EMPTY_TREE
    spec = ["--", *paths] if paths else []
    revs = [rev_from] + ([rev_to] if rev_to else [])
    raw = _git(root, git_dir, "diff", "--name-status", "--no-renames", "-z", *revs, *spec).split(b"\x00")
    entries = [(raw[k].decode(), raw[k + 1].decode()) for k in range(0, len(raw) - 1, 2)]
    if untracked and rev_to is None:
        for p in _git(root, git_dir, "ls-files", "--others", "--exclude-standard", "-z", *spec).split(b"\x00"):
            if p:
                entries.append(("A", p.decode()))
    files = []
    for status, path in sorted(set(entries), key=lambda e: e[1]):
        if path.startswith(OUT_DIRNAME + "/"):
            continue
        old_b = None if status == "A" else _git(root, git_dir, "show", f"{rev_from}:{path}", check=False)
        if status == "D":
            new_b = None
        elif rev_to:
            new_b = _git(root, git_dir, "show", f"{rev_to}:{path}", check=False)
        else:
            f = root / path
            new_b = f.read_bytes() if f.is_file() else None
        old, ob = _decode(old_b)
        new, nb = _decode(new_b)
        files.append(FileDiff(path, old, new, ob or nb, {"A": "add", "D": "del"}.get(status[:1], "mod")))
    return files


# ---------------------------------------------------------------- rendering

@dataclass
class Row:
    kind: str  # eq, del, ins, chg
    old_no: int | None
    new_no: int | None
    old: str | None
    new: str | None
    section: str | None = None


HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")


def _label_sections(rows: list[Row]) -> None:
    """Give every row the Markdown heading it stands under (new text, or old for removed lines)."""
    current = None
    for r in rows:
        text = r.old if r.kind == "del" else r.new
        m = HEADING.match(text or "")
        if m:
            current = m.group(1)
        r.section = current


def _rows(old: str | None, new: str | None) -> list[Row]:
    a = old.splitlines() if old is not None else []
    b = new.splitlines() if new is not None else []
    rows: list[Row] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            rows += [Row("eq", i1 + k + 1, j1 + k + 1, a[i1 + k], b[j1 + k]) for k in range(i2 - i1)]
            continue
        oi, nj = i1, j1
        for o, n in pair_lines(a[i1:i2], b[j1:j2]):
            if o is not None and n is not None:
                rows.append(Row("chg", oi + 1, nj + 1, o, n))
                oi, nj = oi + 1, nj + 1
            elif o is not None:
                rows.append(Row("del", oi + 1, None, o, None))
                oi += 1
            else:
                rows.append(Row("ins", None, nj + 1, None, n))
                nj += 1
    return rows


def _items(rows: list[Row], context: int):
    """Rows near a change stay visible; longer unchanged runs fold."""
    visible = [False] * len(rows)
    for i, r in enumerate(rows):
        if r.kind != "eq":
            for k in range(max(0, i - context), min(len(rows), i + context + 1)):
                visible[k] = True
    items, run = [], []
    for i, r in enumerate(rows):
        if visible[i]:
            if run:
                items += [("row", x) for x in run] if len(run) < 3 else [("fold", run)]
                run = []
            items.append(("row", r))
        else:
            run.append(r)
    if run:
        items += [("row", x) for x in run] if len(run) < 3 else [("fold", run)]
    return items


def _ln(n: int | None) -> str:
    return f'<span class="ln">{n}</span>' if n else '<span class="ln"></span>'


def _inline_row(r: Row, hunk: str) -> str:
    if r.kind == "eq":
        return f'<div class="r eq">{_ln(r.new_no)}<span class="mk"></span><span class="tx">{html.escape(r.new)}</span></div>'
    if r.kind == "chg":
        body, mk, no = word_diff(r.old, r.new), "~", r.new_no
    elif r.kind == "del":
        body, mk, no = html.escape(r.old), "−", r.old_no
    else:
        body, mk, no = html.escape(r.new), "+", r.new_no
    return f'<div class="r {r.kind}"{hunk}>{_ln(no)}<span class="mk">{mk}</span><span class="tx">{body or "&nbsp;"}</span></div>'


def _side_row(r: Row, hunk: str) -> str:
    if r.kind == "eq":
        left = right = html.escape(r.new)
    elif r.kind == "chg":
        left, right = word_sides(r.old, r.new)
    else:
        left = html.escape(r.old) if r.old is not None else None
        right = html.escape(r.new) if r.new is not None else None
    lcell = f'<span class="tx o">{left or "&nbsp;"}</span>' if left is not None else '<span class="tx none"></span>'
    rcell = f'<span class="tx n">{right or "&nbsp;"}</span>' if right is not None else '<span class="tx none"></span>'
    return f'<div class="s {r.kind}"{hunk}>{_ln(r.old_no)}{lcell}{_ln(r.new_no)}{rcell}</div>'


def _view(items, row_fn, fid: str) -> str:
    out, h, prev_changed = [], 0, False
    for kind, x in items:
        if kind == "fold":
            inner = "".join(row_fn(r, "") for r in x)
            out.append(f'<details class="fold"><summary>⋯ {T["folded"].format(n=len(x))}</summary>{inner}</details>')
            prev_changed = False
            continue
        changed = x.kind != "eq"
        hunk = f' data-hunk="{fid}h{h}"' if changed and not prev_changed else ""
        h += 1 if hunk else 0
        if hunk and x.section:
            out.append(f'<div class="hd">{html.escape(x.section)}</div>')
        out.append(row_fn(x, hunk))
        prev_changed = changed
    return "".join(out)


def _badges(f: FileDiff) -> str:
    extra = f' <span class="badge">{T["binary"]}</span>' if f.binary else ""
    return f'<span class="badge {f.kind}">{T[f.kind]}</span>{extra}'


def file_tree(paths: list[str]) -> dict:
    """Folders as nested dicts; each file is kept as (name, index into paths), so equal paths stay apart."""
    root: dict = {"dirs": {}, "files": []}
    for i, p in enumerate(paths):
        *dirs, name = p.split("/")
        node = root
        for d in dirs:
            node = node["dirs"].setdefault(d, {"dirs": {}, "files": []})
        node["files"].append((name, i))
    return root


def _children(node: dict):
    """Sub-folders first, then files, both by name; a chain of lone sub-folders becomes one row ("a/b/c")."""
    dirs = []
    for name in sorted(node["dirs"], key=str.lower):
        child = node["dirs"][name]
        while not child["files"] and len(child["dirs"]) == 1:
            (sub, child), = child["dirs"].items()
            name = f"{name}/{sub}"
        dirs.append((name, child))
    return dirs, sorted(node["files"], key=lambda f: (f[0].lower(), f[1]))


def tree_order(node: dict) -> list[int]:
    """File indices in the order the tree shows them; the sections follow it."""
    dirs, files = _children(node)
    return [i for _, child in dirs for i in tree_order(child)] + [i for _, i in files]


def _tree_html(node: dict, leaves: list[str]) -> str:
    dirs, files = _children(node)
    out = [f'<li class="dir"><details open><summary title="{html.escape(name)}"><span class="dn">{html.escape(name)}</span>'
           f'</summary><ul>{_tree_html(child, leaves)}</ul></details></li>' for name, child in dirs]
    return "".join(out + [leaves[i] for _, i in files])


def _leaf(f: FileDiff, fid: str, plus: int, minus: int) -> str:
    mark, label = KINDS[f.kind], T[f.kind]
    nums = " ".join(x for x in (f'<span class="plus">+{plus}</span>' if plus else "",
                                f'<span class="minus">−{minus}</span>' if minus else "") if x)
    name = f.path.rsplit("/", 1)[-1]
    return (f'<li class="leaf" data-kind="{f.kind}"><a href="#{fid}" title="{html.escape(f.path)} · {label}">'
            f'<span class="st {f.kind}">{mark}</span><span class="fn">{html.escape(name)}</span>'
            f'<span class="nums">{nums or (T["binary"] if f.binary else "")}</span></a></li>')


def _nav(files: list[FileDiff], leaves: list[str]) -> str:
    """The file tree with one filter button per kind of change, when the page has more than one kind."""
    counts = {k: sum(f.kind == k for f in files) for k in KINDS}
    present = [k for k in KINDS if counts[k]]
    flt = "".join(f'<button class="flt {k}" data-kind="{k}" aria-pressed="true">{KINDS[k]} {T[k]} <b>{counts[k]}</b></button>'
                  for k in present) if len(present) > 1 else ""
    return (f'<nav class="tree" aria-label="{T["files"]}"><div class="tree-head">{flt}<button id="fold-all">{T["fold_dirs"]}</button>'
            f'<span class="shown" id="shown"></span></div><ul>{_tree_html(file_tree([f.path for f in files]), leaves)}</ul></nav>')


@functools.lru_cache(maxsize=None)
def _mdredline():
    spec = importlib.util.spec_from_file_location("diffview_mdredline", HERE / "mdredline.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _is_md(f: FileDiff) -> bool:
    return not f.binary and Path(f.path).suffix.lower() in MD_SUFFIXES and f.old != f.new


def _md_view(f: FileDiff, fid: str) -> str:
    """The rendered view of a Markdown file: the redline as JSON, typeset in the browser."""
    src = _mdredline().redline(f.old, f.new)
    payload = json.dumps(src, ensure_ascii=False).replace("<", "\\u003c")
    gone = f'<p class="note">{T["md_deleted"]}</p>' if f.new is None else ""
    return (f'<div class="view md">{gone}<div class="md" data-src="{fid}-md"></div></div>'
            f'<script type="application/json" id="{fid}-md">{payload}</script>')


def _script(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f'<script src="data:text/javascript;base64,{data}"></script>'


def render(files: list[FileDiff], title: str, subtitle: str, context: int = 3, lang: str | None = None) -> str:
    set_lang(lang)
    with_tree = len(files) > 1
    if with_tree:
        files = [files[i] for i in tree_order(file_tree([f.path for f in files]))]
    sections, leaves, total_add, total_del = [], [], 0, 0
    for n, f in enumerate(files, 1):
        fid = f"f{n}"
        prose = " prose" if Path(f.path).suffix.lower() in PROSE_SUFFIXES else ""
        if f.binary:
            plus = minus = 0
            body = f'<p class="note">{T["bin_" + f.kind]}</p>'
        else:
            rows = _rows(f.old, f.new)
            if prose:
                _label_sections(rows)
            plus = sum(r.kind in ("ins", "chg") for r in rows)
            minus = sum(r.kind in ("del", "chg") for r in rows)
            if plus == minus == 0:
                body = f'<p class="note">{T["no_text_change"]}</p>'
            else:
                items = _items(rows, context)
                body = (f'<div class="view inline">{_view(items, _inline_row, fid)}</div>'
                        f'<div class="view side">{_view(items, _side_row, fid + "s")}</div>')
        md = " has-md" if _is_md(f) and (plus or minus) else ""
        if md:
            body += _md_view(f, fid)
        total_add, total_del = total_add + plus, total_del + minus
        counts = "" if f.binary else f' <span class="plus">+{plus}</span> <span class="minus">−{minus}</span>'
        leaves.append(_leaf(f, fid, plus, minus))
        sections.append(f'<section class="file{prose}{md}" id="{fid}" data-kind="{f.kind}"><h2><span class="path">{html.escape(f.path)}</span>'
                        f' {_badges(f)}{counts}</h2>{body}</section>')
    if not files:
        sections.append(f'<p class="note">{T["no_changes"]}</p>')
    if with_tree:
        sections.insert(0, f'<p class="note" id="nomatch" hidden>{T["no_match"]}</p>')
    stamp = _dt.datetime.now().strftime(T["stamp"])
    any_md = any(' has-md"' in s for s in sections)
    new_docs = bool(files) and all(_is_md(f) and f.old is None for f in files)
    body_attrs = (' class="has-tree"' if with_tree else "") + (' data-default-mode="md"' if new_docs and any_md else "")
    i18n = json.dumps({k: T[k] for k in JS_STRINGS}, ensure_ascii=False).replace("<", "\\u003c")
    return PAGE.format(
        title=html.escape(title), subtitle=html.escape(subtitle), stamp=stamp, nfiles=T["nfiles"].format(n=len(files)),
        plus=total_add, minus=total_del, nav=_nav(files, leaves) if with_tree else "", sections="".join(sections), css=CSS, js=JS,
        body_attrs=body_attrs, t=T, i18n=i18n,
        tree_button=f'<button id="tree-btn" aria-pressed="true">{T["files"]}</button>' if with_tree else "",
        md_button=f'<button id="m-md" aria-pressed="false">{T["reading"]}</button>' if any_md else "",
        md_scripts=_script(HERE / "vendor" / "marked.min.js") + _script(HERE / "mdview.js") if any_md else "")


CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#656d76;--line:#d0d7de;--panel:#f6f8fa;--ins:#dafbe1;--ins-strong:#7ee2a0;--ins-fg:#0a3622;--del:#ffebe9;--del-strong:#ffb3ad;--del-fg:#5c0c0c;--chg:#fff8c5;--accent:#0969da;--fold:#eef4ff}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#8d96a0;--line:#30363d;--panel:#161b22;--ins:#12261e;--ins-strong:#1f6f3f;--ins-fg:#aff5c8;--del:#2d1214;--del-strong:#8e1519;--del-fg:#ffdcd7;--chg:#2b2611;--accent:#4493f8;--fold:#121d2f}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header.top{padding:16px 16px 8px;max-width:1500px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:8px 16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;max-width:1500px;margin:0 auto}
.bar button{font:inherit;font-size:14px;padding:4px 10px;border:1px solid var(--line);border-radius:6px;background:var(--panel);color:var(--fg);cursor:pointer}
.bar button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:#fff}
.bar .sep{flex:1}.bar .stat{color:var(--muted);font-size:13px}
main{max-width:1500px;margin:0 auto;padding:8px 16px 48px}
.index{margin:8px 0 16px;padding-left:22px;font-size:14px}.index li{margin:2px 0;overflow-wrap:anywhere}
.index a,.index a:visited{color:var(--accent);text-decoration:none}
section.file{border:1px solid var(--line);border-radius:8px;margin:16px 0;overflow:clip}
section.file>h2{font-size:14px;margin:0;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line);font-weight:600;overflow-wrap:anywhere;position:sticky;top:var(--bar,49px);z-index:3}
.path{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.badge{font-size:11px;font-weight:600;padding:1px 6px;border-radius:10px;border:1px solid var(--line);color:var(--muted);vertical-align:1px}
.badge.add{color:var(--ins-fg);background:var(--ins)}.badge.del{color:var(--del-fg);background:var(--del)}
.plus{color:#1a7f37;font-weight:600}.minus{color:#cf222e;font-weight:600}
@media (prefers-color-scheme:dark){.plus{color:#3fb950}.minus{color:#f85149}}
.note{padding:8px 12px;margin:0;color:var(--muted)}
.view{font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.prose .view{font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.r{display:grid;grid-template-columns:3.2em 1.4em minmax(0,1fr)}
.s{display:grid;grid-template-columns:3.2em minmax(0,1fr) 3.2em minmax(0,1fr)}
.ln{color:var(--muted);text-align:right;padding:0 6px;font:12px/1.9 ui-monospace,monospace;user-select:none;border-right:1px solid var(--line)}
.mk{color:var(--muted);text-align:center;user-select:none}
.tx{white-space:pre-wrap;overflow-wrap:anywhere;padding:1px 8px;min-width:0}
.s .tx.o{border-right:1px solid var(--line)}
.r.ins,.s.ins .tx.n{background:var(--ins)}.r.del,.s.del .tx.o{background:var(--del)}
.r.chg,.s.chg .tx{background:var(--chg)}
.s .tx.none{background:var(--panel)}
.r.del .tx{text-decoration:line-through;text-decoration-color:var(--del-strong)}
ins{background:var(--ins-strong);color:inherit;text-decoration:none;border-radius:2px}
del{background:var(--del-strong);color:inherit;text-decoration:line-through;border-radius:2px}
details.fold>summary{list-style:none;cursor:pointer;background:var(--fold);color:var(--muted);font:12px/2 system-ui,sans-serif;padding:0 12px;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
details.fold>summary::-webkit-details-marker{display:none}
.hd{font:600 12px/1.8 system-ui,sans-serif;color:var(--accent);padding:6px 12px 0;border-top:1px solid var(--line)}
[data-hunk].cur{outline:2px solid var(--accent);outline-offset:-2px}
body:not(.side-mode) .view.side{display:none}body.side-mode .view.inline{display:none}
.view.md{display:none;padding:4px 28px 20px}body.md-mode .view.md{display:block}body.md-mode .file.has-md .view.inline{display:none}
.md{font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;max-width:64em;overflow-wrap:break-word}
.md h1,.md h2,.md h3,.md h4,.md h5,.md h6{line-height:1.3;margin:1.3em 0 .5em}.md h1{font-size:1.6em;padding-bottom:.2em;border-bottom:1px solid var(--line)}
.md h2{font-size:1.35em;padding-bottom:.15em;border-bottom:1px solid var(--line)}.md h3{font-size:1.15em}.md h4,.md h5,.md h6{font-size:1em}
.md p,.md ul,.md ol,.md table,.md pre,.md blockquote{margin:.6em 0}.md ul,.md ol{padding-left:1.8em}.md li{margin:.15em 0}
.md table{border-collapse:collapse;display:block;overflow-x:auto;max-width:100%;font-size:14px}
.md th,.md td{border:1px solid var(--line);padding:4px 8px;vertical-align:top;text-align:left}.md th{background:var(--panel)}
.md code{font:.88em ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--panel);padding:1px 4px;border-radius:4px}
.md pre{background:var(--panel);padding:8px 12px;border-radius:6px;overflow-x:auto}.md pre code{background:none;padding:0;font-size:13px;line-height:1.45}
.md blockquote{padding:0 12px;border-left:4px solid var(--line);color:var(--muted)}.md hr{border:0;border-top:1px solid var(--line)}
.md ins{background:var(--ins);color:inherit;text-decoration:underline;text-decoration-color:#2da44e;text-decoration-thickness:2px}
.md del{background:var(--del);color:var(--del-fg);text-decoration:line-through}
.md pre ins,.md pre del{display:inline-block;width:100%;text-decoration:none}.md pre del{text-decoration:line-through}
.md .md-chg{position:relative}.md .md-chg:not(tr)::before{content:"";position:absolute;left:-14px;top:0;bottom:0;border-left:3px solid #d4a72c}
.md li.md-chg::before{left:-34px}.md tr.md-chg td:first-child{box-shadow:inset 3px 0 0 #d4a72c}
.md-toc{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:13px;color:var(--muted);margin:6px 0 14px}
.md-toc button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--accent);border-radius:12px;padding:1px 9px;cursor:pointer}
.md-link{color:var(--accent);border-bottom:1px dotted var(--accent)}.md-img{display:inline-block;padding:1px 6px;border:1px dashed var(--line);border-radius:4px;color:var(--muted);font-size:13px}
.md a{color:var(--accent)}
[hidden]{display:none!important}
section.file{scroll-margin-top:calc(var(--bar,49px) + 8px)}
.layout{max-width:1500px;margin:0 auto}
body.has-tree header.top,body.has-tree .bar,body.has-tree .layout{max-width:1840px}
body.has-tree .layout{display:grid;grid-template-columns:300px minmax(0,1fr);align-items:start}
body.has-tree main{max-width:none;margin:0;min-width:0}
body.tree-off .layout{grid-template-columns:minmax(0,1fr)}body.tree-off nav.tree{display:none}
nav.tree{position:sticky;top:var(--bar,49px);max-height:calc(100vh - var(--bar,49px));overflow:auto;padding:12px 8px 32px 16px;border-right:1px solid var(--line);font-size:13px;line-height:1.35}
.tree-head{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 10px}
.tree-head button{font:inherit;font-size:12px;padding:2px 9px;border:1px solid var(--line);border-radius:12px;background:var(--panel);color:var(--fg);cursor:pointer}
.tree-head button[aria-pressed=false]{opacity:.5;text-decoration:line-through}
.tree-head b{font-weight:600}.tree-head .shown{color:var(--muted);font-size:12px;flex-basis:100%}.tree-head .shown:empty{display:none}
.flt.add{border-color:#2da44e}.flt.mod{border-color:#d4a72c}.flt.del{border-color:#cf222e}
nav.tree ul{list-style:none;margin:0;padding:0}
nav.tree details>ul{margin-left:7px;padding-left:8px;border-left:1px solid var(--line)}
nav.tree summary{display:flex;list-style:none;cursor:pointer;padding:3px 4px;border-radius:4px;color:var(--muted);font-weight:600}
nav.tree summary::-webkit-details-marker{display:none}nav.tree .dn{min-width:0;overflow-wrap:anywhere}
nav.tree summary::before{content:"▾";flex:none;width:1.1em}nav.tree details:not([open])>summary::before{content:"▸"}
nav.tree summary:hover,.leaf a:hover{background:var(--panel)}
.leaf a{display:flex;gap:6px;align-items:baseline;padding:3px 4px;border-radius:4px;color:var(--fg);text-decoration:none}
.leaf a.cur-file{background:var(--fold);box-shadow:inset 2px 0 0 var(--accent)}
.leaf .st{flex:none;width:1em;text-align:center;font-weight:700}
.st.add{color:#1a7f37}.st.mod{color:#9a6700}.st.del{color:#cf222e}
.leaf .fn{flex:1;min-width:0;overflow-wrap:anywhere;font:12.5px/1.35 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.leaf[data-kind=del] .fn{color:var(--muted)}.leaf .nums{flex:none;font-size:11px;color:var(--muted)}
@media (prefers-color-scheme:dark){.st.add{color:#3fb950}.st.mod{color:#d29922}.st.del{color:#f85149}}
@media (max-width:1000px){body.has-tree .layout{display:block}
nav.tree{position:fixed;top:var(--bar,49px);left:0;right:0;z-index:6;max-height:calc(85vh - var(--bar,49px));background:var(--bg);border-right:0;border-bottom:1px solid var(--line);box-shadow:0 12px 28px rgba(0,0,0,.25);padding:12px 16px 20px}}
@media (max-width:700px){.view.md{padding:4px 14px 14px}.md{font-size:15px}.r{grid-template-columns:2.6em 1.1em minmax(0,1fr)}.s{grid-template-columns:2.2em minmax(0,1fr) 2.2em minmax(0,1fr)}.prose .view{font-size:14px}}
"""

JS = """
(function(){
  var T=window.DVT||{}, b=document.body, KEY='diffview-mode', bar=document.querySelector('.bar'), rendered=false;
  var hasMd=!!document.querySelector('.view.md'), btn={inline:'m-inline',side:'m-side',md:'m-md'};
  function off(){return bar.offsetHeight+60}
  function scrollToEl(t){window.scrollTo({top:t.getBoundingClientRect().top+window.scrollY-off(),behavior:'smooth'})}
  function markChanges(el,fid){
    var BLOCK='p,li,tr,h1,h2,h3,h4,h5,h6,pre,blockquote,dt,dd', seen=[];
    el.querySelectorAll('ins,del').forEach(function(m){var k=m.closest(BLOCK);if(k&&el.contains(k)&&seen.indexOf(k)<0)seen.push(k)});
    seen.forEach(function(k,i){k.classList.add('md-chg');k.setAttribute('data-hunk',fid+'m'+i)});
    var list=[], cur=null;
    el.querySelectorAll('h1,h2,h3,h4,h5,h6,.md-chg').forEach(function(x){
      if(/^H[1-6]$/.test(x.tagName)){cur=x;if(!x.classList.contains('md-chg'))return}
      if(list.indexOf(cur)<0)list.push(cur)});
    if(!list.length)return;
    var box=document.createElement('div');box.className='md-toc';
    box.appendChild(document.createTextNode(T.sections+' ('+list.length+'):'));
    list.forEach(function(h){var t=document.createElement('button');t.type='button';
      t.textContent=h?h.textContent:T.top;t.onclick=function(){scrollToEl(h||el)};box.appendChild(t)});
    el.insertBefore(box,el.firstChild)}
  function renderMd(){if(rendered||!window.DVMarkdown)return;rendered=true;
    document.querySelectorAll('.md[data-src]').forEach(function(el){var id=el.getAttribute('data-src');
      el.innerHTML=window.DVMarkdown.render(JSON.parse(document.getElementById(id).textContent));
      markChanges(el,id)})}
  function setMode(mode,save){if(mode==='md'&&!hasMd)mode='inline';
    b.classList.toggle('side-mode',mode==='side');b.classList.toggle('md-mode',mode==='md');
    if(mode==='md')renderMd();
    Object.keys(btn).forEach(function(k){var e=document.getElementById(btn[k]);if(e)e.setAttribute('aria-pressed',k===mode)});
    if(save){try{localStorage.setItem(KEY,mode)}catch(e){}}}
  var mode='inline';try{mode=localStorage.getItem(KEY)||'inline'}catch(e){}
  if(b.getAttribute('data-default-mode'))mode=b.getAttribute('data-default-mode');
  var q=location.search;if(/[?&]side(?:[&=]|$)/.test(q))mode='side';else if(/[?&]inline(?:[&=]|$)/.test(q))mode='inline';
  else if(/[?&]md(?:[&=]|$)/.test(q))mode='md';
  setMode(mode,false);
  Object.keys(btn).forEach(function(k){var e=document.getElementById(btn[k]);if(e)e.onclick=function(){setMode(k,true)}});
  var open=false;document.getElementById('unfold').onclick=function(){open=!open;
    document.querySelectorAll('details.fold').forEach(function(d){d.open=open});
    this.textContent=open?T.close_all:T.open_all};
  function hunks(){return Array.prototype.filter.call(document.querySelectorAll('[data-hunk]'),function(e){return e.getClientRects().length>0})}
  function barH(){document.documentElement.style.setProperty('--bar',bar.offsetHeight+'px')}
  barH();window.addEventListener('resize',barH);
  function go(dir){var hs=hunks();if(!hs.length)return;var y=window.scrollY+off()+2,t=null;
    if(dir>0){for(var i=0;i<hs.length;i++){if(hs[i].getBoundingClientRect().top+window.scrollY>y){t=hs[i];break}}if(!t)t=hs[0]}
    else{for(var j=hs.length-1;j>=0;j--){if(hs[j].getBoundingClientRect().top+window.scrollY<y-4){t=hs[j];break}}if(!t)t=hs[hs.length-1]}
    document.querySelectorAll('.cur').forEach(function(e){e.classList.remove('cur')});t.classList.add('cur');scrollToEl(t)}
  document.getElementById('next').onclick=function(){go(1)};document.getElementById('prev').onclick=function(){go(-1)};
  document.addEventListener('keydown',function(e){if(e.target.closest('input,textarea'))return;
    if(e.key==='n'||e.key==='j')go(1);else if(e.key==='p'||e.key==='k')go(-1)});
  var nav=document.querySelector('nav.tree');if(!nav)return;
  var tb=document.getElementById('tree-btn'),narrow=window.matchMedia('(max-width:1000px)');
  function placeTree(){if(!narrow.matches){nav.style.top=nav.style.maxHeight='';return}
    var y=Math.max(0,bar.getBoundingClientRect().bottom);
    nav.style.top=y+'px';nav.style.maxHeight=Math.max(160,window.innerHeight-y-32)+'px'}
  function setTree(on){b.classList.toggle('tree-off',!on);tb.setAttribute('aria-pressed',on?'true':'false');placeTree()}
  setTree(!narrow.matches);window.addEventListener('resize',placeTree);
  tb.onclick=function(){setTree(b.classList.contains('tree-off'))};
  nav.addEventListener('click',function(e){if(e.target.closest('li.leaf a')&&narrow.matches)setTree(false)});
  var hide={},hm=/[?&]hide=([^&#]*)/.exec(location.search);
  if(hm)decodeURIComponent(hm[1]).split(',').forEach(function(k){if(k)hide[k]=true});
  var secs=document.querySelectorAll('section.file[data-kind]'),flts=nav.querySelectorAll('button[data-kind]');
  var links={},cur=null,ticking=false;
  nav.querySelectorAll('li.leaf a').forEach(function(a){links[a.getAttribute('href').slice(1)]=a});
  function spy(){ticking=false;placeTree();var lim=bar.offsetHeight+12,hit=null;
    for(var i=0;i<secs.length;i++){if(secs[i].hidden)continue;
      if(!hit||secs[i].getBoundingClientRect().top<=lim)hit=secs[i];else break}
    var a=hit?links[hit.id]:null;if(a===cur)return;
    if(cur)cur.classList.remove('cur-file');cur=a;if(!a)return;a.classList.add('cur-file');
    if(narrow.matches||!a.getClientRects().length)return;
    var r=a.getBoundingClientRect(),n=nav.getBoundingClientRect();
    if(r.top<n.top+30||r.bottom>n.bottom-10)nav.scrollTop+=r.top-n.top-n.height/3}
  function applyFilter(){var shown=0;
    secs.forEach(function(s){s.hidden=!!hide[s.getAttribute('data-kind')];if(!s.hidden)shown++});
    nav.querySelectorAll('li.leaf').forEach(function(l){l.hidden=!!hide[l.getAttribute('data-kind')]});
    nav.querySelectorAll('li.dir').forEach(function(d){d.hidden=!d.querySelector('li.leaf:not([hidden])')});
    flts.forEach(function(x){x.setAttribute('aria-pressed',hide[x.getAttribute('data-kind')]?'false':'true')});
    document.getElementById('shown').textContent=shown<secs.length?T.shown.replace('{shown}',shown).replace('{total}',secs.length):'';
    document.getElementById('nomatch').hidden=shown>0;spy()}
  flts.forEach(function(x){x.onclick=function(){var k=x.getAttribute('data-kind');hide[k]=!hide[k];applyFilter()}});
  var fa=document.getElementById('fold-all'),dirsOpen=true;
  fa.onclick=function(){dirsOpen=!dirsOpen;
    nav.querySelectorAll('details').forEach(function(d){d.open=dirsOpen});
    fa.textContent=dirsOpen?T.fold_dirs:T.open_dirs};
  window.addEventListener('scroll',function(){if(!ticking){ticking=true;requestAnimationFrame(spy)}},{passive:true});
  applyFilter();
})();
"""

PAGE = """<!doctype html>
<html lang="{t[html_lang]}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><meta name="referrer" content="no-referrer">
<title>{title}</title><style>{css}</style></head>
<body{body_attrs}><header class="top"><h1>{title}</h1><div class="sub">{subtitle} · {t[created]} {stamp}</div></header>
<div class="bar">{tree_button}<button id="m-inline" aria-pressed="true">{t[inline]}</button><button id="m-side" aria-pressed="false">{t[side]}</button>{md_button}
<button id="unfold">{t[open_all]}</button><button id="prev" title="{t[prev_title]}">{t[prev]}</button><button id="next" title="{t[next_title]}">{t[next]}</button>
<span class="sep"></span><span class="stat">{nfiles} · <span class="plus">+{plus}</span> <span class="minus">−{minus}</span></span></div>
<div class="layout">{nav}<main>{sections}</main></div><script>window.DVT={i18n};</script>{md_scripts}<script>{js}</script></body></html>
"""


# ---------------------------------------------------------------- serving

def parse_addresses(text: str) -> list[tuple[str, str]]:
    """LAN and Tailscale IPv4 addresses from `ip -4 -o addr show` output, LAN first."""
    found = []
    for line in text.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)/", line)
        if not m or SKIP_IFACES.match(m.group(1)) or "scope host" in line:
            continue
        found.append((m.group(1), m.group(2)))
    return sorted(found, key=lambda x: x[1].startswith("100.") or x[0].startswith("tailscale"))


def addresses() -> list[tuple[str, str]]:
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        out = ""
    found = parse_addresses(out)
    return found or [("localhost", "127.0.0.1")]


def env_lan() -> bool:
    return os.environ.get(LAN_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def urls(port: int, page: str = "", lan: bool = False) -> list[str]:
    """Page URLs: LAN and Tailscale addresses for a --lan server, else localhost."""
    found = addresses() if lan else [("this machine only", "localhost")]
    return [f"http://{ip}:{port}/{page}  ({iface})" for iface, ip in found]


def _marker(out_dir: Path) -> Path:
    return out_dir / ".server.json"


def running_server(out_dir: Path) -> int | None:
    """Port of a live diffview server for this directory, else None."""
    try:
        info = json.loads(_marker(out_dir).read_text())
        os.kill(int(info["pid"]), 0)
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=0.5):
            return int(info["port"])
    except (OSError, ValueError, KeyError):
        return None


def server_lan(out_dir: Path) -> bool:
    """Whether the running server listens on every interface (servers from before --lan always did)."""
    try:
        return bool(json.loads(_marker(out_dir).read_text()).get("lan", True))
    except (OSError, ValueError, AttributeError):
        return False


def _free_port(start: int, host: str = "127.0.0.1") -> int:
    for port in range(start, start + 30):
        with socket.socket() as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    sys.exit(f"no free port in {start}..{start + 29}")


class _Handler(http.server.SimpleHTTPRequestHandler):
    def send_head(self):
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if any(part.startswith(".") for part in path.split("/") if part):
            self.send_error(404)
            return None
        return super().send_head()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


def serve(out_dir: Path, port: int, lan: bool = False) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    if running_server(out_dir):
        return status(out_dir, wait=0)
    host = "0.0.0.0" if lan else "127.0.0.1"
    port = _free_port(port, host)
    _write_index(out_dir)
    httpd = http.server.ThreadingHTTPServer((host, port), functools.partial(_Handler, directory=str(out_dir)))
    _marker(out_dir).write_text(json.dumps({"pid": os.getpid(), "port": port, "lan": lan}))

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    print(f"Serving {out_dir} on port {port}, pid {os.getpid()}. Stop with: kill {os.getpid()}", flush=True)
    if lan:
        print("Listening on every interface: anyone on these networks can read the pages while the server runs.", flush=True)
    else:
        print("Only this machine can open the pages. For another device (a phone, a remote session), "
              f"restart with --lan or {LAN_ENV}=1.", flush=True)
    print("\n".join(urls(port, lan=lan)), flush=True)
    for page in _pages(out_dir)[:1]:
        print("Newest page:\n" + "\n".join(urls(port, page.name, lan)), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _marker(out_dir).unlink(missing_ok=True)
    return 0


def status(out_dir: Path, wait: float = 5.0) -> int:
    deadline = time.time() + wait
    port = running_server(out_dir)
    while not port and time.time() < deadline:
        time.sleep(0.2)
        port = running_server(out_dir)
    if not port:
        print(f"No server for {out_dir}.")
        return 1
    pid = json.loads(_marker(out_dir).read_text())["pid"]
    lan = server_lan(out_dir)
    print(f"Serving {out_dir} on port {port}, pid {pid}. Stop with: kill {pid}")
    print("\n".join(urls(port, lan=lan)))
    pages = _pages(out_dir)[:5]
    if pages:
        print("Pages, newest first:")
        for page in pages:
            print("\n".join(urls(port, page.name, lan)))
    return 0


def _pages(out_dir: Path) -> list[Path]:
    return sorted((p for p in out_dir.glob("*.html") if p.name != "index.html"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def _write_index(out_dir: Path) -> None:
    pages = _pages(out_dir)
    items = "".join(
        f'<li><a href="{html.escape(p.name)}">{html.escape(p.stem)}</a> '
        f'<span class="sub">{_dt.datetime.fromtimestamp(p.stat().st_mtime).strftime(T["stamp"])}</span></li>'
        for p in pages)
    body = f'<ol class="index">{items}</ol>' if pages else f'<p class="note">{T["no_pages"]}</p>'
    (out_dir / "index.html").write_text(
        f'<!doctype html><html lang="{T["html_lang"]}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>Diffs</title><style>{CSS}</style></head><body><header class="top"><h1>Diffs</h1></header>'
        f'<main>{body}</main></body></html>', encoding="utf-8")


# ---------------------------------------------------------------- CLI

def _root(cwd: Path) -> Path:
    res = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(res.stdout.strip()) if res.returncode == 0 else cwd


def _exclude(root: Path) -> None:
    """Keep the output directory out of `git status` without touching tracked files."""
    res = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"], capture_output=True, text=True)
    if res.returncode != 0:
        return
    path = Path(res.stdout.strip())
    path = path if path.is_absolute() else root / path
    line = f"/{OUT_DIRNAME}/"
    text = path.read_text() if path.exists() else ""
    if line not in text.splitlines():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + ("" if text.endswith("\n") or not text else "\n") + line + "\n")


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-.") or "diff"


def _untracked_named(root: Path, git_dir: str, rel: list[str], known: set[str]) -> list[FileDiff]:
    """Files named explicitly that the overlay does not track: new documents. Directories are left out,
    because untracked there means every file of the outer repository."""
    out = []
    for path in rel:
        f = root / path
        if path in known or not f.is_file() or _git(root, git_dir, "ls-files", "-z", "--", path).strip(b"\x00"):
            continue
        new, nb = _decode(f.read_bytes())
        out.append(FileDiff(path, None, new, nb, "add"))
    return out


def build(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="diffview.py", description="Build an HTML diff page.")
    ap.add_argument("paths", nargs="*", help="limit to these paths (git mode)")
    ap.add_argument("--files", nargs=2, metavar=("OLD", "NEW"), help="compare two files instead of git")
    ap.add_argument("--new", nargs="+", metavar="FILE", help="show files as new documents (no old version)")
    ap.add_argument("--from", dest="rev_from", default="HEAD", help="old revision (default HEAD)")
    ap.add_argument("--to", dest="rev_to", help="new revision (default: working tree)")
    ap.add_argument("--git-dir", help="separate git dir whose work tree is the root (overlay repos)")
    ap.add_argument("--no-untracked", action="store_true", help="leave out untracked files")
    ap.add_argument("--name", help="file name of the page, without .html")
    ap.add_argument("--title", help="page title")
    ap.add_argument("--context", type=int, default=3, help="unchanged lines shown around a change")
    ap.add_argument("--out-dir", help=f"output directory (default <root>/{OUT_DIRNAME})")
    ap.add_argument("--lang", choices=sorted(STRINGS), help=f"page language (default ${LANG_ENV}, else en)")
    a = ap.parse_args(argv)
    set_lang(a.lang)

    cwd = Path.cwd()
    root = cwd if a.git_dir else _root(cwd)
    if a.new:
        files = []
        for p in a.new:
            path = Path(p).expanduser()
            path = path if path.is_absolute() else cwd / path
            new, nb = _decode(path.read_bytes())
            try:
                shown = str(path.resolve().relative_to(root.resolve()))
            except ValueError:
                shown = path.name
            files.append(FileDiff(shown, None, new, nb, "add"))
        subtitle = f"{root} · {T['new_docs']}"
        name = a.name or _slug(Path(a.new[0]).stem)
    elif a.files:
        old_p, new_p = (Path(p) for p in a.files)
        old, ob = _decode(old_p.read_bytes())
        new, nb = _decode(new_p.read_bytes())
        files = [FileDiff(new_p.name if old_p.name == new_p.name else f"{old_p.name} → {new_p.name}", old, new, ob or nb)]
        subtitle = f"{old_p} → {new_p}"
        name = a.name or _slug(new_p.stem)
    else:
        rel = []
        for p in a.paths:
            ap_ = Path(p).expanduser()
            ap_ = ap_ if ap_.is_absolute() else cwd / ap_
            try:
                rel.append(str(ap_.resolve().relative_to(root.resolve())))
            except ValueError:
                sys.exit(f"{p} is outside {root}")
        untracked = not a.no_untracked and not a.git_dir
        files = collect_git(root, a.rev_from, a.rev_to, rel, untracked, a.git_dir)
        if a.git_dir and not a.rev_to:
            files += _untracked_named(root, a.git_dir, rel, {f.path for f in files})
        subtitle = f"{root} · {a.rev_from} → {a.rev_to or T['worktree']}" + (f" · {', '.join(rel)}" if rel else "")
        name = a.name or (_slug(Path(rel[0]).name) if len(rel) == 1 else T["default_name"])
    one, many = ("title_new_one", "title_new_many") if a.new else ("title_one", "title_many")
    title = a.title or (T[one].format(path=files[0].path) if len(files) == 1 else T[many].format(root=root.name))

    out_dir = Path(a.out_dir).expanduser() if a.out_dir else root / OUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    if not a.out_dir:
        _exclude(root)
    page = out_dir / f"{name}.html"
    page.write_text(render(files, title, subtitle, a.context, a.lang), encoding="utf-8")
    _write_index(out_dir)
    print(f"Wrote {page} ({len(files)} file(s))")
    port = running_server(out_dir)
    if port:
        print("\n".join(urls(port, page.name, server_lan(out_dir))))
    else:
        script = os.path.abspath(__file__)
        extra = f" --out-dir {out_dir}" if a.out_dir else ""
        lan_note = (f"(listens on your network: {LAN_ENV} is set)" if env_lan()
                    else "(this machine only; add --lan to open the page from a phone or a remote session)")
        print(f"No server for {out_dir} yet. From this directory, start in the background:\n"
              f"  python3 -I {script} serve{extra}\n"
              f"  {lan_note}\n"
              f"then get PID and page URLs with:\n"
              f"  python3 -I {script} status{extra}")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] in ("serve", "status"):
        ap = argparse.ArgumentParser(prog=f"diffview.py {argv[0]}",
                                     description="Serve the diff pages (localhost; --lan for LAN and Tailscale)."
                                     if argv[0] == "serve" else "Show PID, port and page URLs of the running server.")
        if argv[0] == "serve":
            ap.add_argument("--port", type=int, default=DEFAULT_PORT)
            ap.add_argument("--lan", action="store_true",
                            help=f"listen on every interface and print LAN and Tailscale URLs (default ${LAN_ENV})")
        ap.add_argument("--out-dir", help=f"directory to serve (default <root>/{OUT_DIRNAME})")
        a = ap.parse_args(argv[1:])
        out_dir = (Path(a.out_dir).expanduser() if a.out_dir else _root(Path.cwd()) / OUT_DIRNAME).resolve()
        return serve(out_dir, a.port, a.lan or env_lan()) if argv[0] == "serve" else status(out_dir)
    return build(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
