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
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from docwhere.index import Index, path_key, prefix_of
from docwhere.model import norm
from docwhere.versions import newer_versions

CANDIDATES_PER_TERM = 3000
REGEX_CANDIDATES = 20000  # a pattern is asked for every match, not the best few
MAX_SHOWN_MATCHES = 5  # distinct strings a pattern matched, kept per hit for highlighting
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
) -> list[Hit]:
    """Sections under ``roots`` matching ``terms`` (any of them, or all with ``require_all``),
    best first. A term is matched as written, spaces included, so a quoted phrase stays one.
    Sections or paths containing an ``exclude`` word are left out. With ``regex`` the terms
    are regular expressions (case ignored) and a hit's ``terms`` are the strings they matched."""
    if regex:
        return _search_regex(index, roots, terms, require_all, exts, limit, per_file, exclude)
    terms = list(dict.fromkeys(t for t in map(norm, terms) if t))
    if not terms:
        return []
    scope_sql, scope_args = _scope(roots, exts)
    unwanted = _folded(exclude)

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
        if _excluded(row, unwanted):
            continue
        score, matched = score_row(row, folded)
        if matched and (not require_all or len(matched) == len(terms)):
            if row["path"] in newer:
                score *= SUPERSEDED_FACTOR
            scored.append((score, row, [terms[i] for i in matched]))
    return _hits(scored, newer, limit, per_file)


def _hits(scored: list[tuple], newer: dict, limit: int, per_file: int) -> list[Hit]:
    scored.sort(key=lambda s: (-s[0], s[1]["path"], s[1]["id"]))
    hits: list[Hit] = []
    per_path: dict[str, int] = {}
    for score, row, matched in scored:
        if per_path.get(row["path"], 0) >= per_file:
            continue
        per_path[row["path"]] = per_path.get(row["path"], 0) + 1
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
            )
        )
        if len(hits) >= limit:
            break
    return hits


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


def _search_regex(index, roots, patterns, require_all, exts, limit, per_file, exclude) -> list[Hit]:
    compiled = compile_patterns([p for p in patterns if p])
    if not compiled:
        return []
    scope_sql, scope_args = _scope(roots, exts)
    unwanted = _folded(exclude)
    candidates: set[int] = set()
    for pattern in compiled:
        literal = required_literal(pattern.pattern)
        if len(literal) >= 3:  # the full-text index finds the sections that can match
            sql = ("SELECT c.id FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid JOIN files f ON f.id = c.file_id"
                   f" WHERE chunks_fts MATCH ? AND {scope_sql} LIMIT ?")
            args = ['"' + literal.replace('"', '""') + '"', *scope_args, REGEX_CANDIDATES]
        else:  # nothing to narrow by: every section in scope is looked at
            sql = f"SELECT c.id FROM chunks c JOIN files f ON f.id = c.file_id WHERE {scope_sql}"
            args = scope_args
        candidates.update(r[0] for r in index.db.execute(sql, args))
    if not candidates:
        return []
    newer = newer_versions([(r["path"], r["mtime_ns"]) for r in index.files_in(roots)])
    scored = []
    for row in _fetch(index, candidates):
        if _excluded(row, unwanted):
            continue
        text = "\n".join((row["title"], row["crumb"], row["body"]))
        found = [[m.group(0) for m in p.finditer(text)] for p in compiled]
        if not any(found) or (require_all and not all(found)):
            continue
        strings = [m for ms in found for m in ms]
        matched = list(dict.fromkeys(s for s in strings if s))[:MAX_SHOWN_MATCHES]
        if not matched:
            continue
        score = math.log1p(len(strings)) + W_TITLE * any(p.search(row["title"]) for p in compiled)
        if row["path"] in newer:
            score *= SUPERSEDED_FACTOR
        scored.append((score, row, matched))
    return _hits(scored, newer, limit, per_file)


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
    newer = newer_versions([(r["path"], r["mtime_ns"]) for r in index.files_in(roots)])
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
        if r["path"] in newer:
            score *= SUPERSEDED_FACTOR
        scored.append((score, r, matched))
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


def score_row(row, folded: list[str]) -> tuple[float, list[int]]:
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


def locate_snippet(row, terms: list[str]) -> tuple[str, dict]:
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
