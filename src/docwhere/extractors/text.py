"""Plain text and Markdown. Markdown is sectioned by its # headings; anchors are line numbers."""

from __future__ import annotations

import re
from pathlib import Path

from docwhere.extractors import register
from docwhere.model import Chunk, SectionBuilder

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_MARKDOWN = {".md", ".markdown"}


@register(".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".log")
def extract(path: Path) -> list[Chunk]:
    builder = SectionBuilder()
    markdown = path.suffix.lower() in _MARKDOWN
    in_fence = False
    for no, line in enumerate(_decode(path.read_bytes()).splitlines(), 1):
        if markdown and line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        m = _MD_HEADING.match(line) if markdown and not in_fence else None
        if m:
            builder.heading(len(m.group(1)), m.group(2), {"line": no})
        else:
            builder.add(line, {"line": no})
    return builder.finish()


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")
