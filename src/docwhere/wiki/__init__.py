"""Search a wiki through its own search, and read only the pages it returns.

Nothing is mirrored: each question asks the wiki's search, fetches the few pages it returns,
and cuts them into sections the way local files are, so permissions and freshness stay the
wiki's. These commands are docwhere's only network access, and they talk only to the wiki
URLs the user has added; a redirect elsewhere is refused.

A wiki kind (``confluence``) is a class with ``search``, ``page`` and ``check``; add a
module and an entry in ``KINDS`` to support another.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

TIMEOUT = 20  # seconds per request
REACH_TIMEOUT = 3  # seconds to open a connection at all: off the wiki's network, fail fast
MAX_RESPONSE = 16 << 20  # bytes read from one response
KINDS = ("confluence",)


class WikiError(Exception):
    """A wiki could not be reached or answered something unexpected; the message says which."""


@dataclass
class Page:
    id: str
    title: str
    url: str
    space: str = ""
    updated: str = ""
    html: str = ""
    root: str | None = None  # id of the element holding the body, when html is a whole page

    def __post_init__(self) -> None:
        # everything here comes from the server and is printed to the terminal: no escape
        # sequences or bidi overrides may survive (the body is cleaned when it is cut up)
        self.id, self.title, self.space, self.updated = (clean(x) for x in (self.id, self.title, self.space, self.updated))
        self.url = safe_url(self.url)


def clean(text) -> str:
    from docwhere.model import norm

    return norm(str(text or ""))


def safe_url(url: str) -> str:
    """``url`` with anything but printable URL characters percent-encoded."""
    return urllib.parse.quote(str(url or ""), safe="/:?#[]@!$&'()*+,;=%-._~")


@dataclass
class WikiHit:
    n: int
    wiki: str
    page_id: str
    title: str
    crumb: str
    space: str
    url: str
    snippet: str
    terms: list[str]
    updated: str
    score: float
    anchor: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---- settings ----------------------------------------------------------------------------


def origin(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}".lower()


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
    from docwhere.window import _settings

    items = _settings(db).get("wikis") or []
    return [w for w in items if isinstance(w, dict) and w.get("kind") in KINDS and w.get("name") and w.get("url")]


def add_wiki(db: Path, name: str, kind: str, url: str, spaces: list[str]) -> dict:
    from docwhere.window import _save_settings

    if kind not in KINDS:
        raise WikiError(f"unknown wiki kind {kind!r}; known: {', '.join(KINDS)}")
    name = name.strip()
    if not name:
        raise WikiError("a wiki needs a name")
    entry = {"name": name, "kind": kind, "url": checked_url(url), "spaces": [s.strip() for s in spaces if s.strip()]}
    _save_settings(db, wikis=[w for w in wikis(db) if w["name"] != name] + [entry])
    return entry


def remove_wiki(db: Path, name: str) -> bool:
    from docwhere.window import _save_settings

    kept = [w for w in wikis(db) if w["name"] != name]
    if len(kept) == len(wikis(db)):
        return False
    _save_settings(db, wikis=kept)
    return True


def client(entry: dict):
    from docwhere.wiki.confluence import Confluence

    return {"confluence": Confluence}[entry["kind"]](entry["url"], entry.get("spaces") or [])


# ---- HTTP ----------------------------------------------------------------------------------


class _SameOrigin(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed: str) -> None:
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(newurl) != self.allowed:
            raise WikiError(f"the wiki redirected to another site, not followed: {safe_url(newurl)}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class Response:
    status: int
    url: str  # after redirects
    body: bytes
    type: str

    def text(self) -> str:
        charset = "utf-8"
        if "charset=" in self.type:
            charset = self.type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:  # a charset Python does not know
            return self.body.decode("utf-8", errors="replace")

    def json(self):
        try:
            return json.loads(self.text())
        except ValueError:
            return None


_reachable: set[str] = set()


def _check_reachable(url: str) -> None:
    """Fail within seconds when the wiki's server cannot be reached at all (a PC off the
    office network), instead of waiting out the request timeout. Skipped behind a proxy,
    which is what the connection then goes to."""
    import socket

    parts = urllib.parse.urlsplit(url)
    key = origin(url)
    if key in _reachable:
        return
    proxies = urllib.request.getproxies()
    if proxies.get(parts.scheme) and not urllib.request.proxy_bypass(parts.hostname or ""):
        return
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        socket.create_connection((parts.hostname, port), timeout=REACH_TIMEOUT).close()
    except OSError as e:
        raise WikiError(f"cannot reach the wiki at {key} (is this PC on its network?): {e}") from None
    _reachable.add(key)


def get(base: str, path: str, params: dict | None = None) -> Response:
    """GET ``path`` on the wiki at ``base``. HTTP errors are returned, not raised, so callers
    can tell "no such API" (404) from "cannot reach the wiki" (raised as WikiError)."""
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    if origin(url) != origin(base):
        raise WikiError(f"not a page of the wiki: {url}")
    _check_reachable(url)
    opener = urllib.request.build_opener(_SameOrigin(origin(base)))
    request = urllib.request.Request(url, headers={"Accept": "application/json, text/html;q=0.9"})
    try:
        with opener.open(request, timeout=TIMEOUT) as r:
            return Response(r.status, r.geturl(), _read(r), r.headers.get("Content-Type", ""))
    except urllib.error.HTTPError as e:
        with e:
            return Response(e.code, e.geturl() or url, _read(e), e.headers.get("Content-Type", "") if e.headers else "")
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise WikiError(f"cannot reach the wiki at {base}: {reason}") from None


def _read(response) -> bytes:
    body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise WikiError(f"the wiki sent more than {MAX_RESPONSE >> 20} MB for one request; not read")
    return body


# ---- results -------------------------------------------------------------------------------


def hits(name: str, pages: list[Page], terms: list[str], per_page: int = 2) -> list[WikiHit]:
    """The best sections of each page for ``terms``, ranked like local hits."""
    from docwhere.extractors.html import html_chunks
    from docwhere.model import norm
    from docwhere.search import _locate, _score

    folded = [t.lower() for t in map(norm, terms) if t]
    out: list[WikiHit] = []
    for rank, page in enumerate(pages):
        rows = [
            {"title": c.title, "crumb": c.crumb, "body": "\n".join(c.lines), "path": page.title, "anchors": json.dumps(c.anchors)}
            for c in html_chunks(page.html, page.title, page.root)
        ]
        scored = sorted(((_score(r, folded)[0], i) for i, r in enumerate(rows)), key=lambda s: (-s[0], s[1]))
        best = [i for score, i in scored if score > 0][:per_page]
        if not best and rows:  # the wiki matched a form of the word docwhere does not see
            best = [1 if len(rows) > 1 else 0]  # the first section after the bare page title
        for i in best:
            row = rows[i]
            snippet, anchor = _locate(row, [t for t in map(norm, terms) if t]) if folded else (row["body"][:120], {})
            fragment = anchor.get("fragment")
            out.append(WikiHit(
                0, name, page.id, page.title, row["crumb"], page.space,
                page.url + (f"#{urllib.parse.quote(fragment)}" if fragment else ""),
                snippet, terms, page.updated, round(_score(row, folded)[0] - rank * 0.01, 3),
                {k: v for k, v in anchor.items() if k in ("fragment", "term", "text")},
            ))
    out.sort(key=lambda h: -h.score)
    for n, hit in enumerate(out, 1):
        hit.n = n
    return out
