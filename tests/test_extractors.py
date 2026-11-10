import pytest

from docwhere.extractors import extract
from docwhere.model import norm


def test_norm_strips_terminal_control_and_bidi_characters():
    assert norm("a\x1b[31mb‮c​d") == "a [31mbcd"
    assert norm("line\x0bbreak\tand　space") == "line break and space"  # Word's vertical tab is a break


def test_zip_bomb_is_rejected_before_parsing(docs, monkeypatch):
    from docwhere import extractors

    monkeypatch.setattr(extractors, "MAX_UNZIPPED_BYTES", 1000)
    with pytest.raises(ValueError, match="limit"):
        extract(docs / "運用マニュアル.docx")


def test_word_sections_follow_headings(docs):
    chunks = extract(docs / "運用マニュアル.docx")
    crumbs = [c.crumb for c in chunks]
    assert crumbs == ["はじめに", "コマンド", "コマンド > config コマンド", "コマンド > backup コマンド"]
    config = chunks[2]
    assert config.title == "config コマンド"
    assert config.lines[-1] == "オプション: --force / 説明: 検証エラーを無視する"  # table read in place
    assert "オプション / 説明" not in config.lines  # the header row is in every labelled row already


def test_table_rows_are_labelled_by_the_header():
    from docwhere.model import table_lines

    rows = [["項目", "既定値", "範囲"], ["接続タイムアウト", "30秒", "1-300"], ["再送回数", "3", ""]]
    assert table_lines(rows) == ["項目: 接続タイムアウト / 既定値: 30秒 / 範囲: 1-300", "項目: 再送回数 / 既定値: 3"]
    unlabelled = table_lines([["注意", "本手順は管理者権限が必要です。実行前に必ずバックアップを取得してください。"], ["a", "b"]])
    assert unlabelled[0].startswith("注意 / ") and unlabelled[1] == "a / b"  # a first row that is not labels stays
    assert table_lines([["枠で囲んだ注意書き"]]) == ["枠で囲んだ注意書き"]  # a one-cell box, not a table of values


def test_word_table_keeps_vertically_merged_value_on_every_row(tmp_path):
    from docx import Document

    doc = Document()
    table = doc.add_table(rows=3, cols=3)
    for r, cells in enumerate((["区分", "項目", "既定値"], ["通信", "接続タイムアウト", "30秒"], ["", "再送回数", "3"])):
        for c, text in enumerate(cells):
            table.cell(r, c).text = text
    table.cell(1, 0).merge(table.cell(2, 0))
    doc.save(tmp_path / "t.docx")
    (chunk,) = extract(tmp_path / "t.docx")
    assert chunk.lines == ["区分: 通信 / 項目: 接続タイムアウト / 既定値: 30秒", "区分: 通信 / 項目: 再送回数 / 既定値: 3"]


def test_pdf_pages_take_bookmark_titles(docs):
    chunks = extract(docs / "sub" / "設計書.pdf")
    assert [c.anchors for c in chunks] == [[{"page": 1}], [{"page": 2}], [{"page": 3}]]
    assert chunks[1].title == "2 CLI / 2.1 config"
    assert chunks[2].title == "2.1 config"  # page inside the section inherits it
    assert chunks[2].crumb == "2 CLI > 2.1 config"


def test_excel_lines_are_rows_led_by_the_header(docs):
    (chunk,) = extract(docs / "コマンド一覧.xlsx")
    assert chunk.title == "CLI"
    assert chunk.lines[0] == "コマンド\t概要"
    assert chunk.anchors[0]["header"] is True
    assert chunk.lines[1] == "config\t設定を検証する"
    assert chunk.anchors[1] == {"sheet": "CLI", "row": 2, "cols": ["A", "B"]}


