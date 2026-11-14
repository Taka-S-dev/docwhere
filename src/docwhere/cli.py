"""Command line: search, show, open, view, snap, viewer, clean, index, exclude, forget, status, wiki."""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from docwhere import background, doctor
from docwhere.extractors import is_serial
from docwhere.index import (
    FILE_TIMEOUT,
    Index,
    SyncProgress,
    SyncStats,
    default_db_path,
    path_key,
)
from docwhere.present import EvidenceStep, highlight, show_page
from docwhere.progress import duration
from docwhere.wiki import cli as wiki_cli

HINT = "docwhere show --id ID prints the whole section; docwhere open N opens the hit"
STALE_DAYS = 90  # status flags folders not searched for this long
MAX_UNREADABLE_LISTED = 20
MAX_CHANGED_LISTED = 10
# A search waits this long for new or changed files to be read, then answers from what is read
# and leaves the rest to the background reader, saying how much it has not seen yet.
WAIT = 20
# Rough costs, used only to tell whether a search can read the files itself within WAIT.
READ_STARTUP = 3  # starting the reader processes
SECONDS_PER_FILE = 0.3
SECONDS_PER_CONVERSION = 1.5  # a legacy .doc/.xls/.ppt, converted through Office


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
    p.add_argument("--keep-changed", action="store_true",
                   help="search files modified since they were read as last read (new files are still read); "
                        "the JSON's changed says how many")
    p.add_argument("--wait", type=float, default=WAIT, metavar="SEC",
                   help=f"seconds to wait for new files to be read before answering from what is read (default {WAIT})")
    p.add_argument("--wait-all", action="store_true", help="wait until every file is read, however long it takes")
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

    p = sub.add_parser("reader", help=argparse.SUPPRESS)  # the background reader (started by search)
    p.set_defaults(func=cmd_reader)

    p = sub.add_parser("window", help=argparse.SUPPRESS)  # the window process itself (started by view)
    p.add_argument("page", type=Path)
    p.set_defaults(func=cmd_window)

    p = sub.add_parser("manage", help="open the index window: each folder's state and changes, the reading in "
                       "progress, and updating, stopping, adding folders and exclusions")
    p.add_argument("--update", nargs="*", type=Path, metavar="FOLDER",
                   help="first hand these folders (default: every indexed folder) to the background reading, which "
                        "reads what is new or changed; a folder not indexed yet is added. The window shows the progress")
    p.set_defaults(func=cmd_manage)

    p = sub.add_parser("manage-window", help=argparse.SUPPRESS)  # the index window process (started by manage)
    p.set_defaults(func=cmd_manage_window)

    p = sub.add_parser("clean", help="delete every saved view page, snapshot and Word/PowerPoint PDF export (all are also pruned automatically)")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("index", help="build or update the index for folders")
    p.add_argument("roots", type=Path, nargs="*")
    p.add_argument("--rebuild", action="store_true", help="drop and re-extract everything under the folders")
    p.add_argument("--stop", action="store_true", help="stop the background reading started by searches")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--timeout", type=float, default=FILE_TIMEOUT,
                   help=f"seconds one file may take before it is given up as unreadable (default {FILE_TIMEOUT})")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("exclude", help="keep folders or files out of a folder's index and searches; "
                       "with no pattern, list what is excluded there")
    p.add_argument("root", type=Path, help="the folder the patterns apply below, whichever folder is searched")
    p.add_argument("patterns", nargs="*", help="a name (old, *_bak*, *.log) matches a folder or file at any depth; "
                   "a path (specs/archive) is from ROOT. Case is ignored")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--remove", action="store_true", help="stop excluding PATTERNS; the next search or index reads their files")
    g.add_argument("--clear", action="store_true", help="stop excluding everything set on ROOT")
    p.set_defaults(func=cmd_exclude)

    p = sub.add_parser("forget", help="delete the index of folders and their exclusions (rebuilt by the next search there)")
    p.add_argument("roots", type=Path, nargs="+")
    p.set_defaults(func=cmd_forget)

    wiki_cli.register(sub)
    doctor.register(sub)

    p = sub.add_parser("status", help="indexed folders and files that could not be read")
    p.add_argument("--errors", action="store_true", help="list unreadable files")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)
    return parser


