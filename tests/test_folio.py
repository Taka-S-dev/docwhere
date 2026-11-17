import pymupdf
import pytest

from docwhere.folio import printed, printed_offset
from docwhere.window import _outline_trail


def _manual(path, pages=40, front=2):
    """Pages whose footer prints a number ``front`` lower than the page index, like a manual
    whose numbering starts after the cover and contents."""
    doc = pymupdf.open()
    for i in range(1, pages + 1):
        page = doc.new_page()
        page.insert_text((72, 100), f"body text 127 page reference {i}")
        if i > front:
            page.insert_text((500, 820), str(i - front))  # folio in the bottom margin
    doc.set_toc([[1, "1 Intro", 3], [1, "2 Setup", 10], [2, "2.1 Install", 12], [2, "2.2 Config", 20], [1, "3 Use", 30]])
    doc.save(path)
    return pymupdf.open(path)


def test_offset_is_read_from_the_footers(tmp_path):
    doc = _manual(tmp_path / "m.pdf")
    assert printed_offset(doc) == 2
    assert printed(2, 25) == 23 and printed(2, 2) is None and printed(None, 25) is None


def test_no_folio_or_trivial_labels_mean_nothing_to_show(tmp_path):
    doc = pymupdf.open()
    for i in range(10):
        doc.new_page().insert_text((72, 100), f"text {i}")
    doc.set_page_labels([{"startpage": 0, "prefix": "", "style": "D", "firstpagenum": 1}])  # restates the index
    assert printed_offset(doc) is None


def test_outline_trail_holds_the_sections_around_a_page(tmp_path):
    toc = [[lvl, title, page] for lvl, title, page, *_ in _manual(tmp_path / "m.pdf").get_toc()]
    assert _outline_trail(toc, 15) == [["2 Setup", 10], ["2.1 Install", 12]]
    assert _outline_trail(toc, 31) == [["3 Use", 30]]
    assert _outline_trail(toc, 1) == []


def test_offset_of_a_file_is_kept_until_the_file_changes(tmp_path, monkeypatch):
    import docwhere.folio as folio

    pdf, cache = tmp_path / "m.pdf", tmp_path / "folios.json"
    _manual(pdf).close()
    assert folio.offset_of(pdf, cache) == 2
    assert folio.offset_of(tmp_path / "missing.pdf", cache) is None

    # another command: nothing remembered in the process, the file on disk answers
    folio._OFFSETS.clear()
    monkeypatch.setattr(folio, "printed_offset", lambda doc: pytest.fail("read the PDF again"))
    assert folio.offset_of(pdf, cache) == 2

    # a replaced PDF is read again, and a broken cache file is passed over
    monkeypatch.setattr(folio, "printed_offset", lambda doc: 5)
    _manual(pdf, pages=30).close()
    cache.write_text("not json", encoding="utf-8")
    assert folio.offset_of(pdf, cache) == 5


def test_search_and_show_give_the_printed_page_beside_the_index(tmp_path, capsys):
    import json

    from docwhere.cli import main

    folder, db = tmp_path / "docs", str(tmp_path / "index" / "index.db")
    folder.mkdir()
    _manual(folder / "m.pdf").close()
    assert main(["--db", db, "search", "Install", "--in", str(folder), "--json"]) == 0
    hit = json.loads(capsys.readouterr().out)["hits"][0]
    assert (hit["location"], hit["printed_page"]) == ("p.12", 10)  # location keeps the PDF's index
    assert main(["--db", db, "search", "Install", "--in", str(folder)]) == 0
    assert "p.12（印刷 10）" in capsys.readouterr().out
    assert main(["--db", db, "show", "--id", str(hit["id"])]) == 0
    assert "[p.12（印刷 10）]" in capsys.readouterr().out
