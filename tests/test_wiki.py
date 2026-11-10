import json
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from docwhere.cli import main
from docwhere.extractors.html import html_chunks
from docwhere.wiki import WikiError, checked_url
from docwhere.wiki.confluence import Confluence

PAGES = {
    "101": {
        "title": "接続設定",
        "body": "<h2 id='p101-timeout'>タイムアウト</h2><p>接続のタイムアウトは設定ファイルで変える。</p>"
                "<table><tr><th>項目</th><th>既定値</th></tr><tr><td>接続タイムアウト</td><td>30 秒</td></tr></table>"
                "<h2 id='p101-retry'>再試行</h2><p>失敗したら 3 回まで再試行する。</p><script>alert(1)</script>",
    },
    "102": {
        "title": "障害対応",
        "body": "<h2>連絡先</h2><p>当番に電話する。</p><h2>よくある原因</h2><p>回線が遅いとタイムアウトが起きる。</p>",
    },
}


class FakeWiki(BaseHTTPRequestHandler):
    """An old Confluence: with its REST API ("rest"), without it ("legacy"), behind a login
    ("login"), or redirecting its search to another site ("redirect")."""

    mode = "rest"
    requests: list[str] = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        FakeWiki.requests.append(self.path)
        url = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if self.mode == "login" and not url.path.startswith("/login.action"):
            return self._send(302, "", location="/login.action?os_destination=" + urllib.parse.quote(self.path))
        if url.path == "/login.action":
            return self._send(200, "<form>log in</form>")
        if self.mode == "redirect" and url.path.startswith("/rest/api/content/search"):
            return self._send(302, "", location="http://elsewhere.invalid/collect")
        if url.path.startswith("/rest/") and self.mode == "legacy":
            return self._send(404, "<h1>Page not found</h1>")
        if url.path.startswith("/rest/") and self.mode == "api-login":
            return self._send(401, '{"message": "not permitted"}', "application/json")
        if url.path == "/":
            return self._send(200, "<html><head><meta name='ajs-version-number' content='6.15.4'></head><body>home</body></html>")
        if url.path == "/rest/api/space":
            return self._json({"results": [{"key": "OPS", "name": "運用"}, {"key": "DEV", "name": "開発[2J"}]})
        if url.path == "/spacedirectory/view.action":
            return self._send(200, "<a href='/display/OPS'>運用</a><a href='/display/DEV/'>開発</a><a href='/display/OPS/Page'>page</a>")
        if url.path.startswith("/rest/api/space/"):
            key = url.path.rsplit("/", 1)[1]
            return self._json({"key": "OPS", "name": "運用"}) if key == "OPS" else self._json({"message": "no"}, 404)
        if url.path == "/rest/api/content/search":
            terms = [t for t in _terms(q["cql"])]
            found = [i for i, p in PAGES.items() if any(t in p["title"] + p["body"] for t in terms)]
            return self._json({"results": [self._item(i) for i in found][: int(q.get("limit", 5))]})
        if url.path.startswith("/rest/api/content/"):
            page_id = url.path.rsplit("/", 1)[1]
            return self._json(self._item(page_id)) if page_id in PAGES else self._json({}, 404)
        if url.path == "/dosearchsite.action":
            terms = q["queryString"].split()
            found = [i for i, p in PAGES.items() if any(t in p["title"] + p["body"] for t in terms)]
            links = "".join(f"<li><a class='search-result-link visitable' href='/pages/viewpage.action?pageId={i}'>"
                            f"{PAGES[i]['title']}</a></li>" for i in found)
            return self._send(200, f"<a href='/display/OPS'>space home</a><ol class='search-results'>{links}</ol>")
        if url.path == "/pages/viewpage.action" and q.get("pageId") in PAGES:
            p = PAGES[q["pageId"]]
            return self._send(200, f"<html><head><title>{p['title']} - 運用 - Confluence</title>"
                                   f"<meta name='ajs-page-id' content='{q['pageId']}'><meta name='ajs-space-key' content='OPS'></head>"
                                   f"<body><div id='navigation'>メニュー タイムアウト一覧</div><div id='main-content' class='wiki-content'>"
                                   f"{p['body']}</div><div id='footer'>フッター</div></body></html>")
        return self._send(404, "not found")

    def _item(self, page_id):
        p = PAGES[page_id]
        return {"id": page_id, "title": p["title"], "space": {"key": "OPS"}, "version": {"when": "2026-01-05T10:00:00.000+09:00"},
                "body": {"view": {"value": p["body"]}}, "_links": {"webui": f"/display/OPS/{urllib.parse.quote(p['title'])}"}}

    def _json(self, data, status=200):
        self._send(status, json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")

    def _send(self, status, body, type="text/html; charset=utf-8", location=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", type)
        self.send_header("Content-Length", str(len(data)))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(data)


def _terms(cql):
    import re

    return [t.replace('\\"', '"') for t in re.findall(r'text ~ "((?:[^"\\]|\\.)*)"', cql)]


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})  # the test server is on this machine
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWiki)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FakeWiki.mode, FakeWiki.requests = "rest", []
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "cli" / "index.db"))
    url = f"http://127.0.0.1:{server.server_address[1]}"
    assert main(["wiki", "add", "team", url, "--space", "OPS"]) == 0
    yield url
    server.shutdown()


