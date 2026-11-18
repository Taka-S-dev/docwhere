"""PDF: sectioned by the bookmark (outline) tree when there is one, otherwise by headings
recognised from the layout (larger or bold short lines, 第N章 lines).

With bookmarks, a chunk is a page titled by the bookmark it falls under, without the running
header and footer lines that repeat at the page's top and bottom edge. Without them, a
chunk is a detected section that may span pages, each line anchored to its own page.

A page's lines are put back the way they read (see _page_lines): the cells of a table row
become one line, a sentence wrapped over several lines becomes one line, and a mark drawn with
a symbol font's private character (the ○ and ― of a support table) becomes the mark it shows.

What is written inside a picture is not read. The pictures large enough to hold a table or a
screen are counted page by page (see _pictures), so that a search can say they are there.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from docwhere.extractors import counting_pictures, note, pictures, register
from docwhere.model import Chunk, SectionBuilder, norm

HEADING_SIZE_RATIO = 1.15  # this much larger than body text counts as a heading
HEADING_ROW_RATIO = 1.3  # a row of cells set this much larger than body text, all of it, is a heading line
MAX_HEADING_CHARS = 60
MAX_HEADING_LEVELS = 3
MAX_PAGES_NAMED = 10  # pages without text named in the file's note; the count is always given
EDGE_BAND = 0.1  # this share of the page height at the top and bottom holds running headers/footers
EDGE_BLOCKS = 2  # outermost text blocks at the top and at the bottom looked at for one
RUNNING_PAGES = 3  # an edge line on this many pages is a running header or footer...
RUNNING_AT_EDGE = 0.9  # ...when this share of the pages it is on have it at the edge
ROW_SEP = " | "  # between the cells of a table row put into one line
RUNNING_NEAR = 2  # ...and at least half of those pages have another of them this close: a chapter's
#                    header runs on from page to page, a sub-heading that opens a page now and then does not
SIDE_BAND = 0.08  # this share of the page width at the left and right holds the chapter tab
SIDE_TAB_WIDTH = 24.0  # a block narrower than this (points) in the side band is such a tab, not text
SAME_LEFT = 3.0  # lines starting within this many points of each other start at the same place
HANGING_INDENT = 3.0  # a wrapped step goes on this many letters in, under the text after its number
WRAP_SLACK = 2.0  # a line ending within this many letters of the right edge was wrapped there
COLUMN_MIN_LINES = 6  # two blocks of this many lines each, side by side, are columns of text
#                       (fewer are the cells of a table row, each a few lines long)
COLUMN_MIN_WIDTH, COLUMN_MAX_WIDTH = 0.25, 0.49  # ...when each is this share of the page wide
# The standard symbol fonts put their signs at private code points (0xF000 + the font's own code).
_SYMBOL = {0xAE: "→", 0xAC: "←", 0xAD: "↑", 0xAF: "↓", 0xB4: "×", 0xB7: "•", 0xB1: "±", 0xB3: "≥", 0xA3: "≤",
           0xB9: "≠", 0xB0: "°", 0xD7: "⋅", 0xA5: "∞", 0x6D: "μ", 0x57: "Ω", 0x44: "Δ", 0xDE: "⇒"}
_WINGDINGS = {0xFC: "✓", 0xFB: "✗", 0xFE: "☑", 0xFD: "☒", 0x6C: "●", 0x6E: "■", 0x6F: "□", 0xA1: "○", 0xA8: "□",
              0xA7: "▪", 0x9F: "•", 0xE0: "→", 0xDF: "←", 0xE1: "↑", 0xE2: "↓", 0xE8: "→", 0xF0: "⇒", 0x46: "☞",
              0xD8: "➢", 0x75: "◆", 0x76: "❖", 0xA4: "◉"}
PICTURE_MIN_WIDTH, PICTURE_MIN_HEIGHT = 120.0, 40.0  # points; smaller is an icon, a button or a rule
PICTURE_TEXT_CHARS = 50  # with this much text over it, the picture's words are in the text already
PICTURE_REPEAT_PAGES, PICTURE_REPEAT_SHARE = 10, 0.2  # the same picture at the same place on this
#                                                       many pages, and this share of them: a letterhead
GLYPH_PIXELS = 48  # a private-use glyph is drawn about this large to see what mark it is
_SENTENCE_STOP = tuple("。．.!?！？:：」）)】』")
_ITEM_START = re.compile(r"[•・●○■□◆◇※＊*\-–—〔]|[(（]?[0-9０-９]{1,2}[)）.．]|[①-⑳]")
_SECTION_NO = re.compile(r"\d+(?:\.\d+){0,4}\.?|第\s*\d+\s*[章部編節]")
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
        textless: list[int] = []
        glyphs = _Glyphs()
        chunks = _by_bookmarks(doc, toc, textless, glyphs) if toc else _by_layout(doc, textless, glyphs)
        if not chunks and doc.page_count:
            raise ValueError("no text layer (scanned PDF? OCR it first)")
        # pages scanned into a PDF that has text elsewhere: nothing on them can be found, and
        # nothing else says so (a blank page has no picture either, and is not counted)
        scanned = [no for no in textless if doc[no - 1].get_images()]
        if counting_pictures():
            pictures(_pictures(doc))
        if scanned:
            shown = ", ".join(f"p.{no}" for no in scanned[:MAX_PAGES_NAMED])
            more = f" and {len(scanned) - MAX_PAGES_NAMED} more" if len(scanned) > MAX_PAGES_NAMED else ""
            note(f"{len(scanned)} of {doc.page_count} pages are pictures without text and cannot be searched"
                 f" ({shown}{more}); OCR the file to search them")
    return chunks


def _pictures(doc) -> dict[int, tuple[int, float]]:
    """page -> (pictures, share of the page they cover), for the pictures that may hold words no
    search reaches: large enough for a table or a screen, not the same picture at the same place
    page after page (a letterhead, a chapter banner), and without text of the page's own over it
    (a scan that was given a text layer is searched through that layer)."""
    import pymupdf

    placed: dict[int, list[tuple]] = {}
    pages_with: Counter[tuple] = Counter()
    for page in doc:
        # Asked first, since placing the pictures reads the page a second time and most pages have
        # none. A picture written into the page's own drawing (an inline image, meant for a small
        # one) is not in this list and is not counted.
        if not page.get_images(full=True):
            continue
        here = []
        for info in page.get_image_info():
            rect = pymupdf.Rect(info["bbox"]) & page.rect
            if rect.width < PICTURE_MIN_WIDTH or rect.height < PICTURE_MIN_HEIGHT:
                continue
            here.append((rect, (round(rect.x0), round(rect.y0), round(rect.x1), round(rect.y1), info["width"], info["height"])))
        if here:
            placed[page.number + 1] = here
            pages_with.update({same for _, same in here})
    often = max(PICTURE_REPEAT_PAGES, doc.page_count * PICTURE_REPEAT_SHARE)
    found = {}
    for no, here in placed.items():
        page = doc[no - 1]
        kept = [rect for rect, same in here if pages_with[same] < often
                and len("".join(page.get_text("text", clip=rect, flags=_text_only()).split())) < PICTURE_TEXT_CHARS]
        if kept:
            area = sum(rect.width * rect.height for rect in kept) / max(page.rect.width * page.rect.height, 1.0)
            found[no] = (len(kept), round(min(area, 1.0), 2))
    return found


def _by_bookmarks(doc, toc: list[tuple[int, str, int]], textless: list[int], glyphs: "_Glyphs") -> list[Chunk]:
    from docwhere.folio import printed_offset

    starts: dict[int, list[tuple[int, str]]] = {}
    for level, title, page in toc:
        starts.setdefault(page, []).append((level, title))

    read = [_read_page(page, glyphs) for page in doc]
    sizes: Counter[float] = Counter()
    for blocks in read:
        for block in blocks:
            for line in block.lines:
                sizes[line.size] += len(line.text)
    body = sizes.most_common(1)[0][0] if sizes else 0.0
    offset = printed_offset(doc) or 0
    pages = [(no, _page_lines(doc[no - 1].rect, blocks, body, str(no - offset) if no - offset >= 1 else ""))
             for no, blocks in enumerate(read, 1)]
    at_edge: dict[str, list[int]] = {}
    anywhere: Counter[str] = Counter()
    for no, tagged in pages:
        for line in {line for line, edge in tagged if edge}:
            at_edge.setdefault(line, []).append(no)
        anywhere.update({line for line, _ in tagged})
    # A chapter's running header changes from chapter to chapter, so it is counted by pages, not
    # as a share of the document. It is at the edge wherever it is; a word or a list's "1)" that
    # only now and then ends a page is body text. It also runs on from one page to the next: a
    # sub-heading that happens to open three pages far apart is a heading, and is kept.
    running = set()
    for line, on in at_edge.items():
        if len(on) < RUNNING_PAGES or len(on) < anywhere[line] * RUNNING_AT_EDGE:
            continue
        near = sum(1 for a, b in zip(on, on[1:]) if b - a <= RUNNING_NEAR)
        if (near + 1) * 2 >= len(on):
            running.add(line)

    chunks: list[Chunk] = []
    stack: list[str] = []
    for no, tagged in pages:
        here = starts.get(no, [])
        for level, title in here:
            del stack[level - 1 :]
            stack.extend([""] * (level - 1 - len(stack)))
            stack.append(title)
        if not tagged:
            textless.append(no)
            continue
        # Every bookmark that starts on this page is searchable as a heading; a page in the
        # middle of a section inherits the section's title.
        title = " / ".join(t for _, t in here) if here else (stack[-1] if stack else "")
        # a running header or footer is on every page of a chapter: as the body it would be every
        # page's snippet and score; a page that holds nothing else keeps it
        lines = [line for line, edge in tagged if not (edge and line in running)] or [line for line, _ in tagged]
        chunks.append(Chunk(title, " > ".join(t for t in stack if t), lines, [{"page": no}]))
    return chunks


@dataclass
class _Line:
    x0: float
    y0: float
    x1: float
    y1: float
    size: float
    text: str


@dataclass
class _Block:
    x0: float
    y0: float
    x1: float
    y1: float
    lines: list[_Line]


class _Glyphs:
    """What a private-use character of a PDF's own symbol font shows.

    Manuals draw the ○ and ― of their tables with a font of their own: the text then holds a
    character nothing can display, and a table of what is supported reads as a table of blanks.
    The glyph is drawn once and looked at: a ring is ○, a cross ×, a triangle △, a flat bar ― (see
    _mark_drawn). Anything else becomes
    a visible token (〔f0b2〕), so that the page's own legend ("〔f0b2〕: 対応") still says what
    it stands for.
    """

    def __init__(self) -> None:
        self._known: dict[tuple[str, str], str] = {}

    def text(self, page, span: dict) -> str:
        raw = span["text"]
        if not any("\ue000" <= c <= "\uf8ff" for c in raw):
            return raw
        out = []
        for c in raw:
            if "\ue000" <= c <= "\uf8ff":
                key = (span["font"], c)
                if key not in self._known:
                    self._known[key] = self._look(page, span, c)
                c = self._known[key]
            out.append(c)
        return "".join(out)

    def _look(self, page, span: dict, char: str) -> str:
        token = f"〔{ord(char):04x}〕"
        font = span["font"].lower()
        standard = (_SYMBOL if font.endswith("symbol") or "symbolmt" in font
                    else _WINGDINGS if font.endswith("wingdings") or "wingdings-regular" in font else None)
        if standard is not None and ord(char) >= 0xF000:
            return standard.get(ord(char) - 0xF000, token)
        try:
            import pymupdf

            box = None
            for block in page.get_text("rawdict", clip=pymupdf.Rect(span["bbox"]) + (-1, -1, 1, 1))["blocks"]:
                for line in block.get("lines", []):
                    for sp in line["spans"]:
                        for ch in sp["chars"]:
                            if ch["c"] == char and sp["font"] == span["font"]:
                                box = pymupdf.Rect(ch["bbox"])
                                break
            if box is None or box.is_empty:
                return token
            zoom = GLYPH_PIXELS / max(box.width, box.height)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=box, colorspace=pymupdf.csGRAY, alpha=False)
            return _mark_drawn(pix.samples, pix.width, pix.height, pix.stride) or token
        except Exception:
            return token  # a page that cannot be drawn: the token still tells the glyphs apart


def _mark_drawn(samples: bytes, width: int, height: int, stride: int) -> str:
    """The mark a drawn glyph shows: ○ (ring), ● (disc), ■ (filled square), × (cross),
    △ (triangle) or ― (flat bar); "" for any other shape, which is then not guessed at."""
    ink = [[samples[y * stride + x] < 128 for x in range(width)] for y in range(height)]
    xs = [x for row in ink for x, on in enumerate(row) if on]
    ys = [y for y, row in enumerate(ink) if any(row)]
    if not xs:
        return ""
    x0, x1, y0, y1 = min(xs), max(xs), ys[0], ys[-1]
    w, h = x1 - x0 + 1, y1 - y0 + 1
    filled = sum(on for row in ink[y0:y1 + 1] for on in row[x0:x1 + 1]) / (w * h)
    if h * 4 <= w and filled >= 0.6:
        return "―"
    if not 0.8 <= w / h <= 1.25 or w < 8:
        return ""

    def inked(fx0: float, fy0: float, fx1: float, fy1: float) -> bool:
        """Whether there is ink in this part of the glyph's own box (fractions of it)."""
        return any(ink[y][x] for y in range(y0 + int(h * fy0), min(y1 + 1, y0 + max(int(h * fy0) + 1, round(h * fy1))))
                   for x in range(x0 + int(w * fx0), min(x1 + 1, x0 + max(int(w * fx0) + 1, round(w * fx1)))))

    centre = inked(0.4, 0.4, 0.6, 0.6)
    top_corners = inked(0, 0, 0.12, 0.12), inked(0.88, 0, 1, 0.12)
    bottom_corners = inked(0, 0.88, 0.12, 1), inked(0.88, 0.88, 1, 1)
    sides = inked(0.4, 0, 0.6, 0.15), inked(0.4, 0.85, 0.6, 1), inked(0, 0.4, 0.15, 0.6), inked(0.85, 0.4, 1, 0.6)
    corners = (*top_corners, *bottom_corners)
    if filled >= 0.97:
        return "■"
    if centre and all(sides) and not any(corners) and filled >= 0.7:
        return "●"
    if not centre and all(sides) and not any(corners) and 0.08 <= filled <= 0.6:
        return "○"
    if centre and all(corners) and not any(sides) and filled <= 0.45:
        return "×"
    if not centre and all(bottom_corners) and not any(top_corners) and sides[0] and sides[1] and filled <= 0.45:
        return "△"
    return ""


