---
name: diff-view
description: Use when the user wants to see, review or compare changes ("show me the diff", "what changed?", "redline", "compare"): uncommitted work, revisions, a branch, a whole folder, any two files, or two versions of your own answer. Also use instead of pasting a long diff into the chat or pointing the user to an editor's diff view.
---

# diff-view

## Overview

One command turns a diff into a self-contained HTML page; a second one serves it to a browser. The page has word-level marks, inline and side-by-side views, folded unchanged runs, the Markdown heading above each change, dark mode, and it works on a phone. A page with several files gets a folder tree beside the diff (collapsible folders, the current file marked, on a phone behind the "Files" button) with filters for new, changed and deleted files. Markdown files also get the "Reading view": the document typeset, additions underlined green, deletions struck red, and a list of the changed sections.

The page goes to `<root>/.diffview/` (root = git top level of the current directory, or the current directory). The script adds `/.diffview/` to `.git/info/exclude`, so nothing shows up in `git status`. The server serves only that folder.

`DV` below stands for `python3 -I <this skill's folder>/scripts/diffview.py`, usually `python3 -I ~/.claude/skills/diff-view/scripts/diffview.py`.

## Steps

1. **Build**, from inside the project: see the table below.
2. **Serve.** If the build says "No server … yet", start `DV serve` from the same directory as a background process (in Claude Code: the Bash tool's `run_in_background`), then run `DV status` (it waits for the server). The server takes port 8770 or the next free one; `status` prints the PID and the URL of every page, and the root URL lists all pages. If a server for that folder already runs, the build itself prints the page URLs; do not start a second one.
3. **Where the user reads it.** By default the server listens on localhost only. If the user is not at this machine (an SSH or remote session, a phone), start it with `DV serve --lan` instead: it listens on every interface and prints LAN and Tailscale URLs. Tell the user that anyone on that network can read the pages while the server runs. `DIFFVIEW_LAN=1` in the environment makes `--lan` the default.
4. **Hand over the page URLs the script printed** (ending in `<name>.html`). Never a file path, never an address typed from memory.
5. **After more edits**, run the same build again. It overwrites the page, and the user reloads.
6. **When the user is done**, stop the server by the PID from `DV status`: `kill <pid>`. Never `pkill -f`. The `.diffview/` folder may stay (git ignores it); remove it with `rm -r .diffview` if the user wants it gone.

## Quick reference

| Want | Command |
|---|---|
| all uncommitted changes (staged and unstaged), new files included | `DV` |
| only some files or folders | `DV docs/a.md src/` |
| against an older commit | `DV docs/a.md --from abc123` |
| a branch against main | `DV --from $(git merge-base origin/main HEAD) --to HEAD` |
| two versions of your own answer (the user asks what changed after a revision) | write both answers verbatim into two files in a temporary folder, then `DV --files before.md after.md --title "…"` |
| two arbitrary files | `DV --files old.md new.md` |
| a new document to review (no old version), e.g. a fresh spec or plan | `DV --new docs/plan.md` |
| files in an overlay repo (separate git dir over the same work tree) | `DV --git-dir ~/.notes-overlay.git notes/plan.md`, run from the work-tree root; naming a file the overlay does not track shows it as new |
| page name, title, more context | `--name release-notes --title "…" --context 10` |
| page labels in German | `--lang de`, or `DIFFVIEW_LANG=de` in the environment |

Without `--to`, the new side is the working tree. With `--git-dir`, untracked files are left out (they would list the whole outer repo). `DV --help` lists every option.

On the page: "Inline" / "Side by side" / "Reading view" (Markdown only), "Expand all", keys `n` / `p` jump between changes, `?side` or `?md` in the URL opens that view, `?hide=del` (a comma list of `add`, `mod`, `del`) opens it with those files filtered out. A page of new Markdown documents opens in the Reading view. For reviewing a Markdown document, hand over the URL with `?md`.

## Common mistakes

- **`python3 -m http.server` in the repo root** exposes every file (`.env` included). `DV serve` serves only `.diffview/`.
- **Retyping an earlier answer from memory** for the two-answers case: copy it verbatim, or the page shows changes that never happened.
- **Handing over a file path**, or a page written somewhere the user's browser cannot reach: keep the default output folder and hand over the URLs.
- **Diffing secrets**: the page contains the file contents, and anyone who can reach the server can read it. Leave out `.env` and credential files.
- **Wrong directory**: the root comes from the current directory. For an overlay repo, `cd` to the work-tree root first.

Tests for the scripts: `python3 -I -m unittest discover -s tests` in the skill folder (the Markdown renderer tests need `node`, the browser tests `google-chrome`; both skip when missing). `scripts/vendor/marked.min.js` is marked 15.0.12 (MIT, licence next to it), embedded into pages that have a Markdown view.