def _json(capsys, argv):
    capsys.readouterr()
    assert main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_one_request_finds_pages_and_their_sections(wiki, capsys):
    FakeWiki.requests.clear()
    out = _json(capsys, ["wiki", "search", "タイムアウト", "--json"])
    assert len(FakeWiki.requests) == 1  # the CQL search returns the bodies too
    cql = urllib.parse.parse_qs(urllib.parse.urlsplit(FakeWiki.requests[0]).query)["cql"][0]
    assert cql == 'type = page AND (text ~ "タイムアウト") AND space in ("OPS")'
    first = out["hits"][0]
    assert first["title"] == "接続設定" and first["crumb"] == "接続設定 > タイムアウト"
    assert first["url"].endswith("#p101-timeout") and first["space"] == "OPS"
    assert {h["title"] for h in out["hits"]} == {"接続設定", "障害対応"}


def test_show_prints_the_hit_section_without_scripts(wiki, capsys):
    _json(capsys, ["wiki", "search", "再試行", "--json"])
    assert main(["wiki", "show", "1"]) == 0
    text = capsys.readouterr().out
    assert "## 接続設定 > 再試行 ◀ hit" in text and "3 回まで再試行する" in text
    assert "項目: 接続タイムアウト / 既定値: 30 秒" in text  # the neighbouring section, as -C 1
    assert "alert" not in text


def test_without_the_rest_api_the_search_page_is_read(wiki, capsys):
    FakeWiki.mode = "legacy"
    out = _json(capsys, ["wiki", "search", "タイムアウト", "--json"])
    assert {h["title"] for h in out["hits"]} == {"接続設定", "障害対応"}
    assert all("pageId=" in h["url"] for h in out["hits"])
    assert main(["wiki", "show", "1"]) == 0
    text = capsys.readouterr().out
    assert "メニュー" not in text and "フッター" not in text  # only the page body


def test_redirect_to_another_site_is_not_followed(wiki, capsys):
    FakeWiki.mode = "redirect"
    capsys.readouterr()
    assert main(["wiki", "search", "タイムアウト"]) == 2
    assert "another site" in capsys.readouterr().err


def test_a_wiki_that_asks_to_log_in_is_reported(wiki, capsys):
    FakeWiki.mode = "login"
    capsys.readouterr()
    assert main(["wiki", "search", "タイムアウト"]) == 2
    assert "log in" in capsys.readouterr().err


def test_check_reports_what_works(wiki, capsys):
    capsys.readouterr()
    assert main(["wiki", "check", "--term", "タイムアウト"]) == 0
    text = capsys.readouterr().out
    assert "Confluence 6.15.4" in text and "REST API answers" in text and "space OPS: 運用" in text
    assert "2 page(s)" in text
    FakeWiki.mode = "legacy"
    assert main(["wiki", "check", "--term", "タイムアウト"]) == 0
    assert "no REST API" in capsys.readouterr().out


def test_open_only_opens_pages_of_added_wikis(wiki, capsys, monkeypatch):
    import webbrowser

    opened = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    _json(capsys, ["wiki", "search", "タイムアウト", "--json"])
    assert main(["wiki", "open", "1"]) == 0
    assert main(["wiki", "open", "http://elsewhere.invalid/x"]) == 2
    assert main(["wiki", "open", "javascript:alert(1)"]) == 2
    assert len(opened) == 1 and opened[0].startswith(wiki)


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///C:/x", "http://user:pw@wiki.example", "wiki.example", "http://wiki.example/?a=1"])
def test_only_plain_wiki_addresses_can_be_added(url):
    with pytest.raises(WikiError):
        checked_url(url)


