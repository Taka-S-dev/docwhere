"""Query the index and rank sections.

FTS5 (trigram) only gathers candidates; ranking happens here so it can favour what a person
means by "the place that documents X": a section whose heading names X beats a page that
mentions X in passing, a section matching several of the query's wordings beats one
matching a single wording many times, and an old version of a document yields to its newer
version.

Besides words, a search can leave out sections that contain a word (``exclude``), match a
regular expression instead of words (``regex``: error codes, part numbers, setting keys), or
look at file names only (``search_names``).
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from docwhere.index import Index, path_key, prefix_of
from docwhere.model import norm
from docwhere.versions import family

CANDIDATES_PER_TERM = 3000
# seconds a search may spend going through sections one by one (a term under three letters, a
# pattern with no plain run of three): past it the search answers with what it went through and
# says the rest was not looked at, rather than keep the window or the agent waiting minutes
SCAN_SECONDS = 5.0
REGEX_CANDIDATES = 20000  # a pattern is asked for every match, not the best few
MAX_SHOWN_MATCHES = 5  # distinct strings a pattern matched, kept per hit for highlighting
SNIPPET_BEFORE = 40
SNIPPET_AFTER = 80
ROW_SNIPPET_CHARS = 240

W_TITLE = 3.0  # term in the section's own heading
W_CRUMB = 1.0  # term in a parent heading
W_FILENAME = 1.0
W_EXTRA_TERM = 2.0  # each additional distinct term found in the section (wordings of one thing count once)
W_SAME_LINE = 1.5  # two of the things asked for stand close together in one line: a setting beside its value
NEAR_CHARS = 30  # ...this close
RARITY = 1.0  # a term found in few sections weighs up to this much more than the commonest one...
RARITY_SPAN = 50  # ...reached when it is found in this many times fewer sections
W_IN_HEADING = 0.25  # in choosing the snippet line: a term the heading already shows says little
LINE_CHARS = 600  # of the hit line given whole in the JSON, for quoting
SYNONYMS_NAME = "synonyms.csv"  # the user's own wordings, beside the index (see load_variant_groups)
_POINTS_ELSEWHERE = ("を参照してください。", "を参照。", "参照してください。")
SUGGEST_PROBES = 150  # parts of a term tried, at most, when the term itself is found nowhere
_XREF = re.compile(r"^(?:〔[0-9a-f]{4}〕)?\s*\d+ページ(?:\s|$)")  # "6042ページ 材料の数": points elsewhere
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
    # "p.12-18" when the hit is one page of a PDF section that runs over several pages, each
    # its own section with the same heading: the answer may be on a page next to this one
    heading_pages: str | None = None
    # the number printed on the hit's PDF page when it differs from the page index in
    # ``location`` (front matter shifts it); set by the command line, which cites pages
    printed_page: int | None = None
    # {"count", "page_share"}: pictures on the hit's PDF page whose words are not read, and the
    # share of the page they cover; set by the command line. What the question asks for may be
    # written in one of them, where no search finds it.
    pictures: dict | None = None
    # when not all the words asked for are in this part of the section: where in the same
    # section the rest are, [{"term", "location", "snippet"}] (see _across_sections)
    elsewhere: list[dict] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class PatternError(ValueError):
    """A regular expression that does not compile."""


def search(
    index: Index,
    roots: list[Path],
    terms: list[str],
    *,
    require_all: bool = False,
    exts: list[str] | None = None,
    limit: int = 10,
    per_file: int = 3,
    exclude: list[str] = (),
    regex: bool = False,
    capped: list[str] | None = None,
    found: dict | None = None,
    must: list[str] = (),
    groups: list[tuple[str, ...]] = (),
) -> list[Hit]:
    """Sections under ``roots`` matching ``terms`` (any of them, or all with ``require_all``),
    best first. A term is matched as written, spaces included, so a quoted phrase stays one.
    Sections or paths containing an ``exclude`` word are left out. With ``regex`` the terms
    are regular expressions (case ignored) and a hit's ``terms`` are the strings they matched.

    ``must`` terms are searched like the others and have to be there: the subject, beside
    wordings of which any will do. Terms that are wordings of one thing (a built-in group, one
    of ``groups``, サーバ / サーバー) count as one: any of them satisfies ``must`` or
    ``require_all``, and finding two of them is not finding two things.

    Each term brings at most CANDIDATES_PER_TERM sections to be ranked (REGEX_CANDIDATES for a
    pattern); the terms that reached it are added to ``capped``, as sections past it were never
    looked at and a better one may be among them. ``found`` is given how many sections matched
    and in how many files, before ``limit`` and ``per_file`` keep the first of them; with
    ``at_least`` set when a term reached its limit, so more may match than were counted."""
    if regex:
        return _with_heading_pages(index, _search_regex(index, roots, terms, require_all, exts, limit, per_file, exclude, capped, found))
    needed = list(dict.fromkeys(t for t in map(norm, must) if t))
    terms = list(dict.fromkeys(t for t in map(norm, [*terms, *needed]) if t))
    if not terms:
        return []
    scope_sql, scope_args = _scope(roots, exts)
    unwanted = _folded(exclude)
    group_of = term_groups(terms, groups)
    required = set(group_of) if require_all else {group_of[terms.index(t)] for t in needed}

    candidates: set[int] = set()
    sections_with: list[int] = []  # how many sections each term is in: a rare term says more
    deadline = time.monotonic() + SCAN_SECONDS
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
        ids, cut = _ids(index, sql, [*args, *scope_args, CANDIDATES_PER_TERM], deadline)
        if cut or len(ids) >= CANDIDATES_PER_TERM:
            if capped is not None:
                capped.append(term)
            if found is not None:
                found["at_least"] = True
        candidates.update(ids)
        sections_with.append(len(ids))
    if not candidates:
        return []
    commonest = max(sections_with)
    weights = [1.0 + RARITY * min(1.0, math.log(commonest / n) / math.log(RARITY_SPAN)) if n else 1.0 for n in sections_with]

    rows = list(_fetch(index, candidates))
    newer = index.newer_among({r["family"] for r in rows if r["family"]}, roots)  # the versions of these files only
    folded = [t.lower() for t in terms]
    scored, partly = [], []
    for row in rows:
        if _excluded(row, unwanted):
            continue
        score, matched = score_row(row, folded, weights, group_of)
        if not matched:
            continue
        if row["path"] in newer:
            score *= SUPERSEDED_FACTOR
        holds = {group_of[i] for i in matched}
        if required <= holds:
            scored.append((score, row, [terms[i] for i in matched]))
        elif len(required) > 1:
            partly.append((score, row, matched, holds))
    if partly:
        scored += _across_sections(index, partly, scored, required, terms, group_of)
    _count(scored, found)
    return _with_heading_pages(index, _hits(scored, newer, limit, per_file, found))


SPLIT_NEXT = 0.7  # all the words in a section, but not on one page or part: next to each other...
SPLIT_FARTHER = 0.9  # ...and this much less for each further page between them...
SPLIT_LEAST = 0.3  # ...down to this, so a section's words far apart still come before none


def _across_sections(index: Index, partly: list[tuple], whole: list[tuple], required: set[int],
                     terms: list[str], group_of: list[int]) -> list[tuple]:
    """Hits for sections that hold every word asked for, though no one part of them does.

    A section is read in parts: a bookmarked PDF a page each, a long one in pieces of
    MAX_CHUNK_CHARS. "This feature's setting" may have the feature named on one page and the
    setting a few pages on, under the same heading; asked for both, it is found only when the
    parts of one section count together. A heading is not crossed: words in two sections are
    two things that happen to be near.

    ``partly`` holds (score, row, matched, groups held) for parts holding some of the words.
    Each section whose parts hold them all gives one hit, at its part holding the most of them,
    ranked by how close the rest are (SPLIT_NEXT and on) and with where they are as
    ``elsewhere``. A section with a part that holds them all is a hit already (``whole``)."""
    by_heading: dict[tuple, list[tuple]] = {}
    for item in partly:
        row = item[1]
        by_heading.setdefault((row["path"], _heading(row)), []).append(item)
    held = {(r["path"], _heading(r)): [] for _, r, _ in whole}
    for _, r, _ in whole:
        held[(r["path"], _heading(r))].append(r["id"])
    out = []
    for key, items in by_heading.items():
        if len(items) < 2:
            continue
        items.sort(key=lambda item: item[1]["id"])
        runs, run = [], [items[0]]
        for before, after in zip(items, items[1:]):
            if _one_section(index, before[1], after[1]):
                run.append(after)
            else:
                runs.append(run)
                run = [after]
        runs.append(run)
        for run in runs:
            if not required <= set().union(*(item[3] for item in run)):
                continue
            first, last = run[0][1]["id"], run[-1][1]["id"]
            if any(first <= i <= last for i in held.get(key, ())):
                continue  # a part of this section holds every word: that is the hit
            score, row, matched, holds = max(run, key=lambda item: (len(item[3]), item[0]))
            elsewhere, more, farthest = [], 0.0, 0
            for group in sorted(required - holds):
                other = min((item for item in run if group in item[3]),
                            key=lambda item: (abs(item[1]["id"] - row["id"]), -item[0]))
                farthest = max(farthest, abs(other[1]["id"] - row["id"]))
                term = next(terms[i] for i in other[2] if group_of[i] == group)
                snippet, anchor = locate_snippet(other[1], [term])
                elsewhere.append({"term": term, "location": location_label(anchor), "snippet": snippet})
                more += other[0] / len(other[3])  # its share of the score of the part it is in
            near = max(SPLIT_LEAST, SPLIT_NEXT * SPLIT_FARTHER ** (farthest - 1))
            out.append(((score + more) * near, row, [terms[i] for i in matched], elsewhere))
    return out


def _heading(row) -> str:
    """What a part is a part of: its heading path (a bookmarked PDF's page in mid-section has
    the path, and a title that differs from its first page's when several bookmarks start
    there); its title only when it has no path."""
    return row["crumb"] or row["title"]


def _one_section(index: Index, before, after) -> bool:
    """Whether two parts with the same heading are of one section: nothing of another heading
    lies between them."""
    if after["id"] == before["id"] + 1:
        return True
    return index.db.execute(
        "SELECT 1 FROM chunks WHERE id > ? AND id < ? AND (file_id != (SELECT file_id FROM chunks WHERE id = ?)"
        " OR (CASE WHEN crumb != '' THEN crumb ELSE title END) != ?) LIMIT 1",
        (before["id"], after["id"], before["id"], _heading(before)),
    ).fetchone() is None


HEADING_PAGES_LOOKED_AT = 300  # pages either side of a hit looked at for the rest of its heading


def _with_heading_pages(index: Index, hits: list[Hit]) -> list[Hit]:
    """Set ``heading_pages`` on each hit that is one page of a heading spanning several pages (a
    bookmarked PDF is read a page a section). Only shown, never ranked: the order of the hits
    is the same with or without it."""
    for hit in hits:
        if "page" not in hit.anchor or not hit.title:
            continue
        rows = index.db.execute(
            "SELECT id, title, anchors FROM chunks WHERE file_id = (SELECT file_id FROM chunks WHERE id = ?)"
            " AND id BETWEEN ? AND ? ORDER BY id",
            (hit.chunk_id, hit.chunk_id - HEADING_PAGES_LOOKED_AT, hit.chunk_id + HEADING_PAGES_LOOKED_AT),
        ).fetchall()
        at = next((i for i, r in enumerate(rows) if r["id"] == hit.chunk_id), None)
        if at is None:
            continue

        def page_of(r, title: str = hit.title):
            anchors = json.loads(r["anchors"])
            return anchors[0].get("page") if len(anchors) == 1 and r["title"] == title else None

        first = last = at
        while first > 0 and rows[first - 1]["id"] == rows[first]["id"] - 1 and page_of(rows[first - 1]) is not None:
            first -= 1
        while last + 1 < len(rows) and rows[last + 1]["id"] == rows[last]["id"] + 1 and page_of(rows[last + 1]) is not None:
            last += 1
        if first < last and page_of(rows[first]) is not None and page_of(rows[last]) is not None:
            hit.heading_pages = f"p.{page_of(rows[first])}-{page_of(rows[last])}"
    return hits


def _count(scored: list[tuple], found: dict | None) -> None:
    if found is not None:
        found["sections"] = len(scored)
        found["files"] = len({s[1]["path"] for s in scored})
        found.setdefault("at_least", False)


def _hits(scored: list[tuple], newer: dict, limit: int, per_file: int, found: dict | None = None) -> list[Hit]:
    """The first ``limit`` of ``scored``, at most ``per_file`` of a file. With ``found`` holding
    "then_rest", the sections past ``per_file`` follow in rank order up to ``limit`` in all, and
    ``found["first"]`` says how many came before them."""
    scored.sort(key=lambda s: (-s[0], s[1]["path"], s[1]["id"]))
    per_path: dict[str, int] = {}
    first, rest = [], []
    for item in scored:
        path = item[1]["path"]
        if per_path.get(path, 0) < per_file:
            per_path[path] = per_path.get(path, 0) + 1
            first.append(item)
            if len(first) >= limit:
                break
        elif found is not None and found.get("then_rest"):
            rest.append(item)
    if found is not None and found.get("then_rest"):
        found["first"] = len(first)
    hits: list[Hit] = []
    for score, row, matched, *elsewhere in (first + rest)[:limit]:
        snippet, anchor = locate_snippet(row, matched)
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
                elsewhere=elsewhere[0] if elsewhere else None,
            )
        )
    return hits


def section_hit(row, terms: list[str], n: int) -> Hit:
    """A hit for the section ``row`` (from ``Index.chunk_with_neighbors``) with ``terms`` marked,
    for a section named by its id rather than found by the last search."""
    terms = terms or [row["title"] or Path(row["path"]).stem]
    snippet, anchor = locate_snippet(row, terms)
    try:
        modified = time.strftime("%Y-%m-%d", time.localtime(os.stat(row["path"]).st_mtime))
    except OSError:
        modified = ""
    return Hit(n=n, path=row["path"], title=row["title"], crumb=row["crumb"], location=location_label(anchor),
               snippet=snippet, score=0.0, terms=terms, modified=modified, newer_version=None, anchor=anchor,
               chunk_id=row["id"])


def _folded(words) -> list[str]:
    return [w.lower() for w in (norm(x) for x in words) if w]


def _excluded(row, unwanted: list[str]) -> bool:
    """Whether the section, or the path of its file, contains one of ``unwanted``."""
    if not unwanted:
        return False
    text = "\n".join((row["title"], row["crumb"], row["body"], norm(row["path"]))).lower()
    return any(w in text for w in unwanted)


def compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    out = []
    for p in patterns:
        try:
            out.append(re.compile(norm(p), re.IGNORECASE))
        except re.error as e:
            raise PatternError(f"not a valid regular expression: {p} ({e})") from None
    return out


def required_literal(pattern: str) -> str:
    """The longest run of plain characters every match of ``pattern`` must contain ("" when
    there is none to rely on), so the full-text index can narrow the sections to look at."""
    if "|" in pattern.replace("\\|", ""):
        return ""  # alternatives: no single run is required
    runs, run, i = [], "", 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern):
            nxt = pattern[i + 1]
            if nxt.isalnum():  # \d, \w, \b...: a class or an assertion, not a character
                runs.append(run)
                run = ""
            else:
                run += nxt
            i += 2
            continue
        if c in "?*{":  # the character before is optional (or repeated from zero)
            run = run[:-1]
            runs.append(run)
            run = ""
            if c == "{":
                i = pattern.find("}", i) if "}" in pattern[i:] else len(pattern)
        elif c in "([":
            runs.append(run)
            run = ""
            depth, close = 0, ")" if c == "(" else "]"
            while i < len(pattern):  # skip the group or class
                if pattern[i] == "\\":
                    i += 2
                    continue
                depth += pattern[i] == c
                depth -= pattern[i] == close
                if depth == 0:
                    break
                i += 1
        elif c in ".^$+)]}":
            runs.append(run)
            run = ""
        else:
            run += c
        i += 1
    runs.append(run)
    return max(runs, key=len)


def _search_regex(index, roots, patterns, require_all, exts, limit, per_file, exclude, capped=None, found=None) -> list[Hit]:
    compiled = compile_patterns([p for p in patterns if p])
    if not compiled:
        return []
    scope_sql, scope_args = _scope(roots, exts)
    unwanted = _folded(exclude)
    candidates: set[int] = set()
    deadline = time.monotonic() + SCAN_SECONDS
    for pattern in compiled:
        literal = required_literal(pattern.pattern)
        if len(literal) >= 3:  # the full-text index finds the sections that can match
            sql = ("SELECT c.id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN files f ON f.id = c.file_id"
                   f" WHERE chunks_fts MATCH ? AND {scope_sql} LIMIT ?")
            args = ['"' + literal.replace('"', '""') + '"', *scope_args, REGEX_CANDIDATES]
        else:  # nothing to narrow by: every section in scope is looked at
            sql = f"SELECT c.id FROM chunks c JOIN files f ON f.id = c.file_id WHERE {scope_sql}"
            args = scope_args
        ids, cut = _ids(index, sql, args, deadline)
        if cut or (len(literal) >= 3 and len(ids) >= REGEX_CANDIDATES):
            if capped is not None:
                capped.append(pattern.pattern)
            if found is not None:
                found["at_least"] = True
        candidates.update(ids)
    if not candidates:
        return []
    scored = []
    for n, row in enumerate(_fetch(index, candidates)):
        if n % 500 == 0 and time.monotonic() > deadline:  # the rest is not looked at, and that is said
            if capped is not None and not capped:
                capped.extend(p.pattern for p in compiled)
            if found is not None:
                found["at_least"] = True
            break
        if _excluded(row, unwanted):
            continue
        text = "\n".join((row["title"], row["crumb"], row["body"]))
        matches = [[m.group(0) for m in p.finditer(text)] for p in compiled]
        if not any(matches) or (require_all and not all(matches)):
            continue
        strings = [m for ms in matches for m in ms]
        matched = list(dict.fromkeys(s for s in strings if s))[:MAX_SHOWN_MATCHES]
        if not matched:
            continue
        score = math.log1p(len(strings)) + W_TITLE * any(p.search(row["title"]) for p in compiled)
        scored.append((score, row, matched))
    newer = index.newer_among({s[1]["family"] for s in scored if s[1]["family"]}, roots)
    scored = [(score * SUPERSEDED_FACTOR if row["path"] in newer else score, row, m) for score, row, m in scored]
    _count(scored, found)
    return _hits(scored, newer, limit, per_file, found)


def search_names(
    index: Index,
    roots: list[Path],
    terms: list[str],
    *,
    exts: list[str] | None = None,
    limit: int = 10,
    exclude: list[str] = (),
    regex: bool = False,
    unread: list[tuple[str, int]] = (),
) -> list[Hit]:
    """Files under ``roots`` whose name (or, for words, the folders above it) matches every
    term, those matching in the name itself and shorter names first. Files that could not be
    read, and ``unread`` ones (path, mtime_ns) not read yet, are found too; their hit has no
    section (``chunk_id`` 0)."""
    scope_sql, scope_args = _scope(roots, exts)
    unwanted = _folded(exclude)
    compiled = compile_patterns(terms) if regex else []
    words = [] if regex else _folded(terms)
    if not compiled and not words:
        return []
    rows = [dict(r) for r in index.db.execute(
        f"SELECT f.id, f.path, f.mtime_ns, f.error, (SELECT min(c.id) FROM chunks c WHERE c.file_id = f.id) AS first"
        f" FROM files f WHERE {scope_sql}", scope_args)]
    known = {path_key(Path(r["path"])) for r in rows}
    rows += [{"path": str(p), "mtime_ns": m, "error": None, "first": None, "unread": True}
             for p, m in unread if path_key(Path(p)) not in known]
    scored = []
    for r in rows:
        path = norm(r["path"])
        name = os.path.basename(path)
        folded_path, folded_name = path.lower(), name.lower()
        if any(w in folded_path for w in unwanted):
            continue
        if compiled:
            found = [m.group(0) for p in compiled for m in p.finditer(name)]
            if not any(found):
                continue
            matched = list(dict.fromkeys(m for m in found if m))[:MAX_SHOWN_MATCHES] or [name]
            score = 1.0
        else:
            if not all(w in folded_path for w in words):
                continue
            matched = [t for t, w in zip(terms, words) if w in folded_name] or terms
            score = sum(W_TITLE if w in folded_name else W_CRUMB for w in words)
        score -= len(name) / 1e4  # of equal matches, the shorter name (the closer one) first
        score += r["mtime_ns"] / 1e9 / 1e10  # then the newer file
        scored.append((score, r, matched))
    newer = index.newer_among({family(s[1]["path"])[0] for s in scored}, roots)
    scored = [(score * SUPERSEDED_FACTOR if r["path"] in newer else score, r, m) for score, r, m in scored]
    scored.sort(key=lambda s: (-s[0], s[1]["path"]))
    hits = []
    for score, r, matched in scored[:limit]:
        folder = os.path.dirname(r["path"])
        hits.append(Hit(
            n=len(hits) + 1, path=r["path"], title=os.path.basename(r["path"]), crumb="", location="",
            snippet=folder + (f"  (unreadable: {r['error']})" if r["error"] else "  (not read yet)" if r.get("unread") else ""),
            score=round(score, 2),
            terms=matched, modified=time.strftime("%Y-%m-%d", time.localtime(r["mtime_ns"] / 1e9)),
            newer_version=newer.get(r["path"]), anchor={}, chunk_id=r["first"] or 0,
        ))
    return hits


def suggest_parts(index: Index, roots: list[Path], terms: list[str], exts: list[str] | None = None) -> list[dict]:
    """For terms found nowhere, their parts that are found: "何個まで登録できる" is in no
    document as written, "登録できる" is in many. The longest part from each letter on is taken,
    so the parts do not overlap. [{term, part, sections}], sections counted up to 1000."""
    scope_sql, scope_args = _scope(roots, exts)

    def sections(part: str) -> int:
        row = index.db.execute(
            "SELECT count(*) FROM (SELECT 1 FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
            f" JOIN files f ON f.id = c.file_id WHERE chunks_fts MATCH ? AND {scope_sql} LIMIT 1000)",
            ['"' + part.replace('"', '""') + '"', *scope_args],
        ).fetchone()
        return row[0]

    out: list[dict] = []
    probes = 0
    for term in dict.fromkeys(t for t in map(norm, terms) if len(t) > 3):
        if sections(term):
            continue
        i = 0
        while i <= len(term) - 3 and probes < SUGGEST_PROBES:
            for j in range(len(term), i + 2, -1):
                part = term[i:j].strip()
                if len(part) < 3 or part == term:
                    continue
                probes += 1
                if n := sections(part):
                    out.append({"term": term, "part": part, "sections": n})
                    i = j - 1
                    break
                if probes >= SUGGEST_PROBES:
                    break
            i += 1
    return out


def _scope(roots: list[Path], exts: list[str] | None) -> tuple[str, list]:
    parts, args = [], []
    for root in roots:
        key = path_key(Path(root).resolve())
        prefix = prefix_of(key)
        # the files under a folder, or the one file the path names
        parts.append("(substr(f.key, 1, ?) = ? OR f.key = ?)")
        args += [len(prefix), prefix, key]
    sql = "(" + " OR ".join(parts) + ")"
    if exts:
        sql += " AND (" + " OR ".join("f.key LIKE ?" for _ in exts) + ")"
        args += ["%." + e.lower().lstrip(".") for e in exts]
    return sql, args


def _ids(index: Index, sql: str, args: list, deadline: float) -> tuple[list[int], bool]:
    """The ids ``sql`` yields until ``deadline``; True when it was stopped there, the rest not seen."""
    ids: list[int] = []
    index.db.set_progress_handler(lambda: time.monotonic() > deadline, 20000)
    try:
        for r in index.db.execute(sql, args):
            ids.append(r[0])
    except sqlite3.OperationalError as e:
        if "interrupt" not in str(e):
            raise
        return ids, True
    finally:
        index.db.set_progress_handler(None, 0)
    return ids, False


def _fetch(index: Index, ids: set[int]):
    ids = list(ids)
    for i in range(0, len(ids), 900):
        batch = ids[i : i + 900]
        yield from index.db.execute(
            "SELECT c.id, c.title, c.crumb, c.body, c.anchors, f.path, f.mtime_ns, f.family,"
            " (SELECT p.title FROM chunks p WHERE p.id = c.id - 1 AND p.file_id = c.file_id) = c.title AS continued"
            " FROM chunks c JOIN files f ON f.id = c.file_id"
            f" WHERE c.id IN ({','.join('?' * len(batch))})",
            batch,
        )


def term_groups(terms: list[str], groups: list[tuple[str, ...]] = ()) -> list[int]:
    """For each term the number of its group: terms that are wordings of one thing share one.
    A group is a line of VARIANT_GROUPS or of ``groups``, or a katakana word with and without
    its closing long vowel."""
    keys: list[tuple] = []
    for term in terms:
        low = term.lower()
        key: tuple = ("term", low)
        for n, group in enumerate([*VARIANT_GROUPS, *groups]):
            if low in (w.lower() for w in group):
                key = ("group", n)
                break
        else:
            stem = term.rstrip("ー")
            if len(stem) >= 3 and _KATAKANA_TAIL.match(stem):
                key = ("term", stem.lower())
        keys.append(key)
    numbers = {key: n for n, key in enumerate(dict.fromkeys(keys))}
    return [numbers[key] for key in keys]


def score_row(row, folded: list[str], weights: list[float] | None = None,
              group_of: list[int] | None = None) -> tuple[float, list[int]]:
    """The section's score for the terms, and which of them it holds. ``weights`` scales each
    term's share (see RARITY); ``group_of`` says which terms are wordings of one thing."""
    title, crumb, body = row["title"].lower(), row["crumb"].lower(), row["body"].lower()
    filename = norm(os.path.basename(row["path"])).lower()
    score, matched = 0.0, []
    # a later page of the section above it; keys(), as "in" looks through an sqlite3.Row's values
    continued = "continued" in row.keys() and bool(row["continued"])  # noqa: SIM118
    # Wordings of one thing are scored as that one thing: where it is (heading, parent heading,
    # file name), how often its commonest wording is in the text, and how rare it is. Scored a
    # term each, a section repeating 既定値 and 初期値 outranked the one about the subject asked for.
    things: dict[int, list] = {}
    for i, term in enumerate(folded):
        in_title, in_crumb, count, in_name = term in title, term in crumb, body.count(term), term in filename
        # a file named for what is asked about holds it in every section: "this feature's
        # setting" asked of a manual a file per feature
        if not (in_title or in_crumb or count or in_name):
            continue
        matched.append(i)
        thing = things.setdefault(group_of[i] if group_of else i, [False, False, 0, False, math.inf])
        thing[0] |= in_title
        thing[1] |= in_crumb
        thing[2] = max(thing[2], count)
        thing[3] |= in_name
        thing[4] = min(thing[4], weights[i] if weights else 1.0)
    for in_title, in_crumb, count, in_name, weight in things.values():
        score += weight * (W_TITLE * (in_title and not continued) + W_CRUMB * in_crumb + math.log1p(count)
                           + W_FILENAME * in_name)
    if matched:
        score += W_EXTRA_TERM * (len(things) - 1)
        if len(things) > 1:
            score += _nearness(body, [(folded[i], group_of[i] if group_of else i) for i in matched])
    return score, matched


