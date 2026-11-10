"""Confluence Server / Data Center, read without logging in.

The REST API (5.x and later) is used when the server answers it: one CQL search returns the
matching pages with their bodies, so a question costs one request. Servers without it, or
with it switched off, are read through the pages people use: the site search page, then
each result's page, keeping only its main content.
"""

from __future__ import annotations

import html as html_lib
import re
import urllib.parse
from html.parser import HTMLParser

from docwhere.wiki import Page, WikiError, clean, origin
from docwhere.wiki.net import get

EXPAND = "body.view,space,version"
MAX_SPACES = 1000
SPACE_PAGE = 100
ROOT_ID = "main-content"  # the page body in Confluence's page layout


class Confluence:
    def __init__(self, url: str, spaces: list[str]) -> None:
        self.base = url.rstrip("/")
        self.spaces = spaces

    # ---- search ----------------------------------------------------------------------

    def search(self, terms: list[str], require_all: bool = False, limit: int = 5) -> list[Page]:
        r = get(self.base, "/rest/api/content/search", {"cql": self.cql(terms, require_all), "limit": limit, "expand": EXPAND})
        if _is_login(r):  # some servers let anonymous people read pages but not use the API
            return self._site_search(terms, require_all, limit)
        data = r.json() if r.status == 200 else None
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            return [self._rest_page(item) for item in data["results"] if isinstance(item, dict)]
        if r.status in (200, 400, 404, 405, 501):  # no REST API, or no CQL in this version
            return self._site_search(terms, require_all, limit)
        raise WikiError(f"the wiki's search failed (HTTP {r.status})")

    def cql(self, terms: list[str], require_all: bool) -> str:
        quoted = [f'text ~ "{_cql_escape(t)}"' for t in terms if t.strip()]
        query = (" AND " if require_all else " OR ").join(quoted) or 'text ~ ""'
        query = f"type = page AND ({query})"
        if self.spaces:
            query += " AND space in (" + ", ".join(f'"{_cql_escape(s)}"' for s in self.spaces) + ")"
        return query

    def _site_search(self, terms: list[str], require_all: bool, limit: int) -> list[Page]:
        params = {
            "queryString": (" AND " if require_all else " ").join(terms),
            "contentType": "page",
            "where": self.spaces[0] if len(self.spaces) == 1 else "conf_all",
        }
        r = get(self.base, "/dosearchsite.action", params)
        self._refuse_login(r)
        if r.status != 200:
            raise WikiError(f"the wiki has no search this version of docwhere can read (HTTP {r.status})")
        links = _ResultLinks.find(r.text())
        pages, skipped = [], []
        prefix = urllib.parse.urlsplit(self.base).path
        for href in links:
            if len(pages) >= limit:
                break
            absolute = urllib.parse.urljoin(self.base + "/", href)
            path = urllib.parse.urlsplit(absolute).path
            if origin(absolute) != origin(self.base):
                continue  # a link off the wiki on its own search page
            if not path[len(prefix):].startswith(("/display/", "/pages/viewpage.action")):
                continue  # only page links are read
            try:
                page = self.page(absolute)
            except WikiError as e:  # one result gone or closed must not lose the others
                if "log in" in str(e):
                    raise
                skipped.append(e)
                continue
            if not self.spaces or page.space in self.spaces or not page.space:
                pages.append(page)
        if skipped and not pages:
            raise skipped[0]
        return pages

    # ---- pages -----------------------------------------------------------------------

    def page(self, ref: str) -> Page:
        """A page by id, or by its address on this wiki (/pages/viewpage.action?pageId=…,
        /display/SPACE/Title)."""
        ref = ref.strip()
        if ref.startswith(("http://", "https://")):
            if origin(ref) != origin(self.base):
                raise WikiError(f"not a page of this wiki: {ref}")
            parts = urllib.parse.urlsplit(ref)
            ref = urllib.parse.urlunsplit(("", "", parts.path, parts.query, ""))
        prefix = urllib.parse.urlsplit(self.base).path  # a wiki served under /confluence
        if prefix and ref.startswith(prefix + "/"):
            ref = ref[len(prefix):]
        page_id = ref if ref.isdigit() else (urllib.parse.parse_qs(urllib.parse.urlsplit(ref).query).get("pageId") or [""])[0]
        if page_id.isdigit():
            r = get(self.base, f"/rest/api/content/{page_id}", {"expand": EXPAND})
            data = r.json() if r.status == 200 else None  # a login answer falls through to the page itself
            if isinstance(data, dict) and data.get("id"):
                return self._rest_page(data)
            return self._html_page("/pages/viewpage.action", {"pageId": page_id})
        m = re.match(r"^/display/([^/?#]+)/([^?#]+)", ref)
        if m:
            space, title = urllib.parse.unquote_plus(m.group(1)), urllib.parse.unquote_plus(m.group(2))
            r = get(self.base, "/rest/api/content", {"spaceKey": space, "title": title, "expand": EXPAND})
            data = r.json() if r.status == 200 else None
            if isinstance(data, dict) and data.get("results"):
                return self._rest_page(data["results"][0])
            return self._html_page(m.group(0), None)
        raise WikiError(f"not a page address or id: {ref}")

    def _rest_page(self, item: dict) -> Page:
        page_id = str(item.get("id", ""))
        webui = ((item.get("_links") or {}).get("webui")) or ""
        url = self.base + webui if webui.startswith("/") else f"{self.base}/pages/viewpage.action?pageId={page_id}"
        return Page(
            id=page_id,
            title=str(item.get("title", "")),
            url=url,
            space=str((item.get("space") or {}).get("key", "")),
            updated=str((item.get("version") or {}).get("when", "")),
            html=str(((item.get("body") or {}).get("view") or {}).get("value", "")),
        )

    def _html_page(self, path: str, params: dict | None) -> Page:
        r = get(self.base, path, params)
        self._refuse_login(r)
        if r.status != 200:
            raise WikiError(f"page not found on the wiki (HTTP {r.status}): {path}")
        html = r.text()
        meta = _Meta.read(html)
        page_id = meta.get("ajs-page-id", "") or (params or {}).get("pageId", "")
        title = meta.get("ajs-page-title", "")
        if not title:
            m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
            title = (html_lib.unescape(m.group(1)).split(" - ")[0].strip() if m else "") or path
        url = f"{self.base}/pages/viewpage.action?pageId={page_id}" if page_id else self.base + path
        return Page(id=page_id, title=title, url=url, space=meta.get("ajs-space-key", ""), html=html, root=ROOT_ID)

    # ---- spaces ----------------------------------------------------------------------

    def space_list(self) -> list[tuple[str, str]]:
        """(key, name) of the spaces anyone can view: from the REST API, else the space directory."""
        found: list[tuple[str, str]] = []
        for start in range(0, MAX_SPACES, SPACE_PAGE):  # a bounded number of requests, whatever the answers
            r = get(self.base, "/rest/api/space", {"limit": SPACE_PAGE, "start": start})
            data = r.json() if r.status == 200 else None
            if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                break
            found += [(clean(s.get("key")), clean(s.get("name"))) for s in data["results"] if isinstance(s, dict) and s.get("key")]
            if len(data["results"]) < SPACE_PAGE:
                return found
        if found:
            return found[:MAX_SPACES]
        r = get(self.base, "/spacedirectory/view.action")
        self._refuse_login(r)
        if r.status != 200:
            raise WikiError(f"the wiki has no space list this version of docwhere can read (HTTP {r.status})")
        return _SpaceLinks.find(r.text())[:MAX_SPACES]

    # ---- check -----------------------------------------------------------------------

    def check(self, term: str | None = None) -> list[tuple[bool | None, str]]:
        """What works on this server, for someone setting it up: [(ok, line), ...]."""
        lines: list[tuple[bool | None, str]] = []
        r = get(self.base, "/")
        if _is_login(r):
            return [(False, "the wiki asks to log in; docwhere reads only wikis that can be viewed without logging in")]
        meta = _Meta.read(r.text()) if r.status == 200 else {}
        version = clean(meta.get("ajs-version-number", ""))
        lines.append((r.status == 200, f"reached {self.base} (HTTP {r.status})" + (f", Confluence {version}" if version else "")))
        api = get(self.base, "/rest/api/space", {"limit": 1})
        rest = api.status == 200 and isinstance(api.json(), dict)
        if rest:
            lines.append((True, "REST API answers"))
        elif _is_login(api):
            lines.append((None, "the REST API asks to log in; the site search page is used instead"))
        else:
            lines.append((None, f"no REST API (HTTP {api.status}); the site search page is used instead"))
        for space in self.spaces:
            s = get(self.base, f"/rest/api/space/{urllib.parse.quote(space)}") if rest else None
            if s is None:
                lines.append((None, f"space {space}: cannot check without the REST API"))
            else:
                name = clean((s.json() or {}).get("name", "")) if s.status == 200 else ""
                lines.append((s.status == 200, f"space {space}: " + (name or "found") if s.status == 200 else f"space {space}: not found (HTTP {s.status})"))
        if term:
            pages = self.search([term], limit=3)
            found = ", ".join(p.title for p in pages) or "none"
            lines.append((bool(pages), f"search for {term!r}: {len(pages)} page(s): {found}"))
            if not pages and not term.isascii():
                lines.append((None, "no page found for a Japanese word: if pages do contain it, ask the wiki's administrator "
                                    "whether the indexing language (General Configuration) is set to Japanese; other words "
                                    "or shorter forms may still match"))
        else:
            lines.append((None, "add --term WORD to try a search, e.g. a Japanese word that should match"))
        return lines

    def _refuse_login(self, r) -> None:
        if _is_login(r):
            raise WikiError("the wiki asks to log in for this; docwhere reads only what can be viewed without logging in")


