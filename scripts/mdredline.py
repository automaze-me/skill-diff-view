"""Markdown redline: the new text of a Markdown document with the changes marked as <ins> and <del>.

Adapted from the redline of the 1590 concept documents (scripts/redline.py there), without the PDF parts.
Lines are compared as units (a fenced code block is one unit); within a paired line or table cell the
comparison runs word by word. Bold markers and HTML tags stay outside the marks, so the result is still
valid Markdown. A table that a formatter only realigned counts as unchanged. A changed code block shows
its removed and added lines in one <pre> block. Front matter is compared like a code block.
"""
from __future__ import annotations

import difflib
import html
import re

FENCE = re.compile(r"^\s*```")
SEPARATOR = re.compile(r"^\s*\|(?:\s*:?-+:?\s*\|)+\s*$")
PREFIX = re.compile(r"^(\s*(?:>\s?)*(?:#{1,6}\s+|(?:[-*+]|\d+\.)\s+(?:\[[ xX]\]\s+)?)?)")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
ONLY_HTML = re.compile(r"^\s*(?:<[^>]+>\s*)+$")
TOKEN = re.compile(r"`[^`]*`|!?\[[^\]]*\]\([^)]*\)|<[^>\s][^>]*>|\*\*|\s+|\w+|[^\w\s]")
MARKER = re.compile(r"\*\*|<[^>\s][^>]*>")
SIMILAR = 0.45


def cells(line: str) -> list[str]:
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") and not inner.endswith("\\|") else inner
    return [c.strip() for c in re.split(r"(?<!\\)\|", inner)]


class Unit:
    """One line, or a whole fenced code block."""

    def __init__(self, lines: list[str], code: bool = False):
        self.lines = lines
        self.text = "\n".join(lines)
        line = lines[0]
        if code:
            self.kind = "code"
        elif not line.strip():
            self.kind = "blank"
        elif SEPARATOR.match(line):
            self.kind = "sep"
        elif line.lstrip().startswith("|"):
            self.kind = "row"
        elif HEADING.match(line):
            self.kind = "heading"
        elif ONLY_HTML.match(line):
            self.kind = "html"
        else:
            self.kind = "text"
        if self.kind == "sep":
            self.key = "sep" + str(len(cells(line)))
        elif self.kind == "row":
            self.key = "|".join(cells(line))
        elif self.kind == "code":
            self.key = self.text
        else:
            self.key = " ".join(line.split())
        self.group = {"sep": "row", "heading": "text"}.get(self.kind, self.kind)


def _front_matter_as_code(src: str) -> str:
    """Front matter becomes a fenced block, so it is compared and shown like code."""
    if src.startswith("---\n"):
        end = src.find("\n---\n", 4)
        if end != -1:
            return "```yaml\n" + src[4:end] + "\n```\n" + src[end + 5:]
    return src


def units(body: str) -> list[Unit]:
    lines = body.split("\n")
    out, i = [], 0
    while i < len(lines):
        if FENCE.match(lines[i]):
            j = i + 1
            while j < len(lines) and not FENCE.match(lines[j]):
                j += 1
            out.append(Unit(lines[i: j + 1], code=True))
            i = j + 1
        else:
            out.append(Unit([lines[i]]))
            i += 1
    return out


def mark(tokens: list[str], tag: str) -> str:
    """Mark all tokens as added or removed. Bold markers and HTML tags stay outside the mark: in added text
    they stand unmarked, in removed text they are dropped, so nesting follows the new text."""
    out, buf = [], []

    def flush():
        text = "".join(buf)
        buf.clear()
        if not text.strip():
            out.append(text if tag == "ins" else "")
            return
        lead = text[: len(text) - len(text.lstrip())]
        trail = text[len(text.rstrip()):]
        out.append(f"{lead}<{tag}>{text.strip()}</{tag}>{trail}")

    for token in tokens:
        if MARKER.fullmatch(token):
            flush()
            if tag == "ins":
                out.append(token)
        else:
            buf.append(token)
    flush()
    return "".join(out)


def inline(old: str, new: str) -> str:
    a, b = TOKEN.findall(old), TOKEN.findall(new)
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    # A short equal piece between two changes (a space, "und", a comma) joins the change.
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
    out = []
    for tag, i1, i2, j1, j2 in merged:
        if tag == "equal":
            out.append("".join(b[j1:j2]))
        else:
            out.append(mark(a[i1:i2], "del") + mark(b[j1:j2], "ins"))
    return "".join(out)


def ratio(old: Unit, new: Unit) -> float:
    a = [x for x in TOKEN.findall(old.key) if not x.isspace()]
    b = [x for x in TOKEN.findall(new.key) if not x.isspace()]
    m = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return m.ratio() if m.quick_ratio() >= 0.4 else 0.0


