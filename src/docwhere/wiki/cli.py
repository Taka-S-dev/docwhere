"""The ``docwhere wiki`` commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from docwhere.index import default_db_path
from docwhere.present import EvidenceStep, highlight, show_page


def register(sub) -> None:
    """Add the ``wiki`` command and its subcommands to the main parser."""
    w = sub.add_parser("wiki", help="search a wiki (Confluence) through its own search; the only commands that use the network")
    ws = w.add_subparsers(dest="wiki_command", required=True)

    p = ws.add_parser("add", help="add or replace a wiki")
    p.add_argument("name", help="what to call it, e.g. team-wiki")
    p.add_argument("url", help="the wiki's address, e.g. http://wiki.example:8090")
    p.add_argument("--kind", default="confluence", help="wiki software (default confluence)")
    p.add_argument("--space", action="append", default=[], help="only search this space, repeatable (default: all)")
    p.set_defaults(func=cmd_wiki_add)

    p = ws.add_parser("list", help="the wikis added")
    p.set_defaults(func=cmd_wiki_list)

    p = ws.add_parser("remove", help="remove a wiki")
    p.add_argument("name")
    p.set_defaults(func=cmd_wiki_remove)

    p = ws.add_parser("spaces", help="the spaces of a wiki that can be viewed without logging in (key and name)")
    p.add_argument("name", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_wiki_spaces)

    p = ws.add_parser("check", help="what works on a wiki: reachable, version, REST API, spaces, a test search")
    p.add_argument("name", nargs="?")
    p.add_argument("--term", help="a word to try searching for")
    p.set_defaults(func=cmd_wiki_check)

    p = ws.add_parser("search", help="search the wikis; prints the best sections of the pages found")
    p.add_argument("terms", nargs="+", help="search terms; a page matching any of them is a hit (see --all)")
    p.add_argument("--all", action="store_true", help="only pages containing every term")
    p.add_argument("--wiki", help="only this wiki (default: all added)")
    p.add_argument("--space", action="append", default=[], help="only this space, repeatable (overrides the wiki's spaces)")
    p.add_argument("-n", "--limit", type=int, default=5, help="pages to read per wiki (default 5, at most 20)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--view", action="store_true", help="also show the hits in the reader (see wiki view)")
    _wiki_view_options(p, "with --view: ")
    p.set_defaults(func=cmd_wiki_search)

    p = ws.add_parser("view", help="show the last wiki search's hits in the reader, with the answer's quotes framed")
    p.add_argument("n", type=int, nargs="*", help="only these hit numbers (default: all)")
    _wiki_view_options(p)
    p.add_argument("--no-open", action="store_true", help="write the page and print its path only")
    p.set_defaults(func=cmd_wiki_view)

    p = ws.add_parser("show", help="print the sections of a page: hit N of the last wiki search, a page id or its address")
    p.add_argument("ref")
    p.add_argument("--wiki", help="with a page id or address: which wiki (default: the only one added)")
    p.add_argument("--term", action="append", default=[], help="mark sections with this term, repeatable")
    p.add_argument("-C", "--context", type=int, default=1, help="neighbouring sections to print around each hit (default 1)")
    p.add_argument("--whole", action="store_true", help="print every section")
    p.set_defaults(func=cmd_wiki_show)

    p = ws.add_parser("open", help="open hit N of the last wiki search (or a page address of an added wiki) in the browser")
    p.add_argument("ref")
    p.set_defaults(func=cmd_wiki_open)


def _wiki_view_options(p: argparse.ArgumentParser, prefix: str = "") -> None:
    p.add_argument("-C", "--context", type=int, default=1, help=f"{prefix}neighbouring sections to include, folded (default 1)")
    p.add_argument("--quote", action=EvidenceStep, default=[], help=f"{prefix}sentence that answers the question; framed, repeatable")
    p.add_argument("--note", action=EvidenceStep, default="", help=f"{prefix}what the --quote before it shows; "
                   "with notes the evidence reads as steps leading to --answer")
    p.set_defaults(steps=[])
    p.add_argument("--answer", default="", help=f"{prefix}one-line answer shown at the top of the page")
    p.add_argument("--title", default="", help=f"{prefix}the question, used as the page title (default: the terms)")
    p.add_argument("--browser", action="store_true", help=f"{prefix}open the saved page in the browser, not the docwhere window")


MAX_WIKI_PAGES = 20
MAX_WIKI_SHOW_CHARS = 40_000
MAX_HIT_NUMBER = 999  # "wiki show 12" is a hit number; longer numbers are page ids


def _wiki_db(args) -> Path:
    return Path(args.db or default_db_path())


def _wiki_entries(db: Path, name: str | None) -> list[dict]:
    from docwhere.wiki import WikiError
    from docwhere.wiki.registry import wikis

    entries = wikis(db)
    if not entries:
        raise WikiError("no wiki added yet; add one with: docwhere wiki add NAME URL")
    if name:
        entries = [w for w in entries if w["name"] == name]
        if not entries:
            raise WikiError(f"no wiki named {name!r}; see: docwhere wiki list")
    return entries


def _wiki_command(fn):
    """Wiki failures (unreachable, refused redirect, login asked) print one line and exit 2."""
    import functools

    @functools.wraps(fn)
    def run(args) -> int:
        from docwhere.wiki import WikiError, clean

        try:
            return fn(args)
        except WikiError as e:
            print(f"docwhere: {clean(e)}", file=sys.stderr)  # messages quote addresses the server gave
            return 2

    return run


@_wiki_command
def cmd_wiki_add(args) -> int:
    from docwhere.wiki.registry import add_wiki

    entry = add_wiki(_wiki_db(args), args.name, args.kind, args.url, args.space)
    print(f"added {entry['name']}: {entry['url']}" + (f" (spaces: {', '.join(entry['spaces'])})" if entry["spaces"] else ""))
    print(f"next: docwhere wiki check {entry['name']} --term WORD")
    return 0


@_wiki_command
def cmd_wiki_list(args) -> int:
    from docwhere.wiki.registry import wikis

    entries = wikis(_wiki_db(args))
    if not entries:
        print("no wiki added; add one with: docwhere wiki add NAME URL")
    for w in entries:
        print(f"{w['name']}  {w['kind']}  {w['url']}" + (f"  spaces: {', '.join(w['spaces'])}" if w.get("spaces") else ""))
    return 0


@_wiki_command
def cmd_wiki_remove(args) -> int:
    from docwhere.wiki import WikiError
    from docwhere.wiki.registry import remove_wiki

    if not remove_wiki(_wiki_db(args), args.name):
        raise WikiError(f"no wiki named {args.name!r}")
    print(f"removed {args.name}")
    return 0


@_wiki_command
def cmd_wiki_spaces(args) -> int:
    from docwhere.wiki.registry import client

    out = {}
    for entry in _wiki_entries(_wiki_db(args), args.name):
        out[entry["name"]] = client(entry).space_list()
    if args.json:
        print(json.dumps({name: [{"key": k, "name": n} for k, n in spaces] for name, spaces in out.items()},
                         ensure_ascii=False, indent=1))
        return 0
    for name, spaces in out.items():
        print(f"# {name}: {len(spaces)} space(s)")
        for key, title in spaces:
            print(f"{key}	{title}")
    return 0


@_wiki_command
def cmd_wiki_check(args) -> int:
    from docwhere.wiki.registry import client

    failed = False
    for entry in _wiki_entries(_wiki_db(args), args.name):
        print(f"# {entry['name']} ({entry['kind']})")
        for ok, line in client(entry).check(args.term):
            print(("ok   " if ok else "--   " if ok is None else "FAIL ") + line)
            failed |= ok is False
    return 1 if failed else 0


@_wiki_command
def cmd_wiki_search(args) -> int:
    from docwhere.wiki.hits import hits
    from docwhere.wiki.registry import client

    db = _wiki_db(args)
    limit = max(1, min(args.limit, MAX_WIKI_PAGES))
    found, pages = [], {}
    for entry in _wiki_entries(db, args.wiki):
        wiki = client(entry)
        if args.space:
            wiki.spaces = args.space
        read = wiki.search(args.terms, args.all, limit)
        pages.update({(entry["name"], page.id): page for page in read})
        found += hits(entry["name"], read, args.terms)
    found.sort(key=lambda h: -h.score)
    for n, h in enumerate(found, 1):
        h.n = n
    db.parent.mkdir(parents=True, exist_ok=True)
    db.with_name("last_wiki.json").write_text(
        json.dumps({"terms": args.terms, "hits": [h.to_dict() for h in found]}, ensure_ascii=False), encoding="utf-8"
    )
    if args.view:
        path = _show_wiki_view(db, args.terms, [h.to_dict() for h in found], pages, args, open_it=True)
        print(f"view: {path}", file=sys.stderr)
    if args.json:
        print(json.dumps({"terms": args.terms, "hits": [h.to_dict() for h in found],
                          "next": "docwhere wiki show N prints the hit's sections; docwhere wiki open N opens the page"},
                         ensure_ascii=False, indent=1))
        return 0
    if not found:
        print("no hits")
    for h in found:
        where = "  ".join(x for x in (h.crumb, f"[{h.wiki}{' ' + h.space if h.space else ''}]", h.updated[:10]) if x)
        print(f"{h.n:>2}. {where}")
        print(f"    {highlight(h.snippet, h.terms)}")
        print(f"    {h.url}")
    return 0


def _show_wiki_view(db: Path, terms: list[str], found: list[dict], pages: dict, options, open_it: bool) -> str:
    from docwhere.wiki.view import build_wiki_html

    return show_page(
        db,
        lambda in_window: build_wiki_html(found, pages, terms, options.context, options.quote, options.answer, options.title,
                                          steps=getattr(options, "steps", None)),
        options.title or " ".join(terms), options, open_it,
    )


@_wiki_command
def cmd_wiki_view(args) -> int:
    from docwhere.wiki import WikiError
    from docwhere.wiki.registry import client

    db = _wiki_db(args)
    try:
        last = json.loads(db.with_name("last_wiki.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise WikiError("no wiki search yet; run: docwhere wiki search TERMS") from None
    found = [h for h in last.get("hits", []) if not args.n or h.get("n") in args.n]
    pages = {}
    for h in found:  # each page once, from the wiki as it is now
        key = (h["wiki"], h["page_id"])
        if key not in pages:
            pages[key] = client(_wiki_entries(db, h["wiki"])[0]).page(h["page_id"] or h["url"])
    print(_show_wiki_view(db, last.get("terms", []), found, pages, args, open_it=not args.no_open))
    return 0


def _last_wiki_hit(db: Path, n: int) -> dict:
    from docwhere.wiki import WikiError

    try:
        last = json.loads(db.with_name("last_wiki.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise WikiError("no wiki search yet; run: docwhere wiki search TERMS") from None
    hit = next((h for h in last.get("hits", []) if h.get("n") == n), None)
    if hit is None:
        raise WikiError(f"no hit {n} in the last wiki search")
    return hit


@_wiki_command
def cmd_wiki_show(args) -> int:
    from docwhere.extractors.html import html_chunks
    from docwhere.model import norm
    from docwhere.wiki import WikiError
    from docwhere.wiki.registry import client

    db = _wiki_db(args)
    if args.ref.isdigit() and int(args.ref) <= MAX_HIT_NUMBER and not args.wiki:
        hit = _last_wiki_hit(db, int(args.ref))
        entry, ref, terms = _wiki_entries(db, hit["wiki"])[0], hit["page_id"] or hit["url"], args.term or hit["terms"]
    else:
        entries = _wiki_entries(db, args.wiki)
        if len(entries) > 1:
            raise WikiError("several wikis are added; say which with --wiki NAME")
        entry, ref, terms = entries[0], args.ref, args.term
    page = client(entry).page(ref)
    chunks = html_chunks(page.html, page.title, page.root)
    folded = [t.lower() for t in map(norm, terms) if t]
    marked = [i for i, c in enumerate(chunks) if folded and any(t in "\n".join(c.lines).lower() for t in folded)]
    if args.whole or not marked:
        shown = list(range(len(chunks)))
    else:
        shown = sorted({j for i in marked for j in range(i - args.context, i + args.context + 1) if 0 <= j < len(chunks)})
    print(f"# {page.title}\n# {page.url}" + (f"\n# updated {page.updated[:10]}" if page.updated else ""))
    size, previous = 0, None
    for i in shown:
        if previous is not None and i != previous + 1:
            print("\n…")
        previous = i
        text = "\n".join(chunks[i].lines)
        if size + len(text) > MAX_WIKI_SHOW_CHARS:
            print(f"\n… (stopped at {MAX_WIKI_SHOW_CHARS} characters; the rest is on the page)")
            break
        size += len(text)
        fragment = next((a["fragment"] for a in chunks[i].anchors if a.get("fragment")), "")
        print(f"\n## {chunks[i].crumb or chunks[i].title or '(no heading)'}"
              + (" ◀ hit" if i in marked else "") + (f"  #{fragment}" if fragment else ""))
        print(text)
    return 0


@_wiki_command
def cmd_wiki_open(args) -> int:
    import webbrowser

    from docwhere.wiki import WikiError, origin
    from docwhere.wiki.registry import wikis

    db = _wiki_db(args)
    if args.ref.startswith(("http://", "https://")):
        url = args.ref
    elif args.ref.isdigit():
        url = _last_wiki_hit(db, int(args.ref))["url"]
    else:
        raise WikiError("give a hit number from the last wiki search or a page address")
    if origin(url) not in {origin(w["url"]) for w in wikis(db)}:
        raise WikiError(f"not a page of an added wiki: {url}")
    webbrowser.open(url)
    print(f"opened {url}")
    return 0
