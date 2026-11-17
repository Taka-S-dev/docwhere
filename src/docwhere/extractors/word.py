"""Word: chunks follow the heading structure; tables are read in place, one line per row.

Word stores no page numbers (they only exist once Word lays the document out), so hits are
located by section and reopened by searching for the hit text in Word itself.

Text is read wherever the file keeps it: in a tracked insertion (a document under review), in
a content control (a form or template), and in a text box, which follows its paragraph as a
line marked 〔テキストボックス〕. Text struck out by a tracked deletion is not read.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from docwhere.extractors import register
from docwhere.model import MAX_HEADER_CHARS, Chunk, SectionBuilder, table_lines

_HEADING_STYLE = re.compile(r"(?:heading|見出し)\s*(\d)", re.IGNORECASE)
MAX_CELL_CHARS = 2000  # text kept from one table cell
MAX_TABLE_DEPTH = 3  # tables nested deeper than this are not read
MAX_DOCUMENT_CHARS = 50_000_000  # past this the file is treated as unreadable, like a zip bomb
TEXT_BOX = "〔テキストボックス〕"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
# not the paragraph's own text: a text box anchored in it (read as a line of its own), the older
# drawing kept beside a newer one for programs that cannot show it, and text moved away
_NOT_OWN = {f"{_W}txbxContent", _FALLBACK, f"{_W}moveFrom"}


@register(".docx", ".docm")
def extract(path: Path) -> list[Chunk]:
    from docx import Document

    builder = SectionBuilder()
    numberer = _HeadingNumberer()
    budget = _Budget(MAX_DOCUMENT_CHARS)
    for block in _blocks(Document(str(path)), budget):
        if isinstance(block, str):  # a table row or a text box, already counted against the budget
            builder.add(block, {})
            continue
        text = _text(block)
        budget.spend(len(text))
        level = _heading_level(block)
        if level and text.strip():
            builder.heading(level, numberer.label(block, level) + text, {})
        else:
            builder.add(text, {})
    return builder.finish()


def _text(p) -> str:
    """The paragraph's text wherever it is kept: in plain runs, in a tracked insertion, in a
    content control or a hyperlink. python-docx's ``text`` reads the plain runs and hyperlinks
    only, so a sentence added under review, or a form's filled-in field, was not found."""
    own = p._p
    parts = []
    for el in own.iter(f"{_W}t", f"{_W}tab", f"{_W}br", f"{_W}cr"):
        if any(a.tag in _NOT_OWN for a in _between(el, own)):
            continue
        parts.append(el.text or "" if el.tag == f"{_W}t" else "\t" if el.tag == f"{_W}tab" else "\n")
    return "".join(parts)


def _between(el, top) -> Iterator:
    for ancestor in el.iterancestors():
        if ancestor is top:
            return
        yield ancestor


def _text_boxes(p) -> list[str]:
    """The text of each text box anchored in the paragraph, once (a box is stored twice, as a
    drawing and as its older form)."""
    texts = []
    for box in p._p.iter(f"{_W}txbxContent"):
        if any(a.tag == _FALLBACK for a in _between(box, p._p)):
            continue
        text = " ".join("".join(t.text or "" for t in para.iter(f"{_W}t")) for para in box.iter(f"{_W}p")).strip()
        if text and text not in texts:
            texts.append(text)
    return texts


def _children(container) -> Iterator:
    """Paragraphs and tables of the document or cell in order, those inside a content control
    (a block-level w:sdt) included: python-docx passes those over."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = getattr(getattr(container, "element", None), "body", None)
    top = body if body is not None else container._element

    def walk(parent) -> Iterator:
        for child in parent:
            if child.tag == f"{_W}p":
                yield Paragraph(child, container)
            elif child.tag == f"{_W}tbl":
                yield Table(child, container)
            elif child.tag == f"{_W}sdt":
                content = child.find(f"{_W}sdtContent")
                if content is not None:
                    yield from walk(content)

    yield from walk(top)


class _Budget:
    """Characters a document may still produce. Merged cells repeat their text on every row
    they span, so the output can outgrow the file; rows are charged as they are built, nested
    tables included, and extraction stops (the file counts as unreadable) once it runs out."""

    def __init__(self, chars: int) -> None:
        self.left = chars

    def spend(self, chars: int) -> None:
        self.left -= chars
        if self.left < 0:
            raise ValueError(f"document text exceeds the {MAX_DOCUMENT_CHARS:,}-character limit")


def _blocks(container, budget: _Budget, depth: int = 0) -> Iterator:
    """Paragraphs in reading order, with each table read in place as one line per row."""
    from docx.table import Table

    for block in _children(container):
        if not isinstance(block, Table):
            yield block
            for text in _text_boxes(block):
                budget.spend(len(text))
                yield f"{TEXT_BOX} {text}"
        elif depth < MAX_TABLE_DEPTH:
            # a vertically merged cell is the same element on every row it spans: read it once
            texts: dict = {}
            rows = []
            for row in block.rows:
                cells = [_cell_text(c, depth, texts, budget) for c in _row_cells(row)]
                budget.spend(sum(map(len, cells)) + MAX_HEADER_CHARS * len(cells))  # with the labels table_lines adds
                rows.append(cells)
            yield from table_lines(rows)


def _row_cells(row) -> Iterator:
    previous = None  # a horizontally merged cell appears once per grid column it spans
    for cell in row.cells:
        if cell._tc is not previous:
            yield cell
        previous = cell._tc


def _cell_text(cell, depth: int, texts: dict, budget: _Budget) -> str:
    """The cell's paragraphs, and those of any table nested in it, as one string.

    A merged cell's text is repeated on each row it spans, so a crafted file could have a
    small cell expanded many times over; texts are cached per table element and capped.
    """
    if cell._tc in texts:
        return texts[cell._tc]
    parts, size = [], 0
    for block in _blocks(cell, budget, depth + 1):
        text = block if isinstance(block, str) else _text(block)
        parts.append(text)
        size += len(text)
        if size >= MAX_CELL_CHARS:
            break
    texts[cell._tc] = " ".join(parts)[:MAX_CELL_CHARS]
    return texts[cell._tc]


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
