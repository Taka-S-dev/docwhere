"""Printed page numbers ("folios") of a PDF, as opposed to its page index.

A manual's printed number often differs from its PDF page index by the front matter: page
120 of the file says 118 at its foot, and the text refers to pages by the printed number
("127ページ参照"). Real PDF page labels are used when the file has them; otherwise the offset
is read off the pages: numbers standing in the top or bottom margin of pages spread through
the document vote for an offset, and one that a clear majority agrees on is taken.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

SAMPLES = 24
MARGIN = 0.1  # share of the page height counted as header or footer
MIN_AGREEMENT = 0.5
_NUMBER = re.compile(r"(?<![\d.])\d{1,5}(?![\d.])")


def printed_offset(doc) -> int | None:
    """``index - printed`` for the document, or None when the pages carry no recognisable
    folio or it equals the index (nothing worth showing)."""
    n = doc.page_count
    if n < 4:
        return None
    # Labels that only restate the index (1, 2, 3 ...) are common and say nothing; only labels
    # that differ from it are trusted outright.
    if labelled := _offset_from_labels(doc):
        return labelled
    picks = sorted({max(0, min(n - 1, round(n * (k + 0.5) / SAMPLES))) for k in range(SAMPLES)})
    votes: Counter[int] = Counter()
    for i in picks:
        page = doc[i]
        height = page.rect.height
        seen: set[int] = set()
        for block in page.get_text("blocks"):
            top, bottom, text = block[1], block[3], block[4]
            if top > height * (1 - MARGIN) or bottom < height * MARGIN:
                for token in _NUMBER.findall(text):
                    offset = (i + 1) - int(token)
                    if 0 <= offset <= max(50, n // 5):
                        seen.add(offset)
        votes.update(seen)  # one vote per page and offset
    if not votes:
        return None
    offset, count = votes.most_common(1)[0]
    if count < len(picks) * MIN_AGREEMENT:
        return None
    return offset or None


def _offset_from_labels(doc) -> int | None:
    """The offset implied by the PDF's own page labels, when they are plain numbers."""
    try:
        if not doc.get_page_labels():
            return None
    except Exception:
        return None
    offsets = set()
    for i in (0, doc.page_count // 2, doc.page_count - 1):
        label = doc[i].get_label()
        if not label.isdigit():
            return None
        offsets.add((i + 1) - int(label))
    return offsets.pop() if len(offsets) == 1 else None


def printed(offset: int | None, page: int) -> int | None:
    """The printed number of PDF page ``page``, or None when it is the same or unknown."""
    if not offset or page - offset < 1:
        return None
    return page - offset


_OFFSETS: dict[tuple[str, int, int], int | None] = {}  # per file as it is now: path, mtime, size


def offset_of(pdf: Path | str, cache: Path | None = None) -> int | None:
    """``printed_offset`` of the PDF at ``pdf``, or None when it cannot be opened.

    Reading it off a manual of thousands of pages takes about half a second, as long as the
    search itself, so the answer is kept: in this process, and in ``cache`` (a JSON file) when
    one is given, for the next command. The file's time and size are part of what is kept, so
    a replaced PDF is read again. The cache is a convenience: one that cannot be read or
    written is passed over.
    """
    try:
        stat = os.stat(pdf)
    except OSError:
        return None
    name = os.path.normcase(os.path.abspath(pdf))
    key = (name, stat.st_mtime_ns, stat.st_size)
    if key in _OFFSETS:
        return _OFFSETS[key]
    saved = _saved(cache)
    if (entry := saved.get(name)) and entry[:2] == [stat.st_mtime_ns, stat.st_size]:
        _OFFSETS[key] = entry[2]
        return entry[2]
    try:
        import pymupdf

        with pymupdf.open(pdf) as doc:
            offset = printed_offset(doc)
    except Exception:
        return None  # not kept: the file may only be open elsewhere for now
    _OFFSETS[key] = offset
    if cache is not None:
        # read again just before writing: another search may have added a file meanwhile
        saved = _saved(cache) | {name: [stat.st_mtime_ns, stat.st_size, offset]}
        try:
            temporary = cache.with_name(f"{cache.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, cache)  # whole file or nothing, for a search reading it now
        except OSError:
            pass
    return offset


def _saved(cache: Path | None) -> dict:
    if cache is None:
        return {}
    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def page_label(page: int, number: int | None) -> str:
    """"p.120（印刷 118）" when the page prints another number than its index, else "p.120"."""
    return f"p.{page}（印刷 {number}）" if number else f"p.{page}"
