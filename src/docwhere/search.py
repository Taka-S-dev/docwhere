"""Query the index and rank sections.

FTS5 (trigram) only gathers candidates; ranking happens here so it can favour what a person
means by "the place that documents X": a section whose heading names X beats a page that
mentions X in passing, a section matching several of the query's wordings beats one
matching a single wording many times, and an old version of a document yields to its newer
version.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from docwhere.index import Index, path_key, prefix_of
from docwhere.model import norm
from docwhere.versions import newer_versions

CANDIDATES_PER_TERM = 3000
SNIPPET_BEFORE = 40
SNIPPET_AFTER = 80
ROW_SNIPPET_CHARS = 240

W_TITLE = 3.0  # term in the section's own heading
W_CRUMB = 1.0  # term in a parent heading
W_FILENAME = 1.0
W_EXTRA_TERM = 2.0  # each additional distinct term found in the section
SUPERSEDED_FACTOR = 0.8  # an older version still shows, below the current one

CELL_SEP = "\t"  # see extractors.excel


@dataclass
class Hit:
    n: int
    path: str
    title: str
    crumb: str
    location: str
    snippet: str
    score: float
    terms: list[str]
    modified: str
    newer_version: str | None
    anchor: dict
    chunk_id: int

    def to_dict(self) -> dict:
        return asdict(self)


def search(
    index: Index,
    roots: list[Path],
    terms: list[str],
    *,
    require_all: bool = False,
    exts: list[str] | None = None,
    limit: int = 10,
    per_file: int = 3,
) -> list[Hit]:
    terms = list(dict.fromkeys(t for t in map(norm, terms) if t))
    if not terms:
        return []
    scope_sql, scope_args = _scope(roots, exts)

    candidates: set[int] = set()
    for term in terms:
        if len(term) >= 3:
            sql = (
                "SELECT c.id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN files f ON f.id = c.file_id"
                f" WHERE chunks_fts MATCH ? AND {scope_sql} ORDER BY chunks_fts.rank LIMIT ?"
            )
            args = ['"' + term.replace('"', '""') + '"']
        else:  # trigram needs 3+ characters; short terms fall back to a scan
            like = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            sql = (
                "SELECT c.id FROM chunks c JOIN files f ON f.id = c.file_id"
                f" WHERE (c.title LIKE ? ESCAPE '\\' OR c.crumb LIKE ? ESCAPE '\\' OR c.body LIKE ? ESCAPE '\\')"
                f" AND {scope_sql} LIMIT ?"
            )
            args = [like, like, like]
        candidates.update(r[0] for r in index.db.execute(sql, [*args, *scope_args, CANDIDATES_PER_TERM]))
    if not candidates:
        return []

    newer = newer_versions([(r["path"], r["mtime_ns"]) for r in index.files_in(roots)])
    folded = [t.lower() for t in terms]
    scored = []
    for row in _fetch(index, candidates):
        score, matched = _score(row, folded)
        if matched and (not require_all or len(matched) == len(terms)):
            if row["path"] in newer:
                score *= SUPERSEDED_FACTOR
            scored.append((score, row, [terms[i] for i in matched]))
    scored.sort(key=lambda s: (-s[0], s[1]["path"], s[1]["id"]))

    hits: list[Hit] = []
    per_path: dict[str, int] = {}
    for score, row, matched in scored:
        if per_path.get(row["path"], 0) >= per_file:
            continue
        per_path[row["path"]] = per_path.get(row["path"], 0) + 1
        snippet, anchor = _locate(row, matched)
        hits.append(
            Hit(
                n=len(hits) + 1,
                path=row["path"],
                title=row["title"],
                crumb=row["crumb"],
                location=location_label(anchor),
                snippet=snippet,
                score=round(score, 2),
                terms=matched,
                modified=time.strftime("%Y-%m-%d", time.localtime(row["mtime_ns"] / 1e9)),
                newer_version=newer.get(row["path"]),
                anchor=anchor,
                chunk_id=row["id"],
            )
        )
        if len(hits) >= limit:
            break
    return hits


def _scope(roots: list[Path], exts: list[str] | None) -> tuple[str, list]:
    parts, args = [], []
    for root in roots:
        prefix = prefix_of(path_key(Path(root).resolve()))
        parts.append("substr(f.key, 1, ?) = ?")
        args += [len(prefix), prefix]
    sql = "(" + " OR ".join(parts) + ")"
    if exts:
        sql += " AND (" + " OR ".join("f.key LIKE ?" for _ in exts) + ")"
        args += ["%." + e.lower().lstrip(".") for e in exts]
    return sql, args


def _fetch(index: Index, ids: set[int]):
    ids = list(ids)
    for i in range(0, len(ids), 900):
        batch = ids[i : i + 900]
        yield from index.db.execute(
            "SELECT c.id, c.title, c.crumb, c.body, c.anchors, f.path, f.mtime_ns"
            " FROM chunks c JOIN files f ON f.id = c.file_id"
            f" WHERE c.id IN ({','.join('?' * len(batch))})",
            batch,
        )


def _score(row, folded: list[str]) -> tuple[float, list[int]]:
    title, crumb, body = row["title"].lower(), row["crumb"].lower(), row["body"].lower()
    filename = norm(os.path.basename(row["path"])).lower()
    score, matched = 0.0, []
    for i, term in enumerate(folded):
        in_title, in_crumb, count = term in title, term in crumb, body.count(term)
        if not (in_title or in_crumb or count):
            continue
        matched.append(i)
        score += W_TITLE * in_title + W_CRUMB * in_crumb + math.log1p(count) + W_FILENAME * (term in filename)
    if matched:
        score += W_EXTRA_TERM * (len(matched) - 1)
    return score, matched


def _locate(row, terms: list[str]) -> tuple[str, dict]:
    """Snippet from the line matching the most terms, and that line's anchor."""
    lines = row["body"].split("\n")
    anchors = json.loads(row["anchors"])
    anchor_of = (lambda i: anchors[i]) if len(anchors) > 1 else (lambda i: anchors[0] if anchors else {})
    folded = [t.lower() for t in terms]
    best, best_count = 0, -1
    for i, line in enumerate(lines):
        if anchor_of(i).get("header"):
            continue  # a header row matches every query about its column names
        if line == row["title"] and len(lines) > 1:
            continue  # the heading is already shown as the crumb; prefer body text that says more
        count = sum(t in line.lower() for t in folded)
        if count > best_count:
            best, best_count = i, count
            if count == len(folded):
                break
    line = lines[best]
    low = line.lower()
    term, pos = min(((t, low.find(t)) for t in folded if t in low), key=lambda tp: tp[1], default=(folded[0], 0))

    anchor = dict(anchor_of(best))
    anchor["term"] = terms[folded.index(term)]
    anchor["text"] = line
    if "cols" in anchor:
        return _row_snippet(line, anchor, lines, anchors, term), anchor
    start, end = max(0, pos - SNIPPET_BEFORE), min(len(line), pos + len(term) + SNIPPET_AFTER)
    return ("…" if start else "") + line[start:end] + ("…" if end < len(line) else ""), anchor


