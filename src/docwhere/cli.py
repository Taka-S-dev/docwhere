"""Command line: search, show, open, index, status."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from docwhere.index import Index, default_db_path

HINT = "docwhere show --id ID prints the whole section; docwhere open N opens the hit"
MAX_UNREADABLE_LISTED = 20


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")  # cp932 consoles would choke on some characters
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, NotADirectoryError, LookupError) as e:
        print(f"docwhere: {e}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docwhere",
        description="Search PDF, Word, Excel, PowerPoint and text files section by section, then open the hit.",
    )
    parser.add_argument("--db", type=Path, default=None, help=f"index file (default: {default_db_path()})")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="search (updates the index for the folders first)")
    p.add_argument("terms", nargs="+", help="search terms; a section matching any of them is a hit (see --all)")
    p.add_argument("--in", dest="roots", type=Path, action="append", help="folder to search, repeatable (default: .)")
    p.add_argument("--all", action="store_true", help="only sections containing every term")
    p.add_argument("--ext", help="restrict to extensions, e.g. pdf,docx")
    p.add_argument("-n", "--limit", type=int, default=10)
    p.add_argument("--per-file", type=int, default=3, help="max hits per file (default 3)")
    p.add_argument("--no-update", action="store_true", help="search the index as is, skip the change scan")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="print the full section of hit N from the last search")
    p.add_argument("n", type=int, nargs="?")
    p.add_argument("--id", type=int, help="section id from search --json (safe when several searches run at once)")
    p.add_argument("-C", "--context", type=int, default=0, help="also print this many neighbouring chunks")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("open", help="open hit N from the last search at its location")
    p.add_argument("n", type=int)
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("index", help="build or update the index for folders")
    p.add_argument("roots", type=Path, nargs="+")
    p.add_argument("--rebuild", action="store_true", help="drop and re-extract everything under the folders")
    p.add_argument("--workers", type=int, default=None)
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("status", help="indexed folders and files that could not be read")
    p.add_argument("--errors", action="store_true", help="list unreadable files")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)
    return parser


# ---- commands --------------------------------------------------------------------------


def cmd_search(args) -> int:
    from docwhere.search import search

    roots = args.roots or [Path(".")]
    index = Index(args.db)
    updates = []
    if not args.no_update:
        for root in roots:
            updates.append(index.sync(root, progress=_progress(f"indexing {root}")))
    exts = [e.strip() for e in args.ext.split(",")] if args.ext else None
    hits = search(index, roots, args.terms, require_all=args.all, exts=exts, limit=args.limit, per_file=args.per_file)
    _last_path(index).write_text(
        json.dumps({"terms": args.terms, "hits": [h.to_dict() for h in hits]}, ensure_ascii=False), encoding="utf-8"
    )

    # Every unreadable file in scope, not only the ones that failed during this sync: a file
    # that failed last week is just as invisible to this search.
    unreadable = index.errors(roots)
    if args.json:
        out = {
            "terms": args.terms,
            "roots": [str(Path(r).resolve()) for r in roots],
            "index": {k: sum(getattr(u, k) for u in updates) for k in ("files", "updated", "removed")},
            "hits": [
                {"id" if k == "chunk_id" else k: v for k, v in h.to_dict().items() if k != "anchor"} for h in hits
            ],
            "unreadable": [{"path": p, "error": e} for p, e in unreadable[:MAX_UNREADABLE_LISTED]],
            "unreadable_count": len(unreadable),
            "next": HINT,
        }
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    if not hits:
        print("no hits")
    for h in hits:
        where = "  ".join(x for x in (h.crumb or h.title, h.location) if x)
        print(f"{h.n:>2}. {Path(h.path).name}  {where}  ({h.modified})")
        print(f"    {_highlight(h.snippet, h.terms)}")
        print(f"    {h.path}")
        if h.newer_version:
            print(f"    ! newer version: {Path(h.newer_version).name}")
    if unreadable:
        print(f"\n{len(unreadable)} file(s) here could not be searched; see: docwhere status --errors", file=sys.stderr)
    return 0


def cmd_show(args) -> int:
    from docwhere.search import CELL_SEP, location_label

    index = Index(args.db)
    if args.id is not None:
        chunk_id = args.id
    elif args.n is not None:
        chunk_id = _last_hit(index, args.n)["chunk_id"]
    else:
        raise LookupError("give a hit number N or --id")
    rows = index.chunk_with_neighbors(chunk_id, args.context)
    if not rows:
        raise LookupError("the index changed since that search; search again")
    print(f"# {Path(rows[0]['path']).name}\n# {rows[0]['path']}")
    for row in rows:
        anchors = json.loads(row["anchors"])
        print(f"\n## {row['crumb'] or row['title'] or '(no heading)'}" + (" ◀ hit" if row["id"] == chunk_id else ""))
        lines = [line.replace(CELL_SEP, " | ") for line in row["body"].split("\n")]
        if len(anchors) == 1:
            if label := location_label(anchors[0]):
                print(f"[{label}]")
            print("\n".join(lines))
        else:
            previous = None
            for line, anchor in zip(lines, anchors):
                label = location_label(anchor)
                print(f"[{label}] {line}" if label and label != previous else line)
                previous = label
    return 0


def cmd_open(args) -> int:
    from docwhere.opener import open_at

    hit = _last_hit(Index(args.db), args.n)
    print(f"{Path(hit['path']).name}: {open_at(hit['path'], hit['anchor'])}")
    return 0


def cmd_index(args) -> int:
    index = Index(args.db)
    for root in args.roots:
        if args.rebuild:
            index.forget(root)
        started = time.perf_counter()
        s = index.sync(root, progress=_progress(f"indexing {root}"), workers=args.workers)
        print(
            f"{Path(root).resolve()}: {s.files} files, {s.updated} extracted, {s.removed} removed,"
            f" {s.failed} unreadable ({time.perf_counter() - started:.1f}s)"
        )
    return 0


def cmd_status(args) -> int:
    index = Index(args.db)
    roots = index.roots()
    errors = index.errors() if args.errors else []
    if args.json:
        print(json.dumps({"db": str(index.path), "roots": roots, "errors": errors}, ensure_ascii=False, indent=1))
        return 0
    print(f"index: {index.path}")
    for r in roots:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["synced_at"])) if r["synced_at"] else "-"
        print(f"  {r['path']}  {r['files']} files / {r['chunks']} sections / {r['errors']} unreadable  (synced {when})")
    for path, error in errors:
        print(f"  ! {path}: {error}")
    return 0


# ---- helpers ---------------------------------------------------------------------------


def _last_path(index: Index) -> Path:
    return index.path.with_name("last_search.json")


def _last_hit(index: Index, n: int) -> dict:
    path = _last_path(index)
    if not path.exists():
        raise LookupError("no previous search; run docwhere search first")
    hits = json.loads(path.read_text(encoding="utf-8"))["hits"]
    if not 1 <= n <= len(hits):
        raise LookupError(f"hit {n} does not exist (last search had {len(hits)})")
    return hits[n - 1]


def _progress(label: str):
    if not sys.stderr.isatty():
        return None

    def report(done: int, total: int) -> None:
        end = "\n" if done == total else ""
        print(f"\r{label}: {done}/{total}", end=end, file=sys.stderr, flush=True)

    return report


def _highlight(text: str, terms: list[str]) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    low = text.lower()
    marks = [False] * len(text)
    for t in terms:
        t, start = t.lower(), 0
        while t and (i := low.find(t, start)) >= 0:
            marks[i : i + len(t)] = [True] * len(t)
            start = i + 1
    out, on = [], False
    for ch, m in zip(text, marks):
        if m != on:
            out.append("\x1b[1;33m" if m else "\x1b[0m")
            on = m
        out.append(ch)
    return "".join(out) + ("\x1b[0m" if on else "")
