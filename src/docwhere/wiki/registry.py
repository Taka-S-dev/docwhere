"""The wikis the user added (kept in the settings) and the client for each kind."""

from __future__ import annotations

import urllib.parse
from pathlib import Path

from docwhere.settings import load_settings, save_settings
from docwhere.wiki.core import WikiError

KINDS = ("confluence",)


def checked_url(url: str) -> str:
    """The wiki's base URL, without a trailing slash; only plain http(s) addresses."""
    url = url.strip().rstrip("/")
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise WikiError(f"wiki URL must start with http:// or https://: {url}")
    if p.username or p.password or p.query or p.fragment:
        raise WikiError(f"wiki URL must be the address of the wiki itself (no login, query or #): {url}")
    return url


def wikis(db: Path) -> list[dict]:
    items = load_settings(db).get("wikis") or []
    return [w for w in items if isinstance(w, dict) and w.get("kind") in KINDS and w.get("name") and w.get("url")]


def add_wiki(db: Path, name: str, kind: str, url: str, spaces: list[str]) -> dict:
    if kind not in KINDS:
        raise WikiError(f"unknown wiki kind {kind!r}; known: {', '.join(KINDS)}")
    name = name.strip()
    if not name:
        raise WikiError("a wiki needs a name")
    entry = {"name": name, "kind": kind, "url": checked_url(url), "spaces": [s.strip() for s in spaces if s.strip()]}
    save_settings(db, wikis=[w for w in wikis(db) if w["name"] != name] + [entry])
    return entry


def remove_wiki(db: Path, name: str) -> bool:
    kept = [w for w in wikis(db) if w["name"] != name]
    if len(kept) == len(wikis(db)):
        return False
    save_settings(db, wikis=kept)
    return True


def client(entry: dict):
    from docwhere.wiki.confluence import Confluence

    return {"confluence": Confluence}[entry["kind"]](entry["url"], entry.get("spaces") or [])