def _text_only() -> int:
    """Flags for reading a page's text with its places but without its pictures, whose data
    makes up most of the time a page takes otherwise."""
    import pymupdf

    return pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES


def _read_page(page, glyphs: _Glyphs) -> list[_Block]:
    """The page's text blocks with the place and size of each line, in the order the file holds them."""
    blocks = []
    for b in page.get_text("dict", flags=_text_only())["blocks"]:
        if b["type"] != 0:
            continue
        lines = []
        for line in b["lines"]:
            spans = [sp for sp in line["spans"] if sp["text"].strip()]
            text = norm("".join(glyphs.text(page, sp) for sp in spans))
            if text:
                lines.append(_Line(*line["bbox"], round(max(sp["size"] for sp in spans), 1), text))
        if lines:
            blocks.append(_Block(*b["bbox"], lines))
    return blocks


def _page_lines(rect, blocks: list[_Block], body: float, folio: str) -> list[tuple[str, bool]]:
    """(line, at the edge) for each line of the page as it reads.

    At the edge means in one of the EDGE_BLOCKS outermost text blocks at the top or bottom, within
    EDGE_BAND of the page height: where a running header or footer sits, and a table's cells only
    seldom. Such a line loses the page's printed number (``folio``), which is different on every
    page and would keep the header from being seen as one. The chapter tab in the side margin
    ("付 / 録 / A", a letter a line) is left out.

    The rest is put back as it reads. Text on one level is a row: its cells make one line, joined
    by ROW_SEP, and a cell's further lines go into the cell. A line that fills
    its width and does not end its sentence goes on in the next line, so the two make one: a
    phrase is then found whole, wherever the page wrapped it. Pages set in columns of text keep
    the order and the lines of the file, since joining across columns would mix them.
    """
    side = rect.width * SIDE_BAND
    blocks = [b for b in blocks
              if not (b.x1 - b.x0 < SIDE_TAB_WIDTH and (b.x1 <= rect.x0 + side or b.x0 >= rect.x1 - side))]
    top, bottom = rect.y0 + rect.height * EDGE_BAND, rect.y1 - rect.height * EDGE_BAND
    by_y = sorted(range(len(blocks)), key=lambda i: blocks[i].y0)
    edge = {i for i in by_y[:EDGE_BLOCKS] if blocks[i].y1 <= top} | {i for i in by_y[-EDGE_BLOCKS:] if blocks[i].y0 >= bottom}
    out: list[tuple[str, bool]] = []
    if _in_columns(rect, [b for i, b in enumerate(blocks) if i not in edge]):
        for i, b in enumerate(blocks):
            out += [(text, i in edge) for text in (_edge_text(l.text, folio) if i in edge else l.text for l in b.lines) if text]
        return out

    ordered = sorted(range(len(blocks)), key=lambda i: (blocks[i].y0, blocks[i].x0))
    inner = [blocks[i] for i in ordered if i not in edge]
    right = max((l.x1 for b in inner for l in b.lines), default=rect.x1)
    paragraphs: list[tuple[_Line, _Block]] = []  # lines that are no row of cells, to be joined where wrapped
    tail: list[tuple[_Line, _Block]] = []  # the last line of the row before them: a step's text may go on below

    def flush() -> None:
        texts = _joined(tail + paragraphs, right)
        if tail and out:
            # the first text is the row's last line, with whatever it went on into
            out[-1] = (out[-1][0] + texts[0][len(tail[0][0].text):], False)
            texts = texts[1:]
        out.extend((text, False) for text in texts)
        paragraphs.clear()
        tail.clear()

    def at_edge(block: _Block) -> list[tuple[str, bool]]:
        # what stands on one level reads as one line: a chapter's number and its name, a
        # footer's page number and its title
        levels = (" ".join(l.text for cell in cells for l in cell) for cells in _rows([block]))
        return [(text, True) for text in (_edge_text(level, folio) for level in levels) if text]

    for i in (j for j in ordered if j in edge and blocks[j].y1 <= top):
        out += at_edge(blocks[i])
    for band in _bands(inner):
        for cells in _rows(band):
            if len(cells) == 1:
                paragraphs.extend((line, band[0]) for line in cells[0])
                continue
            flush()
            # a section's number and its name, or a line set large throughout: not a table row
            heading = ((len(cells) == 2 and bool(_SECTION_NO.fullmatch(cells[0][0].text)))
                       or min(l.size for cell in cells for l in cell) >= body * HEADING_ROW_RATIO > 0)
            texts = ["".join(_flowing(a.text, b.text if b else None) for a, b in zip(cell, [*cell[1:], None])) for cell in cells]
            # a heading's number and its title stand apart like cells, and read as one line
            out.append(((" " if heading else ROW_SEP).join(texts), False))
            tail[:] = [(cells[-1][-1], band[0])]
    flush()
    for i in (j for j in ordered if j in edge and blocks[j].y0 >= bottom):
        out += at_edge(blocks[i])
    return out


