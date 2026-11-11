import json
from pathlib import Path

import pytest

from docwhere.cli import main


@pytest.fixture(autouse=True)
def no_office_export(monkeypatch):
    """Word and PowerPoint renditions need Office; tests that want one install a fake."""
    from docwhere import rendition, window

    monkeypatch.setattr(rendition, "as_pdf", lambda path, cache: None)
    monkeypatch.setattr(window, "show", lambda db, page: True)  # no GUI in tests; see test_window


def _view(capsys, *args) -> str:
    assert main(["view", "--no-open", *args]) == 0
    path = capsys.readouterr().out.strip()
    return open(path, encoding="utf-8").read()


def _search(docs, tmp_path, capsys, monkeypatch, *terms):
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "cli" / "index.db"))
    assert main(["search", *terms, "--in", str(docs), "--json", "-n", "20"]) == 0
    return json.loads(capsys.readouterr().out)["hits"]


def test_view_page_is_self_contained_with_pdf_page_images(docs, tmp_path, capsys, monkeypatch):
    hits = _search(docs, tmp_path, capsys, monkeypatch, "config")
    page = _view(capsys)

    assert "default-src 'none'" in page and "<script" not in page
    assert page.count('<article class="hit"') == len(hits)
    assert page.count('src="data:image/jpeg;base64,') == sum(h["path"].endswith(".pdf") for h in hits)
    assert "<mark>config</mark>" in page
    assert "#page=" in page  # the PDF link opens at the page


def test_view_escapes_document_text(docs, tmp_path, capsys, monkeypatch):
    (docs / "evil.md").write_text('# config <script>alert(1)</script>\n<img src=x onerror=alert(1)>\n', encoding="utf-8")
    _search(docs, tmp_path, capsys, monkeypatch, "config")
    page = _view(capsys)
    assert "<script>" not in page and "<img src=x" not in page
    assert "&lt;script&gt;" in page


def test_answer_quote_is_marked_above_the_terms(docs, tmp_path, capsys, monkeypatch):
    _search(docs, tmp_path, capsys, monkeypatch, "config")
    quote = "config は設定ファイルを読み込み、値を検証する。"
    page = _view(capsys, "--quote", quote, "--answer", "設定を検証するコマンド <b>")
    assert f'<mark class="ans">{quote}</mark>' in page
    assert '<p class="hit-line answer-line">' in page or '<p class="answer-line">' in page
    assert "設定を検証するコマンド &lt;b&gt;" in page  # the answer is escaped like document text


def test_pdf_quote_is_found_across_width_and_line_differences(tmp_path):
    import pymupdf

    from docwhere.viewer import _find

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_htmlbox(pymupdf.Rect(50, 50, 300, 400), "<p>管理者を除き，１台に１０００人まで<br>登録できます。</p>")
    (hit,) = _find(page, ["管理者を除き,1台に1000人まで登録できます。"])
    assert len(hit) == 2  # one rectangle per line the sentence covers
    assert _find(page, ["存在しない文"]) == []


def test_evidence_spread_over_pages_each_gets_its_closeup_and_page(docs, tmp_path, capsys, monkeypatch):
    _search(docs, tmp_path, capsys, monkeypatch, "config", "--ext", "pdf", "--per-file", "1")  # hit: p.2
    html = _view(capsys, "1", "--quote", "system outline", "--quote", "config --force skips checks", "--quote", "書かれていない文")
    hero = html[html.index('<section class="answer"') : html.index("</section>")]
    assert "根拠 3 件" in hero
    assert '<a class="closeup" href="#hit1-p1" ' in hero and '<a class="closeup" href="#hit1-p3" ' in hero
    assert "資料の中で位置を特定できませんでした。" in hero  # the third quote is in no hit file
    # both evidence pages are shown around the hit page, in page order
    positions = [html.index(f'id="hit1-p{p}"') for p in (1, 2, 3)]
    assert positions == sorted(positions)


def test_evidence_on_a_distant_page_is_found_through_the_index(tmp_path, capsys, monkeypatch):
    import pymupdf

    folder = tmp_path / "manual"
    folder.mkdir()
    doc = pymupdf.open()
    for i in range(1, 7):
        doc.new_page().insert_text((72, 72), "timeout overview" if i == 1 else f"page {i} body")
    doc[4].insert_text((72, 120), "max retry count is 5")
    doc.save(folder / "m.pdf")
    _search(folder, tmp_path, capsys, monkeypatch, "timeout")  # the hit is p.1
    html = _view(capsys, "1", "--quote", "max retry count is 5")
    hero = html[html.index('<section class="answer"') : html.index("</section>")]
    assert '<a href="#hit1-p5">p.5</a>' in hero and "#page=5" in hero
    assert '<a class="closeup" href="#hit1-p5" ' in hero
    assert 'id="hit1-p5"' in html and 'id="hit1-p3"' not in html  # only the evidence page is added


