"""Data shared by the extractors and the index."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

MAX_CHUNK_CHARS = 4000
MAX_HEADER_CHARS = 30  # longer first-row cells are content, not column labels

# Document text is printed to the terminal, so escape sequences and bidi overrides embedded in
# a file must not survive extraction. C0/C1 controls become spaces (some are Word's line and
# page breaks); invisible format characters are dropped so they do not split words.
_CONTROLS = re.compile(r"[\x00-\x08\x0e-\x1b\x7f-\x9f]")
_INVISIBLE = re.compile(r"[­​-‏‪-‮⁠-⁩﻿]")


def norm(text: str) -> str:
    """NFKC-fold (full-width ASCII to half-width, half-width katakana to full-width), strip
    control characters and collapse every run of whitespace to one space.

    Applied to both indexed text and query terms, so width and line-break differences
    never decide whether something matches.
    """
    text = _INVISIBLE.sub("", _CONTROLS.sub(" ", unicodedata.normalize("NFKC", text)))
    return " ".join(text.split())


def table_lines(rows: list[list[str]]) -> list[str]:
    """One line per table row, labelled by the header row: "項目: 接続タイムアウト / 既定値: 30秒".

    A setting's value sits in the cell next to its name, so a hit on the name must carry the
    rest of the row for the snippet to answer "what is the default". The first row counts as
    the header when its cells are distinct short labels; otherwise, or when a row's cells do
    not line up with it (merged cells), the row is joined without labels. Tables with a
    single row or column are boxes around text, not tables of values, and stay one line per cell.
    A labelled header row is not a line of its own: every row already names its columns, and a
    bare "項目 / 既定値" line would be the snippet for any search on a column name.
    """
    rows = [cells for cells in ([norm(c) for c in r] for r in rows) if any(cells)]
    if len(rows) < 2 or max(map(len, rows)) < 2:
        return [c for cells in rows for c in cells if c]
    header = rows[0]
    labelled = all(header) and len(set(header)) == len(header) and max(map(len, header)) <= MAX_HEADER_CHARS
    lines = [] if labelled else [" / ".join(c for c in header if c)]
    for cells in rows[1:]:
        if labelled and len(cells) == len(header):
            lines.append(" / ".join(f"{h}: {c}" for h, c in zip(header, cells) if c))
        else:
            lines.append(" / ".join(c for c in cells if c))
    return lines


@dataclass
class Chunk:
    """One searchable unit: a document section, a PDF page, a slide or a block of cells.

    ``lines`` and ``anchors`` run in parallel so a hit on a line maps back to a place in
    the file. When every line shares one place (a PDF page, a slide) ``anchors`` holds a
    single entry.
    """

    title: str
    crumb: str
    lines: list[str] = field(default_factory=list)
    anchors: list[dict] = field(default_factory=list)


class SectionBuilder:
    """Collect lines under a heading hierarchy and cut them into chunks.

    A new chunk starts at every heading, and a long section is split at MAX_CHUNK_CHARS;
    the continuation keeps the section's title so heading matches still rank it.
    """

    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._stack: list[str] = []
        self._lines: list[str] = []
        self._anchors: list[dict] = []
        self._size = 0

    def heading(self, level: int, title: str, anchor: dict) -> None:
        title = norm(title)
        if not title:
            return
        self._flush()
        level = max(1, level)
        del self._stack[level - 1 :]
        self._stack.extend([""] * (level - 1 - len(self._stack)))
        self._stack.append(title)
        self.add(title, anchor)

    def add(self, text: str, anchor: dict) -> None:
        text = norm(text)
        if not text:
            return
        if self._lines and self._size + len(text) > MAX_CHUNK_CHARS:
            self._flush()
        self._lines.append(text)
        self._anchors.append(anchor)
        self._size += len(text) + 1

    def finish(self) -> list[Chunk]:
        self._flush()
        return self._chunks

    def _flush(self) -> None:
        if self._lines:
            title = self._stack[-1] if self._stack else ""
            crumb = " > ".join(t for t in self._stack if t)
            self._chunks.append(Chunk(title, crumb, self._lines, self._anchors))
        self._lines, self._anchors, self._size = [], [], 0
