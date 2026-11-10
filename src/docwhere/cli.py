"""Command line: search, show, open, view, snap, viewer, clean, index, status."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from docwhere.index import Index, default_db_path

HINT = "docwhere show --id ID prints the whole section; docwhere open N opens the hit"
STALE_DAYS = 90  # status flags folders not searched for this long
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
    p.add_argument("--view", action="store_true", help="also open the hits as an HTML page (see view)")
    _view_options(p, "with --view: ")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("show", help="print the full section of hit N from the last search")
    p.add_argument("n", type=int, nargs="?")
    p.add_argument("--id", type=int, help="section id from search --json (safe when several searches run at once)")
    p.add_argument("-C", "--context", type=int, default=0, help="also print this many neighbouring chunks")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("open", help="open hit N from the last search at its location")
    p.add_argument("n", type=int)
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("view", help="open the last search's hits as one HTML page: excerpts, and PDF pages as images")
    p.add_argument("n", type=int, nargs="*", help="only these hit numbers (default: all)")
    p.add_argument("-C", "--context", type=int, default=1, help="neighbouring sections to include, collapsed (default 1)")
    _view_options(p)
    p.add_argument("--no-open", action="store_true", help="write the page and print its path only")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("snap", help="save the PDF page of a hit as a PNG with the terms highlighted")
    p.add_argument("n", type=int, nargs="?", help="hit number from the last search")
    p.add_argument("--id", type=int, help="section id from search --json; with --term to highlight")
    p.add_argument("--term", action="append", default=[], help="term to highlight, repeatable (with --id)")
    p.add_argument("-P", "--pages", type=int, default=0, help="also save this many pages either side")
    p.add_argument("--quote", action="append", default=[], help="answer sentence to frame on the page, repeatable")
    p.set_defaults(func=cmd_snap)

    p = sub.add_parser("viewer", help="where view pages open (window, the default, or browser), the window's title, the PDF app")
    p.add_argument("where", choices=["window", "browser", "status", "title", "pdf"])
    p.add_argument("value", nargs=argparse.REMAINDER, help="title: the window's name (default Reference). "
                   "pdf: the program and its arguments, {page} and {file} filled in "
                   r'(e.g. "D:\Tools\SumatraPDF.exe" -reuse-instance -page {page} {file}), or auto')
    p.set_defaults(func=cmd_viewer)

    p = sub.add_parser("window", help=argparse.SUPPRESS)  # the window process itself (started by view)
    p.add_argument("page", type=Path)
    p.set_defaults(func=cmd_window)

    p = sub.add_parser("clean", help="delete every saved view page, snapshot and Word/PowerPoint PDF export (all are also pruned automatically)")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("index", help="build or update the index for folders")
    p.add_argument("roots", type=Path, nargs="+")
    p.add_argument("--rebuild", action="store_true", help="drop and re-extract everything under the folders")
    p.add_argument("--workers", type=int, default=None)
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("forget", help="delete the index of folders (it is rebuilt by the next search there)")
    p.add_argument("roots", type=Path, nargs="+")
    p.set_defaults(func=cmd_forget)

    p = sub.add_parser("status", help="indexed folders and files that could not be read")
    p.add_argument("--errors", action="store_true", help="list unreadable files")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)
    return parser


def _view_options(p: argparse.ArgumentParser, prefix: str = "") -> None:
    p.add_argument("-P", "--pages", type=int, default=0, help=f"{prefix}PDF pages either side of each hit page (default 0)")
    p.add_argument("--quote", action="append", default=[], help=f"{prefix}sentence that answers the question; marked above the terms, repeatable")
    p.add_argument("--answer", default="", help=f"{prefix}one-line answer shown at the top of the page")
    p.add_argument("--title", default="", help=f"{prefix}the question, used as the page and tab title (default: the terms)")
    p.add_argument("--browser", action="store_true", help=f"{prefix}open the saved page in the browser, not the docwhere window")


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
    if args.view:
        path = _write_view(index, args.terms, [h.to_dict() for h in hits], 1, args, open_it=True)
        print(f"view: {path}", file=sys.stderr)
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
    from docwhere.window import pdf_app

    index = Index(args.db)
    hit = _last_hit(index, args.n)
    print(f"{Path(hit['path']).name}: {open_at(hit['path'], hit['anchor'], pdf_app(index.path))}")
    return 0


def cmd_view(args) -> int:
    index = Index(args.db)
    last = _last_search(index)
    hits = [_last_hit(index, n) for n in args.n] if args.n else last["hits"]
    print(_write_view(index, last["terms"], hits, args.context, args, open_it=not args.no_open))
    return 0


def cmd_snap(args) -> int:
    from docwhere.viewer import SNAP_DPI, SNAP_RETENTION, page_range, prune, render_page

    index = Index(args.db)
    if args.id is not None:
        rows = index.chunk_with_neighbors(args.id)
        if not rows:
            raise LookupError(f"no section with id {args.id}; search again")
        path, anchors, terms = rows[0]["path"], json.loads(rows[0]["anchors"]), args.term
        anchor = anchors[0] if anchors else {}
    elif args.n is not None:
        hit = _last_hit(index, args.n)
        path, anchor, terms = hit["path"], hit["anchor"], hit["terms"]
    else:
        raise LookupError("give a hit number N or --id")
    if not path.lower().endswith(".pdf") or "page" not in anchor:
        raise LookupError("page images are only available for PDF hits; use docwhere show")
    folder = index.path.with_name("snap")
    folder.mkdir(parents=True, exist_ok=True)
    for page in page_range(anchor["page"], args.pages):
        try:
            data = render_page(path, page, terms, SNAP_DPI, quotes=args.quote)
        except LookupError:
            continue  # past the last page
        out = folder / f"{Path(path).stem}_p{page}.png"
        out.write_bytes(data)
        print(out)
    prune(folder, "*.png", **SNAP_RETENTION)
    return 0


def cmd_viewer(args) -> int:
    from docwhere import window

    index = Index(args.db)
    if args.where == "title":
        if args.value:
            window.set_title(index.path, " ".join(args.value))
        print(f"window title: {window.title(index.path)}")
        return 0
    if args.where == "pdf":
        if args.value:
            window.set_pdf_app(index.path, None if args.value == ["auto"] else args.value)
        command = window.pdf_app(index.path).get("command")
        print("PDF app: " + (subprocess.list2cmdline(command) if command else "auto (Acrobat if installed, else the default PDF app)"))
        return 0
    if args.where != "status":
        window.set_mode(index.path, args.where)
    where = window.mode(index.path)
    note = "" if where == "browser" or window.available() else " (pywebview is not installed: pages will open in the browser)"
    print(f"view pages open in the {'docwhere window' if where == 'window' else 'browser'}{note}")
    return 0


def cmd_window(args) -> int:
    from docwhere import window

    index = Index(args.db)
    path = index.path
    index.close()
    window.run(path, args.page)
    return 0


def cmd_clean(args) -> int:
    from docwhere.viewer import VIEW_PATTERN, prune

    everything = {"min_keep": 0, "max_keep": 0, "max_age": 0, "max_bytes": 0}
    index = Index(args.db)
    views, snaps, renditions = (index.path.with_name(n) for n in ("views", "snap", "renditions"))
    targets = ((views, VIEW_PATTERN), (views, "*.sig"), (snaps, "*.png"), (renditions, "*.pdf"), (renditions / "excerpts", "*.pdf"))
    freed = sum(prune(folder, pattern, **everything) for folder, pattern in targets)
    for leftover in (views / "index.html", views / "history.json"):
        leftover.unlink(missing_ok=True)
    print(f"removed saved views, snapshots and PDF exports ({freed / 1024 / 1024:.1f} MB)")
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
    stale = []
    for r in roots:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["synced_at"])) if r["synced_at"] else "-"
        note = ""
        if not Path(r["path"]).is_dir():
            note = "  ! folder no longer exists"
        elif r["synced_at"] and time.time() - r["synced_at"] > STALE_DAYS * 86400:
            note = f"  ! not searched for over {STALE_DAYS} days"
        if note:
            stale.append(r["path"])
        print(f"  {r['path']}  {r['files']} files / {r['chunks']} sections / {r['errors']} unreadable  (synced {when}){note}")
    for path, error in errors:
        print(f"  ! {path}: {error}")
    if stale:
        # never removed automatically: the next search there would have to rebuild it
        print("\nto free their space: docwhere forget " + " ".join(f'"{p}"' for p in stale))
    return 0


def cmd_forget(args) -> int:
    index = Index(args.db)
    known = {r["path"] for r in index.roots()}
    for root in args.roots:
        path = str(Path(root).resolve())
        if path not in known:
            print(f"{path}: not indexed")
            continue
        index.forget(Path(path))
        print(f"{path}: index removed")
    index.db.execute("VACUUM")  # give the space back to the disk; deleting rows alone does not
    return 0


# ---- helpers ---------------------------------------------------------------------------


def _last_path(index: Index) -> Path:
    return index.path.with_name("last_search.json")


def _last_search(index: Index) -> dict:
    path = _last_path(index)
    if not path.exists():
        raise LookupError("no previous search; run docwhere search first")
    return json.loads(path.read_text(encoding="utf-8"))


def _last_hit(index: Index, n: int) -> dict:
    hits = _last_search(index)["hits"]
    if not 1 <= n <= len(hits):
        raise LookupError(f"hit {n} does not exist (last search had {len(hits)})")
    return hits[n - 1]


def _write_view(index: Index, terms: list[str], hits: list[dict], context: int, options, open_it: bool) -> str:
    """Save the page and open it: in the docwhere window (an interactive reader) unless the
    browser was chosen or the window cannot start. Returns the saved page's path.
    ``options``: the parsed --pages, --quote, --answer, --title and --browser."""
    from docwhere import window
    from docwhere.opener import start_file
    from docwhere.viewer import build_html, save_view

    in_window = open_it and not options.browser and window.mode(index.path) == "window" and window.available()
    page = build_html(index, terms, hits, context, options.pages, options.quote, options.answer, options.title,
                      cut_excerpts=not in_window)
    def sign(path: Path) -> None:
        try:
            window.sign_page(index.path, path)
        except OSError as e:  # e.g. a key file that is not this user's
            print(f"docwhere: the page was not signed, so the window will not run its reader: {e}", file=sys.stderr)

    out = save_view(index.path.with_name("views"), page, options.title or " ".join(terms), options.answer, sign=sign)
    if not open_it:
        return str(out)
    if not options.browser and window.mode(index.path) == "window":
        if window.available() and window.show(index.path, out):
            return str(out)
        print("docwhere: the docwhere window could not start (or the index folder is shared); opening the page in the browser", file=sys.stderr)
    start_file(str(out))
    return str(out)


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