def test_view_selected_hits_only(docs, tmp_path, capsys, monkeypatch):
    _search(docs, tmp_path, capsys, monkeypatch, "config")
    page = _view(capsys, "2")
    assert page.count('<article class="hit"') == 1 and 'id="hit2"' in page


def test_rail_lists_every_piece_of_evidence_and_jumps_to_its_page(docs, tmp_path, capsys, monkeypatch):
    import re

    _search(docs, tmp_path, capsys, monkeypatch, "config", "--ext", "pdf", "--per-file", "1")
    html = _view(capsys, "1", "--quote", "system outline", "--quote", "config --force skips checks", "--quote", "書かれていない文")
    rail = html[html.index('<nav class="rail"') : html.index("</nav>")]
    assert '<a class="to-answer" href="#answer">' in rail and 'id="answer"' in html
    links = re.findall(r'<a class="item" href="#([^"]+)" title="[^"]*"><span class="pg">([^<]*)</span>', rail)
    assert links == [("hit1-p1", "p.1"), ("hit1-p3", "p.3")]
    assert all(f'id="{target}"' in html for target, _ in links)  # every link lands on a page
    assert "位置不明" in rail  # the quote that was not found is listed, without a link

    plain = _view(capsys, "1")  # one hit and nothing to cite: no rail
    assert '<nav class="rail"' not in plain and 'class="layout"' in plain


def test_view_and_snap_add_neighbouring_pages_within_the_document(docs, tmp_path, capsys, monkeypatch):
    hits = _search(docs, tmp_path, capsys, monkeypatch, "config", "--ext", "pdf", "--per-file", "1")
    page = int(hits[0]["location"].removeprefix("p."))  # 設計書.pdf has 3 pages
    html = _view(capsys, "1", "-P", "5")
    assert html.count('src="data:image/jpeg;base64,') == 3  # pages past either end are skipped
    # pages in page order, each labelled and addressable; the hit page is marked
    assert page == 2
    positions = [html.index(f'id="hit1-p{p}"') for p in (1, 2, 3)]
    assert positions == sorted(positions)
    assert '<figure class="page is-hit" id="hit1-p2"' in html

    assert main(["snap", "1", "-P", "1"]) == 0
    saved = capsys.readouterr().out.split()
    assert [s.rsplit("_p", 1)[1] for s in saved] == [f"{p}.png" for p in range(max(1, page - 1), min(3, page + 1) + 1)]


def test_closeup_of_the_answer_heads_the_page(docs, tmp_path, capsys, monkeypatch):
    _search(docs, tmp_path, capsys, monkeypatch, "config", "--ext", "pdf", "--per-file", "1")
    html = _view(capsys, "1", "--quote", "config --force skips checks", "--answer", "--force で検査を飛ばす")
    hero = html[html.index('<section class="answer"') : html.index("</section>")]
    assert "--force で検査を飛ばす" in hero and "設計書.pdf" in hero
    # the hit is p.2 but the quoted sentence is on p.3: the source points there, that page is
    # added below the hit page, and the close-up and the page number jump to it
    assert '<a class="closeup" href="#hit1-p3" ' in hero and '<a href="#hit1-p3">p.3</a>' in hero
    assert "#page=3" in hero
    assert html.index('id="hit1-p2"') < html.index('id="hit1-p3"')
    # the evidence page is told apart from the hit page, and the documents have their own heading
    assert '<figure class="page has-evidence" id="hit1-p3"' in html and '<figure class="page is-hit" id="hit1-p2"' in html
    assert '<h2 class="region">資料 1 件</h2>' in html
    assert 'id="hit1-p1"' not in html  # only the evidence page is added, not every neighbour


def test_snap_writes_the_pdf_page_and_refuses_other_kinds(docs, tmp_path, capsys, monkeypatch):
    hits = _search(docs, tmp_path, capsys, monkeypatch, "config")
    pdf_n = next(h["n"] for h in hits if h["path"].endswith(".pdf"))
    assert main(["snap", str(pdf_n)]) == 0
    png = capsys.readouterr().out.strip()
    assert open(png, "rb").read(8) == b"\x89PNG\r\n\x1a\n"

    word_n = next(h["n"] for h in hits if h["path"].endswith(".docx"))
    assert main(["snap", str(word_n)]) == 2


