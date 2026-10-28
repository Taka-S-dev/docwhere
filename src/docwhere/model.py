"""Data shared by the extractors and the index."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

MAX_CHUNK_CHARS = 4000

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