def _nearness(body: str, terms: list[tuple[str, int]]) -> float:
    """What the section gains for holding two of the things asked for close together: W_SAME_LINE
    within NEAR_CHARS of each other in one line ("トマトは16個まで"), a third of it anywhere in
    one line (a table row is a long line: its cells belong together, less tightly)."""
    best = 0.0
    for line in body.split("\n"):
        at = [(pos, group) for term, group in terms for pos in _positions(line, term)]
        if len({group for _, group in at}) < 2:
            continue
        at.sort()
        if any(b[1] != a[1] and b[0] - a[0] <= NEAR_CHARS for a, b in zip(at, at[1:])):
            return W_SAME_LINE
        best = W_SAME_LINE / 3
    return best


def _positions(line: str, term: str, limit: int = 10) -> list[int]:
    out, start = [], 0
    while len(out) < limit and (i := line.find(term, start)) >= 0:
        out.append(i)
        start = i + 1
    return out


def locate_snippet(row, terms: list[str]) -> tuple[str, dict]:
    """Snippet from the line that says the most, and that line's anchor: the line holding the
    most terms, where a term the section's heading already shows counts for little (the subject
    is in the heading; the line wanted is the one with what is said about it), and a line that
    only points to another page ("6042ページ 材料の数") is passed over when another line matches."""
    lines = row["body"].split("\n")
    anchors = json.loads(row["anchors"])
    anchor_of = (lambda i: anchors[i]) if len(anchors) > 1 else (lambda i: anchors[0] if anchors else {})
    folded = [t.lower() for t in terms]
    heading = f"{row['title']} {row['crumb']}".lower()
    worth = [W_IN_HEADING if t in heading else 1.0 for t in folded]
    best, best_count = 0, -1.0
    for i, line in enumerate(lines):
        if anchor_of(i).get("header"):
            continue  # a header row matches every query about its column names
        if line == row["title"] and len(lines) > 1:
            continue  # the heading is already shown as the crumb; prefer body text that says more
        low = line.lower()
        count = sum(w for t, w in zip(folded, worth) if t in low)
        if count and (_XREF.match(line) or line.endswith(_POINTS_ELSEWHERE)):
            count *= 0.5 if count > 1 else 0.1  # the place it points to is worth showing, not the pointer
        if count > best_count:
            best, best_count = i, count
    line = lines[best]
    low = line.lower()
    # shown around the term that says most: the one the heading does not already show
    here = [(t, low.find(t), w) for t, w in zip(folded, worth) if t in low]
    term, pos, _ = min(here, key=lambda tpw: (-tpw[2], tpw[1]), default=(folded[0], 0, 0))

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


