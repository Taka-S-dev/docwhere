"""Wiki pages cut into sections and ranked the way local hits are."""

from __future__ import annotations

import json
import urllib.parse

from docwhere.extractors.html import html_chunks
from docwhere.model import norm
from docwhere.search import locate_snippet, score_row
from docwhere.wiki.core import Page, WikiHit


def hits(name: str, pages: list[Page], terms: list[str], per_page: int = 2) -> list[WikiHit]:
    """The best sections of each page for ``terms``, ranked like local hits."""
    folded = [t.lower() for t in map(norm, terms) if t]
    out: list[WikiHit] = []
    for rank, page in enumerate(pages):
        rows = [
            {"title": c.title, "crumb": c.crumb, "body": "\n".join(c.lines), "path": page.title, "anchors": json.dumps(c.anchors)}
            for c in html_chunks(page.html, page.title, page.root)
        ]
        scored = sorted(((score_row(r, folded)[0], i) for i, r in enumerate(rows)), key=lambda s: (-s[0], s[1]))
        best = [i for score, i in scored if score > 0][:per_page]
        if not best and rows:  # the wiki matched a form of the word docwhere does not see
            best = [1 if len(rows) > 1 else 0]  # the first section after the bare page title
        for i in best:
            row = rows[i]
            snippet, anchor = locate_snippet(row, [t for t in map(norm, terms) if t]) if folded else (row["body"][:120], {})
            fragment = anchor.get("fragment")
            out.append(WikiHit(
                0, name, page.id, page.title, row["crumb"], page.space,
                page.url + (f"#{urllib.parse.quote(fragment)}" if fragment else ""),
                snippet, terms, page.updated, round(score_row(row, folded)[0] - rank * 0.01, 3),
                {k: v for k, v in anchor.items() if k in ("fragment", "term", "text")},
            ))
    out.sort(key=lambda h: -h.score)
    for n, hit in enumerate(out, 1):
        hit.n = n
    return out