def test_cql_quotes_are_escaped():
    wiki = Confluence("http://wiki.example", ["A\"B"])
    assert wiki.cql(['say "hi"', "x"], True) == 'type = page AND (text ~ "say \\"hi\\"" AND text ~ "x") AND space in ("A\\"B")'


def test_pages_of_another_site_are_refused():
    with pytest.raises(WikiError):
        Confluence("http://wiki.example", []).page("http://elsewhere.invalid/pages/viewpage.action?pageId=1")


def test_html_sections_tables_and_skipped_markup():
    html = ("<html><head><title>手順</title><style>p{}</style></head><body><h1 id='a'>準備</h1><p>電源を入れる。</p>"
            "<table><tr><th>名前</th><th>値</th></tr><tr><td>待ち時間</td><td>10 秒</td></tr></table>"
            "<h2>確認</h2><p>ランプを見る。</p><script>steal()</script></body></html>")
    chunks = html_chunks(html)
    assert [c.crumb for c in chunks] == ["手順 > 準備", "手順 > 準備 > 確認"]
    assert "名前: 待ち時間 / 値: 10 秒" in chunks[0].lines and chunks[0].anchors[0] == {"fragment": "a"}
    assert not any("steal" in line or "p{}" in line for c in chunks for line in c.lines)


def test_saved_html_files_are_searched(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "db" / "index.db"))
    folder = tmp_path / "pages"
    folder.mkdir()
    (folder / "export.html").write_text("<h1>バックアップ</h1><p>毎晩 2 時に取る。</p>", encoding="utf-8")
    out = _json(capsys, ["search", "バックアップ", "--in", str(folder), "--json"])
    assert out["hits"][0]["crumb"] == "バックアップ"


def test_links_off_the_wiki_on_its_search_page_are_skipped(monkeypatch):
    from docwhere.wiki import Page, Response
    from docwhere.wiki import confluence as module

    wiki = Confluence("http://wiki.example", [])
    page = "<a class='search-result-link' href='http://elsewhere.invalid/display/X/Y'>a</a>" \
           "<a class='search-result-link' href='/pages/viewpage.action?pageId=7'>b</a>"
    monkeypatch.setattr(module, "get", lambda base, path, params=None: Response(200, base + path, page.encode(), "text/html; charset=bogus"))
    monkeypatch.setattr(wiki, "page", lambda ref: Page("7", "b", "http://wiki.example/x"))
    assert [p.id for p in wiki._site_search(["x"], False, 5)] == ["7"]


def test_server_strings_cannot_carry_terminal_escapes():
    wiki = Confluence("http://wiki.example", [])
    page = wiki._rest_page({"id": "1", "title": "evil\x1b]0;owned\x07\u202etitle", "space": {"key": "A\x1b[2J"},
                            "version": {"when": "2026\x1b[31m"}, "_links": {"webui": "/display/A/x\x1b[1mY"},
                            "body": {"view": {"value": "<h2 id='a\x1bb'>h</h2><p>x</p>"}}})
    for text in (page.title, page.space, page.updated, page.url):
        assert "\x1b" not in text and "\u202e" not in text and "\x07" not in text
    assert html_chunks(page.html)[0].anchors[0] == {"fragment": "ab"}


def test_error_messages_are_cleaned_before_printing(wiki, capsys):
    capsys.readouterr()
    assert main(["wiki", "show", "/x\x1b]0;owned\x07", "--wiki", "team"]) == 2
    err = capsys.readouterr().err
    assert "\x1b" not in err and "\x07" not in err and "not a page address" in err


def test_html_title_cannot_carry_terminal_escapes():
    chunks = html_chunks("<title>a\x1b]0;owned\x07b</title><h1>h</h1><p>x</p>")
    assert all("\x1b" not in c.crumb and "\x07" not in c.crumb for c in chunks)


def test_wiki_view_frames_the_quote_and_links_to_the_page(wiki, capsys):
    from pathlib import Path

    _json(capsys, ["wiki", "search", "タイムアウト", "--json"])
    FakeWiki.requests.clear()
    assert main(["wiki", "view", "--no-open", "--quote", "接続のタイムアウトは設定ファイルで変える。", "--answer", "設定ファイルで変える"]) == 0
    page = Path(capsys.readouterr().out.strip()).read_text(encoding="utf-8")
    assert len(FakeWiki.requests) == 2  # each page read once, as it is now
    assert '<mark class="ans">接続のタイムアウトは設定ファイルで変える。</mark>' in page
    assert 'href="#w1-s1"' in page and 'id="w1-s1"' in page  # evidence jumps to its section
    assert "<script" not in page and "alert(1)" not in page
    assert 'data-open-url="' + wiki + "/display/OPS/" in page and "#p101-timeout" in page
    assert "社内 wiki" in page and "Confluence" not in page


