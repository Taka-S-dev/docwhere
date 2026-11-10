"""HTTP to an added wiki: same-site redirects only, bounded responses, and a quick give-up
when the wiki's server cannot be reached at all."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from docwhere.wiki.core import WikiError, origin, safe_url

TIMEOUT = 20  # seconds per request
REACH_TIMEOUT = 3  # seconds to open a connection at all: off the wiki's network, fail fast
MAX_RESPONSE = 16 << 20  # bytes read from one response


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