def test_each_view_is_kept_and_listed_in_the_history(docs, tmp_path, capsys, monkeypatch):
    from docwhere import viewer

    monkeypatch.setitem(viewer.VIEW_RETENTION, "min_keep", 1)
    monkeypatch.setitem(viewer.VIEW_RETENTION, "max_keep", 2)
    _search(docs, tmp_path, capsys, monkeypatch, "config")
    paths = []
    for q in ("config とは？", "backup とは？", "<script>x</script>"):
        assert main(["view", "--no-open", "--title", q, "--answer", f"{q} の答え"]) == 0
        paths.append(capsys.readouterr().out.strip())
    assert len(set(paths)) == 3
    folder = tmp_path / "cli" / "views"
    assert not Path(paths[0]).exists()  # only the newest max_keep pages stay
    assert sorted(p.name for p in folder.glob("2*.html")) == sorted(Path(p).name for p in paths[1:])
    history = (folder / "index.html").read_text(encoding="utf-8")
    assert "backup とは？" in history and "config とは？" not in history
    assert "&lt;script&gt;" in history and "<script>" not in history
    assert "<title>backup とは？</title>" in open(paths[1], encoding="utf-8").read()  # no tool name on screen


def test_prune_keeps_the_newest_and_drops_old_or_heavy_files(tmp_path):
    import os
    import time

    from docwhere.viewer import prune

    now = time.time()
    for i, (age_days, size) in enumerate([(0, 10), (0, 10), (0, 10), (30, 10), (0, 500)]):
        f = tmp_path / f"{i}.png"
        f.write_bytes(b"x" * size)
        os.utime(f, (now - age_days * 86400 - i, now - age_days * 86400 - i))  # 0 is the newest
    freed = prune(tmp_path, "*.png", min_keep=1, max_keep=10, max_age=7 * 86400, max_bytes=100)
    assert sorted(f.name for f in tmp_path.glob("*.png")) == ["0.png", "1.png", "2.png"]
    assert freed == 510  # 3.png is too old, 4.png pushes the folder past max_bytes

    prune(tmp_path, "*.png", min_keep=2, max_keep=0, max_age=0, max_bytes=0)
    assert sorted(f.name for f in tmp_path.glob("*.png")) == ["0.png", "1.png"]  # min_keep always stays


def test_clean_removes_views_and_snapshots(docs, tmp_path, capsys, monkeypatch):
    hits = _search(docs, tmp_path, capsys, monkeypatch, "config")
    _view(capsys)
    assert main(["snap", str(next(h["n"] for h in hits if h["path"].endswith(".pdf")))]) == 0
    capsys.readouterr()
    assert main(["clean"]) == 0
    assert "removed" in capsys.readouterr().out
    assert not list((tmp_path / "cli" / "views").iterdir()) and not list((tmp_path / "cli" / "snap").iterdir())


def test_word_hit_is_shown_from_its_pdf_rendition(docs, tmp_path, capsys, monkeypatch):
    import re

    import pymupdf

    from docwhere import rendition

    # stand-in for Word's export: the config section lands on page 2
    fake = tmp_path / "rendition.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "intro page")
    doc.new_page().insert_htmlbox(
        pymupdf.Rect(50, 50, 545, 790),
        "<h2>config コマンド</h2><p>config は設定ファイルを読み込み、値を検証する。</p><p>--force 検証エラーを無視する</p>",
    )
    doc.save(fake)
    monkeypatch.setattr(rendition, "as_pdf", lambda path, cache: fake if path.endswith(".docx") else None)

    _search(docs, tmp_path, capsys, monkeypatch, "config", "--ext", "docx", "--per-file", "1")
    html = _view(capsys, "1", "--quote=--force")
    assert '<figure class="page is-hit has-evidence" id="hit1-p2"' in html  # the hit page also holds the quote
    assert '<span class="tag ev-tag">根拠</span><span class="tag hit-tag">ヒットしたページ</span>' in html
    assert "ページは Word で PDF にしたときの番号です" in html
    hero = html[html.index('<section class="answer"') : html.index("</section>")]
    assert '<a class="closeup" href="#hit1-p2" ' in hero
    assert "PDF にしたものをこのページで開く" in hero
    # Word and Excel files are never linked (the browser would download a copy); the path is offered for copying
    assert not re.search(r'href="file:[^"#]*\.(docx|xlsx)"', html)
    assert 'class="copy"' in html and "Ctrl+C" in html


