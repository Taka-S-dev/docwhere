"""Evidence as a line of reasoning (--note), and figures given as evidence (--figure)."""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from docwhere.cli import _parser, main
from docwhere.viewer import _ring, figure_near, figures_on


@pytest.fixture(autouse=True)
def no_window(monkeypatch):
    from docwhere import window

    monkeypatch.setattr(window, "show", lambda db, page: True)


@pytest.fixture
def spec(tmp_path) -> Path:
    """Two pages: the text sends the reader to figure 2, a picture with no text of its own."""
    root = tmp_path / "docs"
    root.mkdir()
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 80), "3 通信の設定", fontname="japan", fontsize=16)
    p1.insert_text((72, 120), "失敗したときの再試行は図2の流れに従う。", fontname="japan", fontsize=11)
    p2 = doc.new_page()
    p2.insert_text((72, 80), "4 再試行", fontname="japan", fontsize=16)
    picture = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 30), False)
    picture.clear_with(200)
    p2.insert_image(pymupdf.Rect(110, 120, 410, 280), pixmap=picture)
    p2.draw_rect(pymupdf.Rect(110, 400, 300, 500))  # a second figure, drawn
    p2.insert_text((180, 300), "図2 再試行の流れ（最大3回）", fontname="japan", fontsize=10)
    doc.save(root / "通信仕様.pdf")
    return root


def _view(spec: Path, tmp_path: Path, capsys, monkeypatch, *args) -> str:
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "db" / "index.db"))
    assert main(["search", "再試行", "--in", str(spec), "--json"]) == 0
    capsys.readouterr()
    assert main(["view", "--no-open", *args]) == 0
    return Path(capsys.readouterr().out.strip()).read_text(encoding="utf-8")


def test_quotes_figures_and_notes_keep_their_order():
    args = _parser().parse_args(["view", "--quote", "a", "--note", "says a", "--figure", "図2", "--quote", "b", "--note", "says b"])
    assert args.steps == [("quote", "a", "says a"), ("figure", "図2", ""), ("quote", "b", "says b")]
    assert args.quote == ["a", "b"]
    for bad in (["--note", "x"], ["--quote", "a", "--note", "x", "--note", "y"]):
        with pytest.raises(SystemExit):
            _parser().parse_args(["view", *bad])


def test_figures_are_found_by_layout_and_by_caption(spec):
    with pymupdf.open(spec / "通信仕様.pdf") as doc:
        page = doc[1]
        figures = figures_on(page)
        assert [tuple(round(v) for v in f) for f in figures] == [(110, 125, 410, 275), (110, 400, 300, 500)]  # the picture keeps its 2:1 shape
        figure, caption = figure_near(page, "図2 再試行の流れ")
        assert figure == figures[0] and caption.y0 > figure.y1
        assert figure_near(doc[0], "図2 再試行の流れ") is None  # mentioned in text, but no figure there
    ring = _ring(figures[0])
    assert all(_inside_ellipse(ring, corner) for corner in (figures[0].tl, figures[0].tr, figures[0].bl, figures[0].br))


def _inside_ellipse(box, point) -> bool:
    cx, cy, rx, ry = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2, box.width / 2, box.height / 2
    return ((point.x - cx) / rx) ** 2 + ((point.y - cy) / ry) ** 2 <= 1


def test_notes_turn_the_evidence_into_steps_ending_in_the_answer(spec, tmp_path, capsys, monkeypatch):
    html = _view(spec, tmp_path, capsys, monkeypatch,
                 "--quote", "失敗したときの再試行は図2の流れに従う。", "--note", "本文は再試行を図2に任せている",
                 "--figure", "図2 再試行の流れ", "--note", "図2 に最大3回とある",
                 "--answer", "再試行は最大3回です")
    answer = html[html.index('<section class="answer"'):html.index("</section>")]
    assert "根拠の流れ" in answer
    assert answer.index('<span class="no">1</span>') < answer.index("本文は再試行を図2に任せている") \
        < answer.index('<span class="no">2</span>') < answer.index("図2 に最大3回とある") < answer.index('class="therefore"')
    # each step names the section of its own page, not the hit's
    assert answer.index("3 通信の設定") < answer.index("4 再試行")
    assert "根拠の図（丸で囲んだもの）の拡大" in answer
    assert answer.count('src="data:image/jpeg;base64,') == 2
    rail = html[html.index('<nav class="rail"'):]
    assert "図2 に最大3回とある" in rail  # the side list shows the notes
    # and which document each piece is, and the folder it is in
    assert f'<span class="name">通信仕様.pdf</span><span class="dir">{spec}</span>' in rail


def test_a_figure_by_page_and_number_and_one_that_is_not_there(spec, tmp_path, capsys, monkeypatch):
    html = _view(spec, tmp_path, capsys, monkeypatch, "--figure", "p.2#2", "--figure", "p.2#5")
    answer = html[html.index('<section class="answer"'):html.index("</section>")]
    assert "p.2 の2番目の図" in answer and "資料の中で図を特定できませんでした" in answer
    assert "根拠の流れ" not in answer  # without notes, the plain list as before


def test_wiki_view_shows_notes_as_steps():
    from docwhere.wiki.view import _answer

    evidence = [{"quote": "q", "hit": None, "note": "this says q"}]
    html = _answer("the answer", evidence)
    assert "根拠の流れ" in html and "this says q" in html and 'class="therefore"' in html


def test_a_quote_finds_its_text_whichever_wave_dash_either_uses():
    from docwhere.viewer import _find

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 80), "夜間（0時～5時）は再試行しない。", fontname="japan", fontsize=11)  # full-width tilde
    assert _find(page, ["夜間（0時〜5時）は再試行しない。"])  # quoted with the wave dash
    assert not _find(page, ["夜間（0時〜6時）は再試行しない。"])