def _view_options(p: argparse.ArgumentParser, prefix: str = "") -> None:
    p.add_argument("-P", "--pages", type=int, default=0, help=f"{prefix}PDF pages either side of each hit page (default 0)")
    p.add_argument("--quote", action=EvidenceStep, default=[], help=f"{prefix}sentence that answers the question; marked above the terms, repeatable")
    p.add_argument("--figure", action=EvidenceStep, default=[], help=f"{prefix}a figure that answers it, ringed: its caption "
                   "(図3 構成) or a label in it, or p.12#2 for the second figure on page 12; repeatable")
    p.add_argument("--note", action=EvidenceStep, default="", help=f"{prefix}what the --quote or --figure before it shows; "
                   "with notes the evidence reads as steps leading to --answer")
    p.set_defaults(steps=[])
    p.add_argument("--answer", default="", help=f"{prefix}one-line answer shown at the top of the page")
    p.add_argument("--title", default="", help=f"{prefix}the question, used as the page and tab title (default: the terms)")
    p.add_argument("--browser", action="store_true", help=f"{prefix}open the saved page in the browser, not the docwhere window")


# ---- commands --------------------------------------------------------------------------


def cmd_search(args) -> int:
    from docwhere.search import search

    roots = args.roots or [Path(".")]
    index = Index(args.db)
    updates: list[SyncStats] = []
    reading = {"complete": None}  # not checked with --no-update
    changed: list[tuple] = []  # files modified since they were read: read again, or kept as read
    if not args.no_update:
        waiting = []
        for root in roots:
            scan = index.scan(root)
            _note_excluded(root, scan.stats)
            updates.append(scan.stats)
            changed += scan.changed
            if args.keep_changed and scan.changed:
                skip = {t[0] for t in scan.changed}
                scan = dataclasses.replace(scan, todo=[t for t in scan.todo if t[0] not in skip])
            if not scan.todo:
                continue
            # Little to read: read it here, as before, even while the background reader works on
            # another folder (queued behind it, a few changed files would wait for the whole folder).
            if _estimate(scan.todo) <= args.wait:
                _extract(index, scan)
            else:
                waiting.append(scan)
        reading = _reading(index, updates, _wait_for_reader(index, waiting, None if args.wait_all else args.wait))
    exts = [e.strip() for e in args.ext.split(",")] if args.ext else None
    hits = search(index, roots, args.terms, require_all=args.all, exts=exts, limit=args.limit, per_file=args.per_file)
    _last_path(index).write_text(
        json.dumps({"terms": args.terms, "hits": [h.to_dict() for h in hits], "reading": reading}, ensure_ascii=False),
        encoding="utf-8",
    )

    # Every unreadable file in scope, not only the ones that failed during this sync: a file
    # that failed last week is just as invisible to this search.
    unreadable = index.errors(roots)
    # What the exclusion patterns kept out of this search, so a miss caused by one is noticed.
    excludes = _left_out(index, roots, updates)
    kept = {t[0] for t in changed} if args.keep_changed else set()
    if args.view:
        path = _write_view(index, args.terms, [h.to_dict() for h in hits], 1, args, open_it=True, reading=reading)
        print(f"view: {path}", file=sys.stderr)
    if args.json:
        out = {
            "terms": args.terms,
            "roots": [str(Path(r).resolve()) for r in roots],
            "reading": reading,
            # None: not checked (--no-update). kept: searched as last read (--keep-changed)
            "changed": None if args.no_update else {
                "count": len(changed),
                "kept": bool(kept),
                "files": [str(t[1]) for t in changed[:MAX_CHANGED_LISTED]],
            },
            "index": {k: sum(getattr(u, k) for u in updates) for k in ("files", "updated", "removed")},
            "timed_out": [p for u in updates for p in u.timed_out],
            "excludes": excludes,
            "excluded_roots": [
                {"root": str(Path(r).resolve()), "set_on": u.excluded_by[0], "pattern": u.excluded_by[1]}
                for r, u in zip(roots, updates) if u.excluded_by
            ],
            "hits": [
                {"id" if k == "chunk_id" else k: v for k, v in h.to_dict().items() if k != "anchor"}
                | ({"changed_since_read": True} if path_key(h.path) in kept else {})
                for h in hits
            ],
            "unreadable": [{"path": p, "error": e} for p, e in unreadable[:MAX_UNREADABLE_LISTED]],
            "unreadable_count": len(unreadable),
            # wikis added on this PC: a session that did not add them learns here that they exist
            "wikis": _wiki_names(index.path),
            "next": HINT,
        }
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    if not hits:
        print("no hits")
    for h in hits:
        where = "  ".join(x for x in (h.crumb or h.title, h.location) if x)
        print(f"{h.n:>2}. {Path(h.path).name}  {where}  ({h.modified})")
        print(f"    {highlight(h.snippet, h.terms)}")
        print(f"    {h.path}")
        if h.newer_version:
            print(f"    ! newer version: {Path(h.newer_version).name}")
        if path_key(h.path) in kept:
            print("    ! modified since it was read; this is the earlier content")
    if unreadable:
        print(f"\n{len(unreadable)} file(s) here could not be searched; see: docwhere status --errors", file=sys.stderr)
    if any(e["left_out"] for e in excludes):
        listed = ", ".join(f"{e['pattern']} ({e['left_out']})" for e in excludes if e["left_out"])
        print(f"left out by exclusions, not searched: {listed}; see: docwhere status", file=sys.stderr)
    if kept:
        print(f"{len(kept)} file(s) modified since they were read were searched as last read;"
              " search without --keep-changed to read them again", file=sys.stderr)
    if reading["complete"] is False:
        print("\n" + _not_finished(reading), file=sys.stderr)
    if names := _wiki_names(index.path):
        print(f"also added: wiki {', '.join(names)}; search it with: docwhere wiki search", file=sys.stderr)
    return 0


