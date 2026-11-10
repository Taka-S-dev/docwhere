"""Printed page numbers ("folios") of a PDF, as opposed to its page index.

A manual's printed number often differs from its PDF page index by the front matter: page
120 of the file says 118 at its foot, and the text refers to pages by the printed number
("127ページ参照"). Real PDF page labels are used when the file has them; otherwise the offset
is read off the pages: numbers standing in the top or bottom margin of pages spread through
the document vote for an offset, and one that a clear majority agrees on is taken.
"""

from __future__ import annotations

import re
from collections import Counter

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