def test_excel_title_row_before_header_and_header_repeated_per_chunk(tmp_path, monkeypatch):
    from openpyxl import Workbook

    from docwhere.extractors import excel

    wb = Workbook()
    ws = wb.active
    ws.append(["エラーコード一覧"])
    ws.append(["コード", "対処"])
    for i in range(5):
        ws.append([f"E{i}", f"手順{i}"])
    wb.save(tmp_path / "t.xlsx")
    monkeypatch.setattr(excel, "ROWS_PER_CHUNK", 4)
    chunks = extract(tmp_path / "t.xlsx")
    assert chunks[0].lines[:2] == ["エラーコード一覧", "コード\t対処"]
    assert all(c.lines[c.anchors.index(next(a for a in c.anchors if a.get("header")))] == "コード\t対処" for c in chunks)


def test_pdf_without_bookmarks_gets_headings_from_layout(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_htmlbox(
        pymupdf.Rect(50, 50, 545, 790),
        "<h1>第1章 概要</h1><p>本書は製品の概要を述べる。</p>"
        "<h2>1.2 インストール</h2><p>setup.exe を実行する。</p>",
    )
    doc.new_page().insert_htmlbox(pymupdf.Rect(50, 50, 545, 790), "<p>続き: 再起動が必要。</p><h2>1.3 設定</h2><p>config を編集。</p>")
    doc.save(tmp_path / "nobm.pdf")

    chunks = extract(tmp_path / "nobm.pdf")
    crumbs = [c.crumb.replace(" ", "") for c in chunks]  # the HTML renderer may drop the space after 第1章
    assert crumbs == ["第1章概要", "第1章概要>1.2インストール", "第1章概要>1.3設定"]
    install = chunks[1]
    assert {a["page"] for a in install.anchors} == {1, 2}  # the section runs onto page 2


def test_powerpoint_includes_notes_and_normalises_width(docs):
    chunks = extract(docs / "説明会.pptx")
    assert [c.title for c in chunks] == ["概要", "config の使い方"]
    assert "質疑で --force の説明をする" in chunks[1].lines
    assert "ハンカク カナ" in chunks[0].lines


def test_markdown_sections_and_line_numbers(docs):
    chunks = extract(docs / "notes.md")
    assert chunks[1].crumb == "Intro > deploy コマンド"
    assert chunks[1].anchors[1] == {"line": 5}


def test_word_cell_text_is_capped_and_deep_tables_skipped(tmp_path, monkeypatch):
    from docx import Document

    from docwhere.extractors import word

    monkeypatch.setattr(word, "MAX_CELL_CHARS", 50)
    doc = Document()
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "name", "value"
    table.cell(1, 0).text = "big"
    table.cell(1, 1).text = "x" * 500
    inner = table.cell(1, 0).add_table(rows=1, cols=1)
    inner.cell(0, 0).add_table(rows=1, cols=1).cell(0, 0).add_table(rows=1, cols=1).cell(0, 0).text = "too deep"
    doc.save(tmp_path / "t.docx")
    (chunk,) = extract(tmp_path / "t.docx")
    row = chunk.lines[-1]
    assert "x" * 50 in row and "x" * 51 not in row  # the long cell is cut at the cap
    assert "too deep" not in row  # the fourth level of nesting is not read


def test_word_document_text_past_the_limit_is_refused(docs, monkeypatch):
    from docwhere.extractors import word

    monkeypatch.setattr(word, "MAX_DOCUMENT_CHARS", 20)
    with pytest.raises(ValueError, match="limit"):
        extract(docs / "運用マニュアル.docx")


def test_word_merged_cell_cannot_outgrow_the_budget_while_a_table_is_built(tmp_path, monkeypatch):
    from docx import Document

    from docwhere.extractors import word

    doc = Document()
    table = doc.add_table(rows=200, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "name", "value"
    table.cell(1, 0).text = "y" * 1000
    table.cell(1, 0).merge(table.cell(199, 0))  # one cell, repeated on 199 rows
    doc.save(tmp_path / "t.docx")
    monkeypatch.setattr(word, "MAX_DOCUMENT_CHARS", 50_000)
    built = []
    real = word.table_lines
    monkeypatch.setattr(word, "table_lines", lambda rows: built.append(len(rows)) or real(rows))
    with pytest.raises(ValueError, match="limit"):
        extract(tmp_path / "t.docx")
    assert built == []  # refused while the rows were being read, before the table was assembled