def _row_snippet(line: str, anchor: dict, lines: list[str], anchors: list[dict], term: str) -> str:
    """Spreadsheet row as "header: value / ...", and point the anchor at the matching cell."""
    values = line.split(CELL_SEP)
    cols = anchor["cols"]
    hit_col = next((c for c, v in zip(cols, values) if term in v.lower()), cols[0])
    anchor["cell"] = f"{hit_col}{anchor['row']}"

    headers: dict[str, str] = {}
    for header_line, a in zip(lines, anchors):
        if a.get("header"):
            headers = dict(zip(a["cols"], header_line.split(CELL_SEP)))
            break
    text = " / ".join(f"{headers[c]}: {v}" if c in headers else v for c, v in zip(cols, values))
    return text if len(text) <= ROW_SNIPPET_CHARS else text[:ROW_SNIPPET_CHARS] + "…"


def location_label(anchor: dict) -> str:
    if "page" in anchor:
        return f"p.{anchor['page']}"
    if "slide" in anchor:
        return f"slide {anchor['slide']}"
    if "cell" in anchor:
        return f"{anchor['sheet']}!{anchor['cell']}"
    if "cols" in anchor:
        return f"{anchor['sheet']}!{anchor['cols'][0]}{anchor['row']}:{anchor['cols'][-1]}{anchor['row']}"
    if "line" in anchor:
        return f"L{anchor['line']}"
    return ""
