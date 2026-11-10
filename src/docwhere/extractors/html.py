"""HTML: saved web pages and wiki exports, sectioned by their h1-h6 headings.

The markup is only read, never rendered: scripts, styles and embedded objects are skipped,
and tables become one labelled line per row as in Word. Anchors carry the id of the heading
above a line when it has one, so a hit can link to that part of the page.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from docwhere.extractors import register
from docwhere.extractors.text import _decode
from docwhere.model import Chunk, SectionBuilder, norm, table_lines

MAX_HTML_CHARS = 20_000_000  # text kept from one page; the rest is dropped

_SKIP = {"script", "style", "noscript", "template", "svg", "math", "object", "iframe", "head", "select", "button"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
_BLOCK = {
    "p", "div", "li", "ul", "ol", "pre", "blockquote", "dd", "dt", "dl", "section", "article", "header",
    "footer", "nav", "aside", "main", "figure", "figcaption", "address", "hr", "br", "caption", "details", "summary",
}
_HEADINGS = {f"h{i}": i for i in range(1, 7)}


class _Reader(HTMLParser):
    def __init__(self, builder: SectionBuilder, shift: int, root_id: str | None) -> None:
        super().__init__(convert_charrefs=True)
        self.builder, self.shift, self.root_id = builder, shift, root_id
        self.root_tag: str | None = None  # set while inside the root element
        self.root_depth = 0
        self.found_root = False
        self.skip = 0
        self.text: list[str] = []
        self.heading: tuple[int, str] | None = None  # (level, id) while inside a heading
        self.anchor: dict = {}
        self.tables: list[dict] = []  # open tables: {"rows": [[cell, ...]], "cell": [text] | None}
        self.budget = MAX_HTML_CHARS
        self.title: list[str] = []
        self.in_title = False

    # -- where we are -------------------------------------------------------------------

    def _reading(self) -> bool:
        return not self.skip and (self.root_id is None or self.root_tag is not None)

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if self.root_id is not None and self.root_tag is None:
            if dict(attrs).get("id") == self.root_id and tag not in _VOID:
                self.root_tag, self.root_depth, self.found_root = tag, 1, True
            return
        if self.root_tag is not None and tag == self.root_tag:
            self.root_depth += 1
        if tag in _SKIP:
            if tag not in _VOID:
                self.skip += 1
            return
        if self.skip:
            return
        if tag in _HEADINGS and not self.tables:
            self._flush()
            self.heading = (_HEADINGS[tag], dict(attrs).get("id") or "")
        elif tag == "table":
            self._flush()
            self.tables.append({"rows": [], "cell": None})
        elif tag == "tr" and self.tables:
            self._end_cell()
            self.tables[-1]["rows"].append([])
        elif tag in ("td", "th") and self.tables:
            self._end_cell()
            if not self.tables[-1]["rows"]:
                self.tables[-1]["rows"].append([])
            self.tables[-1]["cell"] = []
        elif tag in _BLOCK:
            self._break()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if self.root_tag is not None and tag == self.root_tag:
            self.root_depth -= 1
            if self.root_depth == 0:
                self._flush()
                self.root_tag = None
                return
        if not self._reading() and not (self.skip and tag in _SKIP):
            return
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if tag in _HEADINGS and self.heading is not None:
            level, hid = self.heading
            text = " ".join("".join(self.text).split())
            self.text, self.heading = [], None
            hid = norm(hid).replace(" ", "")
            if hid:
                self.anchor = {"fragment": hid}
            self.builder.heading(level + self.shift, text, dict(self.anchor))
        elif tag == "table" and self.tables:
            self._end_cell()
            table = self.tables.pop()
            lines = table_lines(table["rows"])
            if self.tables and self.tables[-1]["cell"] is not None:  # a table inside a cell
                self.tables[-1]["cell"].append(" " + " ".join(lines) + " ")
            else:
                for line in lines:
                    self._spend(line)
                    self.builder.add(line, dict(self.anchor))
        elif tag in ("td", "th") and self.tables:
            self._end_cell()
        elif tag in _BLOCK:
            self._break()

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if not self._reading() or self.budget <= 0:
            return
        if self.tables and self.tables[-1]["cell"] is not None:
            self.tables[-1]["cell"].append(data)
        elif self.tables:
            return  # text between cells
        else:
            self.text.append(data)

    # -- output -------------------------------------------------------------------------

    def _end_cell(self):
        table = self.tables[-1]
        if table["cell"] is not None:
            text = " ".join("".join(table["cell"]).split())
            self._spend(text)
            table["rows"][-1].append(text)
            table["cell"] = None

    def _break(self):
        if self.heading is None and not self.tables:
            self._flush()

    def _flush(self):
        if self.heading is not None:
            return
        text = "".join(self.text)
        self.text = []
        if text.strip():
            self._spend(text)
            self.builder.add(text, dict(self.anchor))

    def _spend(self, text: str) -> None:
        self.budget -= len(text)


def html_chunks(html: str, title: str = "", root_id: str | None = None) -> list[Chunk]:
    """Chunks of an HTML document. ``title`` (a wiki page's name) becomes the top heading;
    ``root_id`` keeps only the element with that id (a wiki page's body without its menus),
    or the whole document when no element has it."""
    builder = SectionBuilder()
    if title:
        builder.heading(1, title, {})
    reader = _Reader(builder, 1 if title else 0, root_id)
    reader.feed(html)
    reader.close()
    reader._flush()
    if root_id is not None and not reader.found_root:
        return html_chunks(html, title)
    chunks = builder.finish()
    if not title and reader.title and chunks:
        page_title = norm("".join(reader.title))
        if page_title:  # the page's name heads every section, as a file name would
            for chunk in chunks:
                if chunk.crumb != page_title and not chunk.crumb.startswith(page_title + " > "):
                    chunk.crumb = f"{page_title} > {chunk.crumb}" if chunk.crumb else page_title
                chunk.title = chunk.title or page_title
    return chunks


@register(".html", ".htm")
def extract(path: Path) -> list[Chunk]:
    return html_chunks(_decode(path.read_bytes()))