def _wiki_names(db: Path) -> list[str]:
    from docwhere.wiki.registry import wikis

    return [w["name"] for w in wikis(db)]


def _estimate(todo: list[tuple]) -> float:
    """Rough seconds to read ``todo``: enough to tell a few files from a folder's worth."""
    office = sum(is_serial(t[1]) for t in todo)
    return READ_STARTUP + office * SECONDS_PER_CONVERSION + (len(todo) - office) * SECONDS_PER_FILE


def _wait_for_reader(index: Index, scans: list, limit: float | None) -> list[tuple]:
    """Hand ``scans`` to the background reader and wait up to ``limit`` seconds (None: until
    they are read). Returns the entries still not read."""
    from docwhere.progress import Reporter

    if not scans:
        return []
    for scan in scans:
        background.request(index, scan.root)
    total = sum(len(s.todo) for s in scans)
    report = Reporter("reading in the background")
    deadline = None if limit is None else time.monotonic() + limit
    while True:
        left = [t for s in scans for t in index.pending(s.todo)]
        report(SyncProgress(total - len(left), total, []))
        if not left or (deadline is not None and time.monotonic() >= deadline):
            return left
        if background.state(index) is None:  # the reader died (or was stopped): start another
            for scan in scans:
                if index.pending(scan.todo):
                    background.request(index, scan.root)
        time.sleep(1)


def _reading(index: Index, updates: list[SyncStats], left: list[tuple]) -> dict:
    """How much of the searched folders this search saw. ``complete`` is false while files are
    still being read, so a missing answer may be in a file not read yet."""
    files = sum(u.files for u in updates)
    out = {"complete": not left, "files": files, "read": files - len(left), "remaining": len(left)}
    if left:
        state = background.state(index)
        estimate = state.seconds_left() if state else None
        out["remaining_types"] = dict(Counter(Path(t[1]).suffix.lower() for t in left).most_common())
        # the reader's own estimate once it has one (see docwhere.eta); until then a guess by file kind
        out["about_seconds_left"] = round(estimate if estimate is not None else _estimate(left))
        out["background"] = state is not None
    return out


def _not_finished(reading: dict) -> str:
    kinds = ", ".join(list(reading["remaining_types"])[:3])
    return (
        f"not finished reading: this searched {reading['read']} of {reading['files']} files; {reading['remaining']} are"
        f" still being read in the background (mostly {kinds}), about {duration(reading['about_seconds_left'])} left.\n"
        "to search every file: search again once reading has finished (docwhere status shows how far it got),"
        " or add --wait-all to wait for it"
    )