_QUERY_TOKEN = re.compile(r'(-?)"([^"]*)"|(\S+)')


def parse_query(text: str) -> tuple[list[str], list[str]]:
    """A query typed in a box: words split at spaces, "a phrase" kept whole, and a word or
    phrase after a minus (-旧版) to leave out. Returns (terms, exclude)."""
    terms, exclude = [], []
    for minus, phrase, word in _QUERY_TOKEN.findall(text):
        if word:
            minus, phrase = ("-", word[1:]) if word.startswith("-") and len(word) > 1 else ("", word)
        if phrase.strip():
            (exclude if minus else terms).append(phrase.strip())
    return terms, exclude


# Wordings documents use for the same thing. Each group is searched as one: a term in a group
# brings the rest along. The user's own groups come from synonyms.csv beside the index.
VARIANT_GROUPS = [
    ("ログイン", "ログオン", "サインイン"),
    ("ログアウト", "ログオフ", "サインアウト"),
    ("設定", "コンフィグ", "config"),
    ("既定値", "デフォルト", "初期値", "default"),
    ("上限", "最大", "最大値"),
    ("下限", "最小", "最小値"),
    ("削除", "消去"),
    ("起動", "立ち上げ"),
    ("再起動", "リブート", "reboot"),
    ("エラー", "異常", "error"),
    ("タイムアウト", "timeout"),
    ("パスワード", "パスフレーズ"),
    ("ユーザー名", "ユーザID", "ユーザーID", "アカウント名"),
]
_KATAKANA_TAIL = re.compile(r"^[ァ-ヴー]+[ァ-ヴ]$")
_CUT_LONG_VOWEL = set("タダザバラリジ")  # last kana of words written with or without ー (サーバ, メモリ)