def _is_login(r) -> bool:
    return r.status in (401, 403) or "login.action" in r.url


def _cql_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class _Meta(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            a = dict(attrs)
            if a.get("name", "").startswith("ajs-") and a.get("content") is not None:
                self.values.setdefault(a["name"], a["content"])

    @classmethod
    def read(cls, html: str) -> dict[str, str]:
        p = cls()
        p.feed(html)
        return p.values


class _SpaceLinks(HTMLParser):
    """Spaces on the space directory page: links to /display/KEY with the space's name."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.spaces: dict[str, str] = {}
        self.key: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            m = re.search(r"/display/([^/?#]+)/?$", dict(attrs).get("href") or "")
            self.key, self.text = (urllib.parse.unquote(m.group(1)), []) if m else (None, [])

    def handle_data(self, data):
        if self.key:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.key:
            name = clean("".join(self.text))
            if name:
                self.spaces.setdefault(clean(self.key), name)
            self.key = None

    @classmethod
    def find(cls, html: str) -> list[tuple[str, str]]:
        p = cls()
        p.feed(html)
        return list(p.spaces.items())


class _ResultLinks(HTMLParser):
    """Links to result pages on the site search page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.marked: list[str] = []
        self.any: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        a = dict(attrs)
        href = a.get("href") or ""
        if not ("/display/" in href or "viewpage.action" in href):
            return
        if "search-result-link" in (a.get("class") or ""):
            self.marked.append(href)
        self.any.append(href)

    @classmethod
    def find(cls, html: str) -> list[str]:
        p = cls()
        p.feed(html)
        links = p.marked or p.any
        return list(dict.fromkeys(links))