def pair(olds: list[Unit], news: list[Unit]) -> list[tuple[Unit | None, Unit | None]]:
    """Pair old and new units of a changed block, most similar first, keeping the order."""
    best, bi, bj = 0.0, -1, -1
    if len(olds) * len(news) <= 40000:
        for i, o in enumerate(olds):
            for j, n in enumerate(news):
                if o.group != n.group or o.kind == "blank":
                    continue
                r = 1.0 if o.kind == n.kind == "sep" else ratio(o, n)
                if r > best:
                    best, bi, bj = r, i, j
    if best < SIMILAR:
        return [(o, None) for o in olds] + [(None, n) for n in news]
    return pair(olds[:bi], news[:bj]) + [(olds[bi], news[bj])] + pair(olds[bi + 1:], news[bj + 1:])


def whole_line(line: str, tag: str) -> str:
    prefix = PREFIX.match(line).group(1)
    return prefix + mark(TOKEN.findall(line[len(prefix):]), tag)


def row(old: str | None, new: str | None) -> str:
    if old is None or new is None:
        tag = "ins" if old is None else "del"
        return "| " + " | ".join(mark(TOKEN.findall(c), tag) for c in cells(new or old)) + " |"
    oc, nc = cells(old), cells(new)
    if len(oc) != len(nc):
        return row(old, None) + "\n" + row(None, new)
    return "| " + " | ".join(n if o == n else inline(o, n) for o, n in zip(oc, nc)) + " |"


def code(old: Unit | None, new: Unit | None) -> str:
    """A changed code block as one <pre> block with removed and added lines marked."""
    a = old.lines[1:-1] if old is not None else []
    b = new.lines[1:-1] if new is not None else []
    indent = re.match(r"^\s*", (new or old).lines[0]).group(0)
    rows = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            rows += [html.escape(line[len(indent):] if line.startswith(indent) else line) for line in b[j1:j2]]
        else:
            rows += [f"<del>{html.escape(line[len(indent):] if line.startswith(indent) else line)}</del>" for line in a[i1:i2]]
            rows += [f"<ins>{html.escape(line[len(indent):] if line.startswith(indent) else line)}</ins>" for line in b[j1:j2]]
    return "<pre><code>" + "\n".join(rows) + "</code></pre>"


def redline(old: str | None, new: str | None) -> str:
    """Markdown of the new text with the changes against the old one marked; a new or deleted document unmarked."""
    if old is None or new is None:
        return new if new is not None else (old or "")
    if old == new:
        return new
    a, b = units(_front_matter_as_code(old)), units(_front_matter_as_code(new))
    out: list[str] = []

    def emit(o: Unit | None, n: Unit | None) -> None:
        if o is not None and n is not None and o.key == n.key:
            out.append(n.text)
            return
        unit = n or o
        if unit.kind == "blank":
            out.append("")
        elif unit.kind == "html":
            if n is not None:
                out.append(n.text)
        elif unit.kind == "code" or (o is not None and o.kind == "code") or (n is not None and n.kind == "code"):
            if o is not None and o.kind != "code":
                out.append(whole_line(o.text, "del"))
                o = None
            if n is not None and n.kind != "code":
                out.append(whole_line(n.text, "ins"))
                n = None
            if o is not None or n is not None:
                out.append(code(o, n))
        elif unit.kind == "sep":
            out.append((n or o).text)
        elif unit.kind == "row":
            out.append(row(o.text if o else None, n.text if n else None))
        elif o is not None and n is not None:
            if o.kind == n.kind and ratio(o, n) >= SIMILAR:
                out.append(inline(o.text, n.text))
            else:
                out.append(whole_line(o.text, "del") + "\n\n" + whole_line(n.text, "ins"))
        else:
            out.append(whole_line(unit.text, "ins" if n is not None else "del"))

    ops = difflib.SequenceMatcher(None, [u.key for u in a], [u.key for u in b], autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in ops:
        if tag == "equal":
            for o, n in zip(a[i1:i2], b[j1:j2]):
                emit(o, n)
        else:
            for o, n in pair(a[i1:i2], b[j1:j2]):
                emit(o, n)

    # Blank lines between table rows (left over from a removed blank line next to a table) would split the table.
    lines = "\n".join(out).split("\n")
    kept = [line for k, line in enumerate(lines)
            if line.strip() or not (0 < k < len(lines) - 1
                                    and lines[k - 1].lstrip().startswith("|") and lines[k + 1].lstrip().startswith("|"))]
    return "\n".join(kept)