def load_variant_groups(path: Path) -> list[tuple[str, ...]]:
    """The user's groups: one per line of ``path``, words separated by commas; # starts a comment."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return []
    groups = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        words = tuple(w for w in (norm(x) for x in line.split(",")) if w)
        if len(words) > 1:
            groups.append(words)
    return groups


def expand_variants(terms: list[str], groups: list[tuple[str, ...]] = ()) -> list[str]:
    """Wordings to search besides ``terms`` (none of the terms themselves): the other members
    of a group a term belongs to, and a katakana word with and without its closing long vowel
    (サーバ / サーバー, ユーザ / ユーザー)."""
    have = {norm(t).lower() for t in terms}
    extra: list[str] = []

    def add(word: str) -> None:
        if word.lower() not in have:
            have.add(word.lower())
            extra.append(word)

    for term in map(norm, terms):
        for group in [*VARIANT_GROUPS, *groups]:
            if term.lower() in (w.lower() for w in group):
                for w in group:
                    add(w)
        stem = term.rstrip("ー")
        if len(stem) >= 3 and _KATAKANA_TAIL.match(stem):
            if stem != term:
                add(stem)  # サーバー -> サーバ
            elif stem[-1] in _CUT_LONG_VOWEL:
                add(stem + "ー")  # サーバ -> サーバー; only endings that drop it (-er, -or, -ry)
    return extra
