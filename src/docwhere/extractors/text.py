"""Plain text, Markdown and CSV. Markdown is sectioned by its # headings; a CSV row is one line
labelled by the header row, as a sheet's row is. Anchors are line numbers."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from docwhere.extractors import note, register
from docwhere.model import MAX_HEADER_CHARS, Chunk, SectionBuilder, norm

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_MARKDOWN = {".md", ".markdown"}
_DELIMITED = {".csv": ",", ".tsv": "\t"}
GARBLED_SHARE = 0.02  # this share of undecodable characters and the file is noted as partly unreadable


@register(".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".log")
def extract(path: Path) -> list[Chunk]:
    builder = SectionBuilder()
    markdown = path.suffix.lower() in _MARKDOWN
    in_fence = False
    text = _decode(path.read_bytes())
    if text and text.count("\ufffd") > len(text) * GARBLED_SHARE:
        note("the file is in an encoding docwhere does not know: much of its text could not be read")
    if delimiter := _DELIMITED.get(path.suffix.lower()):
        for no, line in _rows(text, delimiter):
            builder.add(line, {"line": no})
        return builder.finish()
    for no, line in enumerate(text.splitlines(), 1):
        if markdown and line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        m = _MD_HEADING.match(line) if markdown and not in_fence else None
        if m:
            builder.heading(len(m.group(1)), m.group(2), {"line": no})
        else:
            builder.add(line, {"line": no})
    return builder.finish()


def _rows(text: str, delimiter: str):
    """(line number, text) for each row: "項目: 接続タイムアウト / 既定値: 30秒" when the first row
    is a header of distinct short labels, so a hit on a name carries its value; the cells joined
    by " / " otherwise. The header row itself is no line: every row already names its columns."""
    try:
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
        rows = [(reader.line_num, [norm(c) for c in row]) for row in reader]
    except csv.Error:
        yield from enumerate(text.splitlines(), 1)  # not a table after all: read as lines
        return
    rows = [(no, cells) for no, cells in rows if any(cells)]
    if not rows:
        return
    header = rows[0][1]
    labelled = (len(rows) > 1 and len(header) > 1 and all(header) and len(set(header)) == len(header)
                and max(map(len, header)) <= MAX_HEADER_CHARS)
    for no, cells in rows[1:] if labelled else rows:
        if labelled and len(cells) == len(header):
            yield no, " / ".join(f"{h}: {c}" for h, c in zip(header, cells) if c)
        else:
            yield no, " / ".join(c for c in cells if c)


def _decode(data: bytes) -> str:
    """The file's text. UTF-16 is told by its byte order mark, or by the zero bytes that every
    other letter of mostly-ASCII text has in it (what PowerShell's ">" and many Windows tools
    write); read a byte a letter it is garbage, found by no search and reported by nothing."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    head = data[:4096]
    if len(head) >= 16 and head.count(0) > len(head) // 4:
        even, odd = head[0::2].count(0), head[1::2].count(0)
        return data.decode("utf-16-le" if odd > even else "utf-16-be", errors="replace")
    for encoding in ("utf-8-sig", "cp932", "euc_jp"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")
