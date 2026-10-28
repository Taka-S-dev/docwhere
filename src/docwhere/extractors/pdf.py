"""PDF: sectioned by the bookmark (outline) tree when there is one, otherwise by headings
recognised from the layout (larger or bold short lines, 第N章 lines).

With bookmarks, a chunk is a page titled by the bookmark it falls under. Without them, a
chunk is a detected section that may span pages, each line anchored to its own page.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from docwhere.extractors import register
from docwhere.model import Chunk, SectionBuilder, norm

HEADING_SIZE_RATIO = 1.15  # this much larger than body text counts as a heading
MAX_HEADING_CHARS = 60
MAX_HEADING_LEVELS = 3
_CHAPTER = re.compile(r"^第\s*[0-9一二三四五六七八九十百]+\s*([章部編節])")
_NUMBERED = re.compile(r"^(\d+(?:\.\d+){0,4})\.?\s+\S")
_SENTENCE_END = ("。", "、", "，", ",", ":", "：")


@register(".pdf")
def extract(path: Path) -> list[Chunk]:
    import pymupdf

    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            raise ValueError("password protected")
        toc = [(level, norm(title), page) for level, title, page in doc.get_toc(simple=True) if page >= 1 and norm(title)]
        chunks = _by_bookmarks(doc, toc) if toc else _by_layout(doc)
        if not chunks and doc.page_count:
            raise ValueError("no text layer (scanned PDF? OCR it first)")
    return chunks


def _by_bookmarks(doc, toc: list[tuple[int, str, int]]) -> list[Chunk]:
    starts: dict[int, list[tuple[int, str]]] = {}
    for level, title, page in toc:
        starts.setdefault(page, []).append((level, title))

    chunks: list[Chunk] = []
    stack: list[str] = []
    for page in doc:
        no = page.number + 1
        here = starts.get(no, [])
        for level, title in here:
            del stack[level - 1 :]
            stack.extend([""] * (level - 1 - len(stack)))
            stack.append(title)
        lines = [line for line in map(norm, page.get_text().splitlines()) if line]
        if not lines:
            continue
        # Every bookmark that starts on this page is searchable as a heading; a page in the
        # middle of a section inherits the section's title.
        title = " / ".join(t for _, t in here) if here else (stack[-1] if stack else "")
        chunks.append(Chunk(title, " > ".join(t for t in stack if t), lines, [{"page": no}]))
    return chunks


def _by_layout(doc) -> list[Chunk]:
    pages: list[tuple[int, list[tuple[str, float, bool]]]] = []
    chars_by_size: Counter[float] = Counter()
    pages_with_line: Counter[str] = Counter()
    for page in doc:
        lines = list(_styled_lines(page))
        pages.append((page.number + 1, lines))
        for text, size, _ in lines:
            chars_by_size[size] += len(text)
        pages_with_line.update({text for text, _, _ in lines})
    if not chars_by_size:
        return []

    body = chars_by_size.most_common(1)[0][0]
    # running headers/footers repeat on most pages and are not section headings
    repeated = {t for t, n in pages_with_line.items() if n >= 3 and n * 2 >= len(pages)}

    def prominent(text: str, size: float, bold: bool) -> bool:
        if text in repeated or len(text) > MAX_HEADING_CHARS or text.endswith(_SENTENCE_END):
            return False
        return size >= body * HEADING_SIZE_RATIO or (bold and size >= body) or bool(_CHAPTER.match(text))

    heading_sizes = sorted({s for _, ls in pages for t, s, b in ls if prominent(t, s, b)}, reverse=True)

    builder = SectionBuilder()
    for no, lines in pages:
        for text, size, bold in lines:
            if prominent(text, size, bold):
                builder.heading(_level(text, size, heading_sizes), text, {"page": no})
            else:
                builder.add(text, {"page": no})
    return builder.finish()


def _styled_lines(page):
    """(text, font size, bold) for every visual line on the page."""
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            text = norm("".join(s["text"] for s in spans))
            if text:
                yield text, round(max(s["size"] for s in spans), 1), any(s["flags"] & 16 for s in spans)


def _level(text: str, size: float, heading_sizes: list[float]) -> int:
    if m := _CHAPTER.match(text):
        return 2 if m.group(1) == "節" else 1
    if m := _NUMBERED.match(text):
        return min(m.group(1).count(".") + 1, MAX_HEADING_LEVELS)
    return min(heading_sizes.index(size) + 1, MAX_HEADING_LEVELS) if size in heading_sizes else MAX_HEADING_LEVELS