def test_sheet_hit_is_shown_as_a_table_excerpt_with_the_row_marked(docs, tmp_path, capsys, monkeypatch):
    _search(docs, tmp_path, capsys, monkeypatch, "backup", "--ext", "xlsx")
    html = _view(capsys, "1", "--quote", "コマンド: backup / 概要: データ退避")
    card = html[html.index('<article class="hit"') :]
    assert '<figure class="sheet"><figcaption>CLI</figcaption>' in card
    assert '<tr class="header-row"><th scope="row">1</th><td>コマンド</td><td>概要</td></tr>' in card
    assert 'class="hit-row" id="hit1-s' in card and "-r3" in card
    hero = html[html.index('<section class="answer"') : html.index("</section>")]
    assert "CLI 3行目" in hero and '<figure class="sheet">' in hero


def test_every_id_in_the_page_is_unique(docs, tmp_path, capsys, monkeypatch):
    import re
    from collections import Counter

    _search(docs, tmp_path, capsys, monkeypatch, "config")
    html = _view(capsys, "--quote", "コマンド: config / 概要: 設定を検証する", "--quote", "config --force skips checks")
    repeated = [i for i, n in Counter(re.findall(r'\sid="([^"]+)"', html)).items() if n > 1]
    assert repeated == []


def test_pdf_links_open_a_small_excerpt_around_the_page(tmp_path, capsys, monkeypatch):
    import re

    import pymupdf

    folder = tmp_path / "manual"
    folder.mkdir()
    doc = pymupdf.open()
    for i in range(1, 31):
        doc.new_page().insert_text((72, 72), "limit table" if i == 25 else f"page {i}")
    doc.save(folder / "big.pdf")
    _search(folder, tmp_path, capsys, monkeypatch, "limit")
    html = _view(capsys, "1")

    part = re.search(r'href="(file:[^"#]*/x-[0-9a-f]+\.pdf)#page=(\d+)"', html)
    assert part and part.group(2) == "11"  # pages 15-30 are kept; p.25 is the 11th of them
    assert 'class="whole static-only" href="file:' in html and "big.pdf#page=25" in html
    excerpt = pymupdf.open(next((tmp_path / "cli" / "renditions" / "excerpts").glob("x-*.pdf")))
    assert excerpt.page_count == 16 and excerpt[10].get_label() == "25"  # original numbers kept


def test_excerpt_keeps_cross_reference_links(tmp_path):
    import pymupdf

    from docwhere.rendition import Excerpts

    src = tmp_path / "manual.pdf"
    doc = pymupdf.open()
    for i in range(40):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    rect = pymupdf.Rect(72, 100, 200, 120)
    doc[19].insert_link({"kind": pymupdf.LINK_GOTO, "from": rect, "page": 21, "to": pymupdf.Point(0, 0)})  # p.20 -> p.22
    doc[19].insert_link({"kind": pymupdf.LINK_GOTO, "from": rect + (0, 30, 0, 30), "page": 38, "to": pymupdf.Point(0, 0)})  # -> p.39
    doc.save(src)

    excerpts = Excerpts(tmp_path / "cache")
    path, at = excerpts.link(str(src), 20)
    excerpts.close()
    links = pymupdf.open(path)[at - 1].get_links()
    inside = [link for link in links if link["kind"] == pymupdf.LINK_GOTO]
    assert [link["page"] for link in inside] == [21 - 9]  # p.22 is the 13th page of the excerpt p.10-30
    outside = pymupdf.open(path).xref_object(next(link["xref"] for link in links if link["kind"] != pymupdf.LINK_GOTO))
    assert f"{src.resolve().as_uri()}#page=39" in outside  # p.39 is beyond the excerpt: the full PDF opens there


def test_window_pages_cut_no_excerpts(docs, tmp_path, capsys, monkeypatch):
    from docwhere.index import Index
    from docwhere.viewer import build_html

    hits = _search(docs, tmp_path, capsys, monkeypatch, "config", "--ext", "pdf", "--per-file", "1")
    index = Index(tmp_path / "cli" / "index.db")
    for hit in hits:
        hit["anchor"] = {"page": int(hit["location"].removeprefix("p."))}
        hit["chunk_id"] = hit.pop("id")
    page = build_html(index, ["config"], hits, cut_excerpts=False)
    assert "/x-" not in page and ".pdf#page=" in page  # the whole PDF, its file name percent-encoded
    assert not (tmp_path / "cli" / "renditions" / "excerpts").exists()
