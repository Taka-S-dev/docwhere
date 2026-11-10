"""What every part of the wiki support shares: the error, a page read from a wiki, a hit,
and the cleaning applied to everything a server sends."""

from __future__ import annotations

import urllib.parse
from dataclasses import asdict, dataclass, field


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


def origin(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}".lower()