def _extract(index: Index, scan) -> SyncStats:
    from docwhere.progress import Reporter

    stats = index.extract(scan, progress=Reporter(f"indexing {scan.root}"))
    for path in stats.timed_out:
        print(f"gave up on (took too long, listed as unreadable): {path}", file=sys.stderr)
    return stats


def _left_out(index: Index, roots: list[Path], updates: list[SyncStats]) -> list[dict]:
    """Each pattern that applies to the searched folders, with the folders and files it left out
    in this search's sync (None when the search skipped the sync)."""
    if not updates:
        return [{**e, "left_out": None} for r in roots for e in index.excludes_applying(r)]
    totals: dict[tuple[str, str], int] = {}
    for u in updates:
        for rule, n in u.left_out.items():
            totals[rule] = totals.get(rule, 0) + n
    return [{"pattern": p, "set_on": folder, "left_out": n} for (folder, p), n in totals.items()]


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
    print(_write_view(index, last["terms"], hits, args.context, args, open_it=not args.no_open, reading=last.get("reading")))
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


def cmd_reader(args) -> int:
    return background.serve(Index(args.db))


def cmd_window(args) -> int:
    from docwhere import window

    index = Index(args.db)
    path = index.path
    index.close()
    window.run(path, args.page)
    return 0


def cmd_manage(args) -> int:
    from docwhere import manage

    index = Index(args.db)
    path = index.path
    queued: list[Path] = []
    if args.update is not None:
        # handed over before the window opens, so the reading goes on even where no window can
        folders = [Path(f).resolve() for f in args.update] or [Path(r["path"]) for r in index.roots()]
        for folder in folders:
            if not folder.is_dir():
                raise NotADirectoryError(f"not a folder: {folder}")
        for folder in folders:
            background.request(index, folder)
        queued = folders
    index.close()
    if queued:
        print(f"updating in the background: {', '.join(str(f) for f in queued)}"
              " (searches meanwhile answer from what is read; docwhere status shows how far it got)")
    if not manage.open_manager(path):
        print("docwhere: the index window cannot open here (pywebview or WebView2 missing, or the index folder"
              " can be written by others); use docwhere status to follow the reading", file=sys.stderr)
        return 0 if queued else 1
    print("opened the index window")
    return 0


def cmd_manage_window(args) -> int:
    from docwhere import manage

    index = Index(args.db)
    path = index.path
    index.close()
    manage.run(path)
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
    if args.stop:
        print("background reading stopped (what it read is kept)" if background.stop(index)
              else "no background reading was running")
        return 0
    if not args.roots:
        print("docwhere: give the folders to index", file=sys.stderr)
        return 2
    for root in args.roots:
        if args.rebuild:
            index.forget(root, keep_excludes=True)
        started = time.perf_counter()
        s = _sync(index, root, workers=args.workers, timeout=args.timeout)
        print(
            f"{Path(root).resolve()}: {s.files} files, {s.updated - s.unchanged} extracted,"
            + (f" {s.unchanged} only re-dated (content unchanged, not read again)," if s.unchanged else "")
            + f" {s.removed} removed,"
            f" {s.failed} unreadable ({time.perf_counter() - started:.1f}s)"
        )
    return 0


def cmd_status(args) -> int:
    index = Index(args.db)
    roots = index.roots()
    errors = index.errors() if args.errors else []
    reader = background.state(index)
    reading = None if reader is None else {
        "folder": reader.current, "read": reader.done, "files": reader.total, "queued": reader.queued,
        "about_seconds_left": None if reader.seconds_left() is None else round(reader.seconds_left()),
    }
    times = index.read_times()
    if args.json:
        print(json.dumps({"db": str(index.path), "roots": roots, "errors": errors, "background": reading,
                          "read_times": times}, ensure_ascii=False, indent=1))
        return 0
    print(f"index: {index.path}")
    if reader is not None:
        where = f"{reader.current}: {reader.done}/{reader.total} files" if reader.current else "starting"
        if reader.current and reader.seconds_left() is not None and reader.done < reader.total:
            where += f", about {duration(reader.seconds_left())} left"
        print(f"reading in the background: {where}" + (f", then {len(reader.queued)} more folder(s)" if reader.queued else "")
              + " (docwhere index --stop to stop it)")
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
        if r["excludes"]:
            print("    excluding: " + ", ".join(_described(e) for e in r["excludes"]))
    for path, error in errors:
        print(f"  ! {path}: {error}")
    if times:
        # where indexing spends its time on this PC, the slowest kind first
        print("reading time by kind: " + ", ".join(
            f"{ext or '(none)'} {t['files']} files {duration(t['seconds'])} (avg {t['average']:.2f}s)"
            for ext, t in list(times.items())[:6]
        ))
    if stale:
        # never removed automatically: the next search there would have to rebuild it
        print("\nto free their space: docwhere forget " + " ".join(f'"{p}"' for p in stale))
    return 0