def test_search_view_needs_no_second_read(wiki, capsys, monkeypatch):
    from docwhere import window

    monkeypatch.setattr(window, "available", lambda: False)
    monkeypatch.setattr("docwhere.opener.start_file", lambda path: None)
    FakeWiki.requests.clear()
    capsys.readouterr()
    assert main(["wiki", "search", "再試行", "--view", "--quote", "失敗したら 3 回まで再試行する。"]) == 0
    assert len(FakeWiki.requests) == 1
    assert "view: " in capsys.readouterr().err


def test_the_window_opens_only_pages_of_added_wikis(wiki, monkeypatch, tmp_path):
    import os
    import webbrowser
    from pathlib import Path

    from docwhere import window

    opened = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    api = window.Api(Path(os.environ["DOCWHERE_DB"]))
    api._trusted = True
    assert api.open_url(wiki + "/display/OPS/x") and opened == [wiki + "/display/OPS/x"]
    for bad in ("http://elsewhere.invalid/x", "file:///C:/Windows/notepad.exe", "javascript:alert(1)"):
        with pytest.raises(PermissionError):
            api.open_url(bad)
    assert len(opened) == 1


def test_a_rest_api_that_asks_to_log_in_falls_back_to_the_pages(wiki, capsys):
    FakeWiki.mode = "api-login"  # pages open to everyone, the API only to logged-in users
    out = _json(capsys, ["wiki", "search", "タイムアウト", "--json"])
    assert {h["title"] for h in out["hits"]} == {"接続設定", "障害対応"}
    assert main(["wiki", "show", "1"]) == 0
    assert "接続のタイムアウトは設定ファイルで変える。" in capsys.readouterr().out
    assert main(["wiki", "check"]) == 0
    assert "REST API asks to log in" in capsys.readouterr().out


def test_an_unreachable_wiki_fails_fast(tmp_path, monkeypatch, capsys):
    import socket
    import time

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "db" / "index.db"))
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens there now
    assert main(["wiki", "add", "off", f"http://127.0.0.1:{port}"]) == 0
    started = time.monotonic()
    assert main(["wiki", "search", "x"]) == 2
    assert time.monotonic() - started < 10 and "cannot reach" in capsys.readouterr().err


def test_spaces_are_listed_with_their_names(wiki, capsys):
    out = _json(capsys, ["wiki", "spaces", "--json"])
    assert out == {"team": [{"key": "OPS", "name": "運用"}, {"key": "DEV", "name": "開発 [2J"}]}  # no escape survives
    FakeWiki.mode = "legacy"
    out = _json(capsys, ["wiki", "spaces", "--json"])
    assert out == {"team": [{"key": "OPS", "name": "運用"}, {"key": "DEV", "name": "開発"}]}


def test_one_unreadable_result_does_not_lose_the_others(monkeypatch):
    from docwhere.wiki import Page, Response
    from docwhere.wiki import confluence as module

    wiki = Confluence("http://wiki.example", [])
    results = "".join(f"<a class='search-result-link' href='/pages/viewpage.action?pageId={i}'>p</a>" for i in (1, 2))
    monkeypatch.setattr(module, "get", lambda base, path, params=None: Response(200, base + path, results.encode(), "text/html"))

    def page(ref):
        if ref.endswith("=1"):
            raise WikiError("page not found on the wiki (HTTP 404)")
        return Page("2", "two", "http://wiki.example/x")

    monkeypatch.setattr(wiki, "page", page)
    assert [p.id for p in wiki._site_search(["x"], False, 5)] == ["2"]


def test_space_list_requests_are_bounded(monkeypatch):
    from docwhere.wiki import Response
    from docwhere.wiki import confluence as module

    calls = []
    junk = json.dumps({"results": [{"name": "no key"}] * module.SPACE_PAGE}).encode()

    def get(base, path, params=None):
        calls.append(path)
        if path == "/rest/api/space":
            return Response(200, base + path, junk, "application/json")
        return Response(200, base + path, b"<a href='/display/A'>A</a>", "text/html")

    monkeypatch.setattr(module, "get", get)
    assert Confluence("http://wiki.example", []).space_list() == [("A", "A")]
    assert calls.count("/rest/api/space") == module.MAX_SPACES // module.SPACE_PAGE
