"""Word: chunks follow the heading structure; tables are read in place, merged cells once.

Word stores no page numbers (they only exist once Word lays the document out), so hits are
located by section and reopened by searching for the hit text in Word itself.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from docwhere.extractors import register
from docwhere.model import Chunk, SectionBuilder

_HEADING_STYLE = re.compile(r"(?:heading|見出し)\s*(\d)", re.IGNORECASE)


@register(".docx", ".docm")
def extract(path: Path) -> list[Chunk]:
    from docx import Document

    builder = SectionBuilder()
    numberer = _HeadingNumberer()
    for p in _paragraphs(Document(str(path))):
        level = _heading_level(p)
        if level and p.text.strip():
            builder.heading(level, numberer.label(p, level) + p.text, {})
        else:
            builder.add(p.text, {})
    return builder.finish()


def _paragraphs(container) -> Iterator:
    """Paragraphs in reading order, descending into (nested) tables."""
    from docx.table import Table

    for block in container.iter_inner_content():
        if not isinstance(block, Table):
            yield block
            continue
        seen = set()  # a horizontally merged cell appears once per grid column it spans
        for row in block.rows:
            for cell in row.cells:
                if cell._tc in seen:
                    continue
                seen.add(cell._tc)
                yield from _paragraphs(cell)


def _ppr_chain(p) -> Iterator:
    """The paragraph's own properties, then its style's, then each base style's."""
    yield p._p.pPr
    style = p.style
    while style is not None:
        yield style.element.pPr
        style = style.base_style


def _heading_level(p) -> int | None:
    from docx.oxml.ns import qn

    for ppr in _ppr_chain(p):
        el = ppr.find(qn("w:outlineLvl")) if ppr is not None else None
        if el is not None:
            level = int(el.get(qn("w:val")))
            return level + 1 if level < 9 else None  # 9 = body text
    m = _HEADING_STYLE.match(p.style.name if p.style is not None else "")
    return int(m.group(1)) if m else None


class _HeadingNumberer:
    """Best-effort "3.2.1 " prefixes for auto-numbered headings.

    The number Word shows is generated at render time and is not in the paragraph text.
    This counts numbered headings per level, which matches the common 1 / 1.1 / 1.1.1
    scheme; custom formats such as 第3章 come out as plain digits.
    """

    def __init__(self) -> None:
        self._counters = [0] * 9

    def label(self, p, level: int) -> str:
        if not _is_numbered(p):
            return ""
        self._counters[level - 1] += 1
        self._counters[level:] = [0] * (9 - level)
        return ".".join(str(c) for c in self._counters[:level]) + " "


def _is_numbered(p) -> bool:
    from docx.oxml.ns import qn

    for ppr in _ppr_chain(p):
        num = ppr.find(qn("w:numPr")) if ppr is not None else None
        if num is not None:
            num_id = num.find(qn("w:numId"))
            return num_id is None or num_id.get(qn("w:val")) != "0"
    return False