def _edge_text(text: str, folio: str) -> str:
    """A header or footer line without the page's printed number, alone or run into its end."""
    if not folio:
        return text
    if text == folio:
        return ""
    if text.endswith(folio) and not text[-len(folio) - 1].isdigit() and text[-len(folio) - 1] != ".":
        return text[: -len(folio)].rstrip()
    if text.startswith(folio + " "):
        return text[len(folio) + 1 :]
    return text


def _in_columns(rect, blocks: list[_Block]) -> bool:
    """Whether the page is set in columns of text: two tall blocks of text side by side."""
    tall = [b for b in blocks if len(b.lines) >= COLUMN_MIN_LINES
            and rect.width * COLUMN_MIN_WIDTH <= b.x1 - b.x0 <= rect.width * COLUMN_MAX_WIDTH]
    return any(a.x1 <= b.x0 and min(a.y1, b.y1) - max(a.y0, b.y0) > 0.5 * min(a.y1 - a.y0, b.y1 - b.y0)
               for a in tall for b in tall)


def _bands(blocks: list[_Block]) -> list[list[_Block]]:
    """Blocks grouped by level: blocks whose first lines stand side by side on one level are
    the cells of one row, however the file split them."""
    bands: list[list[_Block]] = []
    for b in blocks:
        first = b.lines[0]
        middle = (first.y0 + first.y1) / 2
        if bands:
            lead = bands[-1][0].lines[0]
            beside = all(b.x0 >= o.x1 - 1 or b.x1 <= o.x0 + 1 for o in bands[-1])
            if beside and lead.y0 <= middle <= lead.y1 and abs(first.size - lead.size) <= 2:
                bands[-1].append(b)
                continue
        bands.append([b])
    return bands