def cmd_exclude(args) -> int:
    index = Index(args.db)
    root = Path(args.root).resolve()
    status = 0
    try:
        if args.remove or args.clear:
            if args.remove and not args.patterns:
                print("docwhere: --remove needs the patterns to stop excluding", file=sys.stderr)
                return 2
            removed, unknown = index.remove_excludes(root, None if args.clear else args.patterns)
            for p in unknown:
                print(f"{root}: was not excluding {p}", file=sys.stderr)
                status = 1
            if removed:
                print(f"{root}: no longer excluding {', '.join(removed)}; the next search or index reads their files")
        elif args.patterns:
            added, dropped = index.add_excludes(root, args.patterns)
            if added:
                print(f"{root}: now excluding {', '.join(added)} ({dropped} indexed files removed)")
    except ValueError as e:
        print(f"docwhere: {e}", file=sys.stderr)
        return 2
    rows = {r["path"]: r for r in index.roots()}
    own = rows[str(root)]["excludes"] if str(root) in rows else []
    print(f"{root}: excluding " + ", ".join(_described(e) for e in own) if own else f"{root}: nothing excluded here")
    for e in index.excludes_applying(root):
        if e["set_on"] != str(root):
            print(f"  also applies here: {e['pattern']} (set on {e['set_on']})")
    return status


def cmd_forget(args) -> int:
    index = Index(args.db)
    known = {r["path"] for r in index.roots()}
    for root in args.roots:
        path = str(Path(root).resolve())
        if path not in known:
            print(f"{path}: not indexed")
            continue
        excludes = index.excludes(Path(path))
        index.forget(Path(path))
        print(f"{path}: index removed" + (f", and no longer excluding {', '.join(excludes)}" if excludes else ""))
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


def _write_view(
    index: Index, terms: list[str], hits: list[dict], context: int, options, open_it: bool, reading: dict | None = None
) -> str:
    """Save the page and open it: in the docwhere window (an interactive reader) unless the
    browser was chosen or the window cannot start. Returns the saved page's path.
    ``options``: the parsed --pages, --quote, --answer, --title and --browser. ``reading``: how
    much of the folders the search saw, so the page says when it was not everything."""
    from docwhere.viewer import build_html

    return show_page(
        index.path,
        lambda in_window: build_html(index, terms, hits, context, options.pages, options.quote, options.answer,
                                     options.title, cut_excerpts=not in_window, reading=reading,
                                     steps=getattr(options, "steps", None)),
        options.title or " ".join(terms), options, open_it,
    )


def _sync(index: Index, root: Path, **options) -> SyncStats:
    from docwhere.progress import Reporter

    stats = index.sync(root, progress=Reporter(f"indexing {root}"), **options)
    for path in stats.timed_out:
        print(f"gave up on (took too long, listed as unreadable): {path}", file=sys.stderr)
    _note_excluded(root, stats)
    return stats


def _note_excluded(root: Path, stats: SyncStats) -> None:
    if stats.excluded_by:
        folder, pattern = stats.excluded_by
        print(f"{Path(root).resolve()} is excluded by {pattern} set on {folder}, so nothing in it is searched"
              f' (to include it: docwhere exclude "{folder}" --remove "{pattern}")', file=sys.stderr)


def _described(exclude: dict) -> str:
    matched = exclude["matched"]
    return f"{exclude['pattern']} ({'not counted until this folder is indexed' if matched is None else f'{matched} left out'})"
