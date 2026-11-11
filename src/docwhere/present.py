"""How results reach the user: terms marked in the terminal, and result pages saved and
opened in the docwhere window or the browser."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


class EvidenceStep(argparse.Action):
    """--quote and --figure add a piece of evidence to ``steps`` in the order given (and
    --quote also to ``quote``); --note says what the piece before it shows."""

    def __call__(self, parser, namespace, value, option_string=None):
        steps = list(getattr(namespace, "steps", None) or [])
        if self.dest == "note":
            if not steps:
                parser.error("--note explains the --quote or --figure before it: give that first")
            if steps[-1][2]:
                parser.error("one --note per --quote or --figure")
            steps[-1] = (steps[-1][0], steps[-1][1], value)
        else:
            steps.append((self.dest, value, ""))
            setattr(namespace, self.dest, [*(getattr(namespace, self.dest, None) or []), value])
        namespace.steps = steps


def show_page(db: Path, build, title: str, options, open_it: bool) -> str:
    """Build a result page (``build(in_window)``), save and sign it, and open it in the docwhere
    window or the browser; returns the saved page's path."""
    from docwhere import window
    from docwhere.opener import start_file
    from docwhere.viewer import save_view

    in_window = open_it and not options.browser and window.mode(db) == "window" and window.available()
    page = build(in_window)

    def sign(path: Path) -> None:
        try:
            window.sign_page(db, path)
        except OSError as e:  # e.g. a key file that is not this user's
            print(f"docwhere: the page was not signed, so the window will not run its reader: {e}", file=sys.stderr)

    out = save_view(db.with_name("views"), page, title, options.answer, sign=sign)
    if not open_it:
        return str(out)
    if not options.browser and window.mode(db) == "window":
        if window.available() and window.show(db, out):
            return str(out)
        print("docwhere: the docwhere window could not start (or the index folder is shared); opening the page in the browser", file=sys.stderr)
    start_file(str(out))
    return str(out)


def highlight(text: str, terms: list[str]) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    low = text.lower()
    marks = [False] * len(text)
    for t in terms:
        t, start = t.lower(), 0
        while t and (i := low.find(t, start)) >= 0:
            marks[i : i + len(t)] = [True] * len(t)
            start = i + 1
    out, on = [], False
    for ch, m in zip(text, marks):
        if m != on:
            out.append("\x1b[1;33m" if m else "\x1b[0m")
            on = m
        out.append(ch)
    return "".join(out) + ("\x1b[0m" if on else "")