def _rows(band: list[_Block]) -> list[list[list[_Line]]]:
    """The band as rows of cells, each cell the lines it holds from top to bottom.

    The lines on the first level are the cells. A line further down belongs to the cell it
    starts under. A level that has every cell again starts a new row, unless a cell above it
    fills its column: its text then goes on below, and the level is the rest of the same row."""
    lines = sorted((l for b in band for l in b.lines), key=lambda l: ((l.y0 + l.y1) / 2, l.x0))
    levels: list[list[_Line]] = []
    for line in lines:
        middle = (line.y0 + line.y1) / 2
        if levels and levels[-1][0].y0 <= middle <= levels[-1][0].y1:
            levels[-1].append(line)
        else:
            levels.append([line])
    rows: list[list[list[_Line]]] = []
    edge = max(l.x1 for l in lines)
    for level in levels:
        level.sort(key=lambda l: l.x0)
        if not rows:
            rows.append([[line] for line in level])
            continue
        cells = rows[-1]
        if len(level) == len(cells) and len(level) > 1:
            ends = [c[0].x0 for c in cells[1:]] + [edge]
            if not any(c[-1].x1 >= end - WRAP_SLACK * c[-1].size for c, end in zip(cells, ends)):
                rows.append([[line] for line in level])
                continue
        if len(cells) == 1:
            if len(level) == 1:
                rows.append([[level[0]]])  # a line of its own, joined later if it was wrapped
            else:
                rows.append([[line] for line in level])
            continue
        for line in level:
            under = [c for c in cells if c[0].x0 <= line.x0 + SAME_LEFT]
            (under[-1] if under else cells[0]).append(line)
    return rows


