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


def test_a_bookmarked_pdf_leaves_its_running_header_and_footer_out_of_each_page(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    bodies = ["キャベツを洗う", "ニンジンを切る", "タマネギを炒める", "スープを煮る"]
    for body in bodies:
        page = doc.new_page()
        page.insert_htmlbox(pymupdf.Rect(50, 20, 545, 60), "<p>9 野菜の下ごしらえ</p>")  # running header
        page.insert_htmlbox(pymupdf.Rect(50, 300, 545, 340), f"<p>{body}</p>")
        page.insert_htmlbox(pymupdf.Rect(50, 800, 545, 830), "<p>架空の料理本</p>")  # running footer
    doc.set_toc([[1, "9 野菜の下ごしらえ", 1], [2, "9.1 切り方", 2]])
    doc.save(tmp_path / "bm.pdf")

    chunks = extract(tmp_path / "bm.pdf")
    assert [c.lines for c in chunks] == [[b] for b in bodies]
    assert chunks[2].title == "9.1 切り方"  # the header is gone from the text, not from the title


def test_a_line_in_the_middle_of_pages_is_kept_even_when_it_repeats(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    for _ in range(4):
        doc.new_page().insert_htmlbox(pymupdf.Rect(50, 300, 545, 340), "<p>材料を量る</p>")
    doc.set_toc([[1, "1 準備", 1]])
    doc.save(tmp_path / "same.pdf")
    assert [c.lines for c in extract(tmp_path / "same.pdf")] == [["材料を量る"]] * 4


def test_a_word_that_only_sometimes_ends_a_page_is_kept_there(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    for i in range(8):
        page = doc.new_page()
        page.insert_htmlbox(pymupdf.Rect(50, 300, 545, 340), f"<p>手順 {i}</p>")
        page.insert_htmlbox(pymupdf.Rect(50, 800 if i < 3 else 400, 545, 830 if i < 3 else 430), "<p>注意</p>")
    doc.set_toc([[1, "1 準備", 1]])
    doc.save(tmp_path / "word.pdf")
    assert all("注意" in c.lines for c in extract(tmp_path / "word.pdf"))


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


def test_powerpoint_notes_page_without_a_body_is_read(tmp_path):
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "構成"
    slide.placeholders[1].text = "本文"
    body = slide.notes_slide.notes_placeholder
    body._element.getparent().remove(body._element)  # deleted in PowerPoint by the author
    prs.save(tmp_path / "s.pptx")
    assert [(c.title, c.lines) for c in extract(tmp_path / "s.pptx")] == [("構成", ["構成", "本文"])]


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


def _partly_scanned(path):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Chapter one text")
    picture = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8))
    picture.clear_with(200)
    doc.new_page().insert_image(pymupdf.Rect(72, 72, 300, 300), pixmap=picture)  # a scanned page
    doc.new_page()  # a page left blank on purpose: nothing missed there
    doc.save(path)


def test_scanned_pages_of_a_pdf_with_text_are_noted(tmp_path):
    from docwhere.extractors import notes

    path = tmp_path / "mixed.pdf"
    _partly_scanned(path)
    chunks = extract(path)
    assert [c.lines for c in chunks] == [["Chapter one text"]]
    assert notes() == ["1 of 3 pages are pictures without text and cannot be searched (p.2); OCR the file to search them"]
    extract(_plain(tmp_path))
    assert notes() == []  # what was said of one file is not carried to the next


def _plain(tmp_path):
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Only text")
    doc.save(tmp_path / "plain.pdf")
    return tmp_path / "plain.pdf"


def test_a_side_tab_cut_a_character_a_line_is_shown_as_one_line():
    from docwhere.model import display_lines

    lines = ["野菜の下ごしらえ", "料", "理", "編", "1.", "本文の続き"]
    assert display_lines(lines) == [(0, "野菜の下ごしらえ"), (1, "料理編"), (4, "1."), (5, "本文の続き")]
    assert display_lines(["1.", "手順", "2.", "手順"]) == [(0, "1."), (1, "手順"), (2, "2."), (3, "手順")]  # short, but apart


def _table_manual(path, pages=8, front=1):
    """A bookmarked manual as they are laid out: a chapter tab in the margin, the page number
    in the footer, a table of marks drawn with a private glyph, and a sentence wrapped at the
    right edge."""
    import pymupdf

    doc = pymupdf.open()
    for i in range(1, pages + 1):
        page = doc.new_page()
        for k, letter in enumerate("参考欄"):
            page.insert_text((570, 300 + 20 * k), letter, fontsize=14, fontname="japan")  # chapter tab
        page.insert_text((60, 100), "項目", fontsize=8, fontname="japan")
        page.insert_text((300, 100), "内容", fontsize=8, fontname="japan")
        page.insert_text((60, 115), "登録できるグループ数", fontsize=8, fontname="japan")
        page.insert_text((300, 115), str(500 + i), fontsize=8, fontname="japan")
        wide = "あ" * 52  # reaches the right edge of the text, so it was wrapped there
        page.insert_text((60, 200), f"大鍋の野菜をゆっくり煮込んで{wide}"[:54], fontsize=9, fontname="japan")
        page.insert_text((60, 212), "す。", fontsize=9, fontname="japan")
        page.insert_text((60, 240), "短い行", fontsize=9, fontname="japan")
        page.insert_text((60, 252), "次の行", fontsize=9, fontname="japan")
        if i in (1, 4, 7):
            page.insert_text((60, 60), "下ごしらえと保存のしかた", fontsize=9, fontname="japan")  # a heading, pages apart
        page.insert_text((40, 815), str(i - front) if i > front else "", fontsize=14, fontname="japan")
        page.insert_text((80, 815), "3 設定の仕様", fontsize=8, fontname="japan")  # running footer
    doc.set_toc([[1, "3 設定の仕様", 1]])
    doc.save(path)


def test_a_bookmarked_pdf_reads_rows_wrapped_sentences_and_no_page_furniture(tmp_path):
    _table_manual(tmp_path / "t.pdf")
    chunks = extract(tmp_path / "t.pdf")
    lines = chunks[3].lines
    assert "項目 | 内容" in lines and "登録できるグループ数 | 504" in lines  # a row is one line, value beside its name
    assert any(line.startswith("大鍋の野菜をゆっくり煮込んで") and line.endswith("す。") for line in lines)  # the wrap is undone
    assert "短い行" in lines and "次の行" in lines  # short lines are lines of their own
    assert "下ごしらえと保存のしかた" in lines  # opens three pages far apart: a heading, not a running header
    text = "\n".join(lines)
    assert "3 設定の仕様" not in text and "第" not in text  # the footer and the chapter tab are left out
    assert "3" not in lines  # ...and so is the page's printed number


def test_a_glyph_is_read_as_the_mark_it_draws():
    from docwhere.extractors.pdf import _mark_drawn

    def drawn(rows):
        size = len(rows)
        return _mark_drawn(bytes(0 if c == "#" else 255 for row in rows for c in row), size, size, size)

    n = 20
    ring = ["".join("#" if 7.5 <= ((x - 9.5) ** 2 + (y - 9.5) ** 2) ** 0.5 <= 9.9 else "." for x in range(n)) for y in range(n)]
    disc = ["".join("#" if ((x - 9.5) ** 2 + (y - 9.5) ** 2) ** 0.5 <= 9.9 else "." for x in range(n)) for y in range(n)]
    cross = ["".join("#" if abs(x - y) <= 1 or abs(x + y - (n - 1)) <= 1 else "." for x in range(n)) for y in range(n)]
    bar = ["".join("#" if 9 <= y <= 10 else "." for x in range(n)) for y in range(n)]
    triangle = ["".join("#" if y == n - 1 or abs(abs(x - 9.5) - y / 2) <= 0.8 else "." for x in range(n)) for y in range(n)]
    hand = ["".join("#" if (x * 7 + y * 3) % 11 == 0 else "." for x in range(n)) for y in range(n)]
    assert [drawn(g) for g in (ring, disc, cross, bar, triangle)] == ["○", "●", "×", "―", "△"]
    assert drawn(hand) == ""  # anything else is not guessed at


def test_dashes_and_the_wave_dash_are_one_form_each():
    from docwhere.model import norm

    assert norm("400〜499") == norm("400~499") == norm("400～499")
    assert norm("Wi‐Fi") == norm("Wi–Fi") == "Wi-Fi"
    assert norm("サーバー") == "サーバー"  # the long vowel mark is a letter


def test_utf16_text_is_read_and_a_csv_row_carries_its_header(tmp_path):
    (tmp_path / "bom.txt").write_bytes("設定\ntimeout = 30\n".encode("utf-16"))
    (tmp_path / "nobom.log").write_bytes("connect timeout = 30\nretry = 5\n".encode("utf-16-le"))
    assert extract(tmp_path / "bom.txt")[0].lines == ["設定", "timeout = 30"]
    assert extract(tmp_path / "nobom.log")[0].lines == ["connect timeout = 30", "retry = 5"]
    (tmp_path / "s.csv").write_text('項目,既定値\n接続タイムアウト,30秒\n"再送, 回数",5\n', encoding="utf-8-sig")
    [chunk] = extract(tmp_path / "s.csv")
    assert chunk.lines == ["項目: 接続タイムアウト / 既定値: 30秒", "項目: 再送, 回数 / 既定値: 5"]
    assert chunk.anchors == [{"line": 2}, {"line": 3}]
    (tmp_path / "plain.csv").write_text("a,b\n", encoding="utf-8")  # one row: nothing to label
    assert extract(tmp_path / "plain.csv")[0].lines == ["a / b"]


def test_word_text_under_review_in_a_form_field_and_in_a_text_box_is_read(tmp_path):
    from docx import Document
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    w = nsdecls("w")
    doc = Document()
    doc.add_heading("仕様", 1)
    p = doc.add_paragraph("再送回数は")
    # a tracked insertion, a tracked deletion and an inline content control in one paragraph
    p._p.append(parse_xml(f'<w:ins {w} w:id="1" w:author="a"><w:r><w:t>最大5回</w:t></w:r></w:ins>'))
    p._p.append(parse_xml(f'<w:del {w} w:id="2" w:author="a"><w:r><w:delText>最大3回</w:delText></w:r></w:del>'))
    p._p.append(parse_xml(f'<w:sdt {w}><w:sdtContent><w:r><w:t>とする。</w:t></w:r></w:sdtContent></w:sdt>'))
    # a block-level content control holding a paragraph of its own
    doc.element.body.insert(2, parse_xml(
        f'<w:sdt {w}><w:sdtContent><w:p><w:r><w:t>承認者: 山田</w:t></w:r></w:p></w:sdtContent></w:sdt>'))
    # a text box anchored in a paragraph, stored as a drawing and again as its older form
    box = '<w:txbxContent><w:p><w:r><w:t>注意: 電源を切らない</w:t></w:r></w:p></w:txbxContent>'
    mc = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
    host = doc.add_paragraph("図の説明")
    host._p.append(parse_xml(
        f'<w:r {w} {mc}><mc:AlternateContent><mc:Choice Requires="wps">{box}</mc:Choice>'
        f'<mc:Fallback>{box}</mc:Fallback></mc:AlternateContent></w:r>'))
    doc.save(tmp_path / "review.docx")

    lines = [line for chunk in extract(tmp_path / "review.docx") for line in chunk.lines]
    assert "承認者: 山田" in lines
    assert "再送回数は最大5回とする。" in lines  # the insertion and the field, not the deleted text
    assert lines.count("〔テキストボックス〕 注意: 電源を切らない") == 1 and "図の説明" in lines


def _manual_with_pictures(path, pages=12):
    """A manual: a letterhead on every page, a screen pasted into page 3, an icon on page 4, and
    page 5 a scan that was given a text layer."""
    import pymupdf

    def picture(shade):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8))
        pix.clear_with(shade)
        return pix

    doc = pymupdf.open()
    for no in range(1, pages + 1):
        page = doc.new_page()
        page.insert_image(pymupdf.Rect(50, 20, 250, 70), pixmap=picture(120))  # the letterhead
        page.insert_text((72, 120), f"Section {no} is explained in the table below")
    doc[2].insert_image(pymupdf.Rect(72, 200, 372, 500), pixmap=picture(200))
    doc[3].insert_image(pymupdf.Rect(72, 200, 102, 230), pixmap=picture(200))
    doc[4].insert_image(pymupdf.Rect(72, 200, 372, 500), pixmap=picture(240))
    for row in range(6):
        doc[4].insert_text((80, 230 + row * 30), "E2041 the unit did not answer in time")
    doc.save(path)


def test_the_pictures_whose_words_are_not_read_are_counted_page_by_page(tmp_path):
    from docwhere.extractors import pictures

    path = tmp_path / "manual.pdf"
    _manual_with_pictures(path)
    extract(path)
    # not the letterhead (every page), not the icon (small), not the scan (its words are text)
    assert pictures() == {3: (1, 0.18)}
    extract(_plain(tmp_path))
    assert pictures() == {}  # what was counted in one file is not carried to the next