def _flowing(text: str, after: str | None) -> str:
    """``text`` and what joins it to the line after it in the same cell: nothing where a
    sentence was wrapped between two letters of Japanese, a space otherwise."""
    if after is None:
        return text
    wrapped = not text.endswith(_SENTENCE_STOP) and not _ITEM_START.match(after)
    return text if wrapped and ord(text[-1]) > 0x2E7F and ord(after[0]) > 0x2E7F else text + " "


def _joined(lines: list[tuple[_Line, _Block]], right: float) -> list[str]:
    """Lines of running text, each wrapped sentence put back into one line.

    A line goes on in the next when it reaches the right edge (of the page's text, or of its
    own block), does not end a sentence, and the next line starts under it, close below, in the
    same size, and is no item of a list."""
    out: list[str] = []
    previous: tuple[_Line, _Block] | None = None
    for line, block in lines:
        if previous is not None:
            a, a_block = previous
            slack = WRAP_SLACK * a.size
            full = a.x1 >= right - slack or (a_block is block and a.x1 >= block.x1 - slack and len(block.lines) > 1)
            # under it, or under its text when the line opens with a step's number ("1. ...")
            below = 0 <= line.y0 - a.y0 <= 2.2 * a.size and -SAME_LEFT <= line.x0 - a.x0 <= HANGING_INDENT * a.size
            if (full and below and abs(line.size - a.size) <= 0.5 and len(a.text) >= 8
                    and not a.text.endswith(_SENTENCE_STOP) and not _ITEM_START.match(line.text)):
                out[-1] = _flowing(out[-1], line.text) + line.text
                previous = (line, block)
                continue
        out.append(line.text)
        previous = (line, block)
    return out


def _by_layout(doc, textless: list[int], glyphs: "_Glyphs") -> list[Chunk]:
    pages: list[tuple[int, list[tuple[str, float, bool]]]] = []
    chars_by_size: Counter[float] = Counter()
    pages_with_line: Counter[str] = Counter()
    for page in doc:
        lines = list(_styled_lines(page, glyphs))
        pages.append((page.number + 1, lines))
        if not lines:
            textless.append(page.number + 1)
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


def _styled_lines(page, glyphs: "_Glyphs"):
    """(text, font size, bold) for every visual line on the page."""
    for block in page.get_text("dict", flags=_text_only())["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            text = norm("".join(glyphs.text(page, s) for s in spans))
            if text:
                yield text, round(max(s["size"] for s in spans), 1), any(s["flags"] & 16 for s in spans)


def _level(text: str, size: float, heading_sizes: list[float]) -> int:
    if m := _CHAPTER.match(text):
        return 2 if m.group(1) == "節" else 1
    if m := _NUMBERED.match(text):
        return min(m.group(1).count(".") + 1, MAX_HEADING_LEVELS)
    return min(heading_sizes.index(size) + 1, MAX_HEADING_LEVELS) if size in heading_sizes else MAX_HEADING_LEVELS
