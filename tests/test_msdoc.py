"""Reading .doc without Word (extractors.msdoc). Where Word is installed, documents Word makes are
read both ways and compared; the rest runs anywhere."""

import json
import sys
from pathlib import Path

import pytest

from docwhere import doccheck, extractors
from docwhere.extractors import DOC_READER_ENV, is_serial, msdoc, release
from docwhere.extractors import legacy as legacy_module
from docwhere.model import Chunk


# ---- anywhere ----------------------------------------------------------------------------------

def test_a_file_that_is_not_a_word_97_document_is_left_to_word(tmp_path):
    cases = {
        "rtf.doc": b"{\\rtf1\\ansi Some text saved as RTF under a .doc name}",
        "html.doc": "<html><body>表</body></html>".encode("utf-8"),
        "cut.doc": bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 100,  # the signature and nothing after it
        "empty.doc": b"",
    }
    for name, data in cases.items():
        (tmp_path / name).write_bytes(data)
        with pytest.raises(msdoc.Unsupported):
            msdoc.extract(tmp_path / name)


def _blanked(text: str) -> str:
    return msdoc._Document._without_codes(text, []).replace("\0", "")


def test_a_fields_code_is_left_out_and_its_result_kept():
    assert _blanked("前\x13 PAGE \x143\x15後") == "前3後"
    assert _blanked("索引\x13 XE \"語\" \x15の後") == "索引の後"  # a field without a result
    # a field inside another's result: its code out, its result kept, as text in a .docx
    assert _blanked("\x13 HYPERLINK \"x\" \x14リンク\x13 PAGE \x142\x15先\x15") == "リンク2先"
    # a field inside another's code, as in an IF: all of it code
    assert _blanked("\x13 IF \x13 PAGE \x141\x15 = 1 \"はい\" \x14はい\x15") == "はい"
    with pytest.raises(msdoc.Unsupported):
        _blanked("\x13 PAGE \x14開いたまま")


def test_ruby_is_read_as_word_writes_it_in_a_docx():
    word_ruby = '\x13EQ \\* jc2 \\* "Font:游明朝" \\* hps11 \\o\\ad(\\s\\up 10(なべ),鍋)\x15と包丁'
    assert _blanked(word_ruby) == "なべ鍋と包丁"
    # written by hand, without the switches Word's own ruby has: a field in the .docx, no text
    assert _blanked("\x13 eq \\o\\ad(\\s\\up 9(フリガナ),氏名)\x15欄") == "欄"
    # combined characters (組み文字) and enclosed ones (囲い文字) are fields there too
    assert _blanked("\x13eq \\o(\\s\\up 6(組　),\\s\\do 2(　み))\x15文字") == "文字"
    assert _blanked("\x13 EQ \\o\\ac(○,済) \x15") == ""
    with pytest.raises(msdoc.Unsupported):  # some of Word's switches, not all: not guessed at
        _blanked('\x13EQ \\* jc2 \\o(\\s\\up 10(なべ),鍋)\x15')


def test_property_lists_of_variable_length_are_walked_whole():
    table = bytes([0x08, 0xD6]) + (5).to_bytes(2, "little") + bytes([1, 0, 0, 0x10]) + bytes([0x16, 0x24, 1])
    assert [s for s, _ in msdoc._sprm_list(table)] == [0xD608, 0x2416]  # the table definition's size counted right
    tabs = bytes([0x15, 0xC6, 255, 1, 1, 0, 2, 0, 1, 3, 0, 0]) + bytes([0x16, 0x24, 1])
    assert [s for s, _ in msdoc._sprm_list(tabs)] == [0xC615, 0x2416]
    with pytest.raises(msdoc.Unsupported):
        msdoc._sprm_list(bytes([0x0B, 0x46, 1]))  # a two-byte operand cut short


def test_the_setting_decides_who_reads_a_doc_and_in_which_worker(tmp_path, monkeypatch):
    doc = tmp_path / "a.doc"
    doc.write_bytes(b"")
    by_word = [Chunk("Word", "", ["read by Word"], [{}])]
    monkeypatch.setattr(legacy_module, "doc_through_word", lambda path: by_word)

    monkeypatch.setenv(DOC_READER_ENV, "word")
    assert is_serial(doc)  # one at a time, in Office's worker, as before
    assert legacy_module.extract_doc(doc) == by_word

    monkeypatch.setenv(DOC_READER_ENV, "fast")
    assert not is_serial(doc)  # alongside the other files
    own = [Chunk("own", "", ["read without Word"], [{}])]
    monkeypatch.setattr(msdoc, "extract", lambda path: own)
    assert legacy_module.extract_doc(doc) == own

    def refuse(path):
        raise msdoc.Unsupported("tracked deletions")

    monkeypatch.setattr(msdoc, "extract", refuse)
    extractors.set_worker("pooled")
    try:
        with pytest.raises(extractors.NeedsApplication):  # handed on to Word's worker
            legacy_module.extract_doc(doc)
    finally:
        extractors.set_worker(None)
    assert legacy_module.extract_doc(doc) == by_word  # Word's worker reads it

    def crash(path):
        raise IndexError("a fault in the reader")

    monkeypatch.setattr(msdoc, "extract", crash)
    assert legacy_module.extract_doc(doc) == by_word  # a fault costs speed, never the file


def test_doc_reader_is_word_until_turned_on(tmp_path, monkeypatch, capsys):
    from docwhere.cli import main

    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "index" / "index.db"))
    assert main(["doc-reader"]) == 0
    assert "doc-reader: office" in capsys.readouterr().out
    assert main(["doc-reader", "fast"]) == 0
    assert "doc-reader: fast" in capsys.readouterr().out
    settings = json.loads((tmp_path / "index" / "settings.json").read_text(encoding="utf-8"))
    assert settings["doc_reader"] == "fast"
    assert main(["doc-reader", "office"]) == 0 and "doc-reader: office" in capsys.readouterr().out


def test_doc_check_takes_files_spread_over_all_and_skips_words_lock_files(tmp_path):
    for name in ("a.doc", "b.DOC", "~$a.doc", "c.docx", "sub/d.doc"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_bytes(b"")
    assert [p.name for p in doccheck.doc_files([tmp_path])] == ["a.doc", "b.DOC", "d.doc"]
    paths = [Path(f"{i:03d}.doc") for i in range(100)]
    picked = doccheck.sample(paths, 4)
    assert [p.name for p in picked] == ["000.doc", "025.doc", "050.doc", "075.doc"]
    assert doccheck.sample(paths, 0) == paths


# ---- with Word ---------------------------------------------------------------------------------

H1, H2, NORMAL = -2, -3, -1


@pytest.fixture(scope="module")
def made(tmp_path_factory) -> Path:
    """.doc files made by Word, of the kinds this reader must read as Word does, and one it
    must leave to Word. Made-up cooking text."""
    if sys.platform != "win32":
        pytest.skip("needs Word")
    try:
        import win32com.client

        word = win32com.client.DispatchEx("Word.Application")
    except Exception:
        pytest.skip("Word not available")
    out = tmp_path_factory.mktemp("made")
    word.Visible = False
    word.DisplayAlerts = 0

    def add(doc, text, style=NORMAL):
        end = doc.Content.End - 1
        doc.Range(end, end).InsertAfter(text)
        doc.Range(doc.Content.End - 1, doc.Content.End - 1).InsertParagraphAfter()
        doc.Paragraphs(doc.Paragraphs.Count - 1).Style = style
        return doc.Paragraphs(doc.Paragraphs.Count - 1).Range

    def table(doc, rows, at=None):
        if at is None:
            end = doc.Content.End - 1
            at = doc.Range(end, end)
        t = doc.Tables.Add(at, len(rows), len(rows[0]))
        for i, row in enumerate(rows, 1):
            for j, text in enumerate(row, 1):
                t.Cell(i, j).Range.Text = text
        return t

    def save(doc, name):
        doc.SaveAs2(str(out / name), FileFormat=0)
        doc.Close(0)

    try:
        doc = word.Documents.Add()
        add(doc, "献立", H1)
        add(doc, "下ごしらえの説明。\tタブと" + chr(11) + "改行。English text too.")
        add(doc, "番号付きの見出し", H2).ListFormat.ApplyOutlineNumberDefault()
        p = add(doc, "注のある文。")
        doc.Footnotes.Add(doc.Range(p.End - 2, p.End - 2), Text="注の本文は読まない。")
        p = add(doc, "鍋と包丁")
        doc.Range(p.Start, p.Start + 1).PhoneticGuide(Text="なべ")
        p = add(doc, "記号の前")
        doc.Range(p.End - 1, p.End - 1).InsertSymbol(74, "Wingdings", False)
        add(doc, "𠮷野家の𩸽と🍳の後ろの文。")  # two cps each in a .doc
        add(doc, "材料", H1)
        t = table(doc, [["区分", "料理", "時間"], ["和食", "煮物", "20分"], ["", "焼き魚", "10分"], ["洋食", "グラタン", "15分"]])
        t.Cell(2, 1).Merge(t.Cell(3, 1))  # 和食 over two rows: its text on both, as in a .docx
        add(doc, "")
        p = add(doc, "箱のある段落。")
        box = doc.Shapes.AddTextbox(1, 100, 100, 200, 50, p)
        box.TextFrame.TextRange.Text = "箱の中の文\r二段落目"
        p = add(doc, "つながった箱の段落。")
        first = doc.Shapes.AddTextbox(1, 100, 200, 100, 40, p)
        second = doc.Shapes.AddTextbox(1, 250, 200, 100, 40, p)
        first.TextFrame.Next = second.TextFrame
        first.TextFrame.TextRange.Text = "次の箱へ流れる文。" * 4
        save(doc, "read.doc")

        doc = word.Documents.Add()
        add(doc, "入れ子", H1)
        outer = table(doc, [["外", "側"], ["a", "b"]])
        middle = table(doc, [["中1", "中2"], ["中3", "中4"]], outer.Cell(2, 2).Range)
        table(doc, [["奥1", "奥2"]], middle.Cell(2, 2).Range)
        add(doc, "表の後。")
        save(doc, "nested.doc")

        doc = word.Documents.Add()
        add(doc, "変更履歴", H1)
        p = add(doc, "残る文と消す文。")
        add(doc, "丸ごと消す段落。")
        t = table(doc, [["材料", "分量"], ["塩", "少々"], ["砂糖", "大さじ1"]])
        add(doc, "")
        doc.TrackRevisions = True
        doc.Range(p.Start + 4, p.Start + 7).Delete()
        doc.Paragraphs(3).Range.Delete()
        t.Rows(2).Delete()
        doc.Range(doc.Content.End - 1, doc.Content.End - 1).InsertAfter("足した文。")
        save(doc, "tracked.doc")

        doc = word.Documents.Add()
        p = add(doc, "ルビの一部だけの指定: ")
        doc.Fields.Add(doc.Range(p.End - 1, p.End - 1), -1, "EQ \\* jc2 \\o\\ad(\\s\\up 10(なべ),鍋)", False)
        save(doc, "partial_ruby.doc")
    finally:
        word.Quit()
    return out


def _flat(chunks):
    return [(c.title, c.crumb, c.lines) for c in chunks]


@pytest.mark.parametrize("name", ["read.doc", "nested.doc", "tracked.doc"])
def test_a_doc_is_read_without_word_as_word_reads_it(made, name):
    try:
        by_word = legacy_module.doc_through_word(made / name)
    finally:
        release()
    assert _flat(msdoc.extract(made / name)) == _flat(by_word)


def test_what_the_reader_reads_as_word_does(made):
    lines = [line for c in msdoc.extract(made / "read.doc") for line in c.lines]
    assert "なべ鍋と包丁" in lines  # ruby, as Word writes it in a .docx
    assert "𠮷野家の𩸽と🍳の後ろの文。" in lines  # and the text after such characters stays in place
    assert "区分: 和食 / 料理: 焼き魚 / 時間: 10分" in lines  # a merged cell's text on each of its rows
    assert "〔テキストボックス〕 箱の中の文 二段落目" in lines
    assert sum("次の箱へ流れる文" in line for line in lines) == 1  # a chain of boxes once
    assert not any("注の本文" in line for line in lines)
    nested = [line for c in msdoc.extract(made / "nested.doc") for line in c.lines]
    assert any("中1" in line and "奥2" in line for line in nested)  # inner tables read inside the outer cell
    tracked = [line for c in msdoc.extract(made / "tracked.doc") for line in c.lines]
    assert "残る文と。" in tracked and not any("丸ごと消す" in line for line in tracked)
    assert any("足した文" in line for line in tracked) and not any("塩" in line for line in tracked)


def test_what_word_would_read_otherwise_is_left_to_word(made):
    with pytest.raises(msdoc.Unsupported, match="ruby"):
        msdoc.extract(made / "partial_ruby.doc")


def test_doc_check_reports_and_can_turn_the_reader_on(made, tmp_path, monkeypatch, capsys):
    from docwhere.cli import main

    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "index" / "index.db"))
    assert main(["doc-check", str(made), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["checked"], out["same"], out["differ"], out["office_failed"]) == (4, 3, [], [])
    assert sum(out["to_office"].values()) == 1 and not out["enabled"]
    assert main(["doc-check", str(made), "--enable"]) == 0
    assert "now read without Office" in capsys.readouterr().out
    assert main(["doc-reader"]) == 0 and "doc-reader: fast" in capsys.readouterr().out


def test_an_index_reads_doc_without_word_when_turned_on(made, tmp_path, monkeypatch):
    from docwhere.index import Index
    from docwhere.settings import save_settings

    monkeypatch.setenv(DOC_READER_ENV, "word")  # put back after the test, which the index changes
    contents = {}
    for mode in ("office", "fast"):
        db = tmp_path / mode / "index.db"
        save_settings(db, doc_reader=mode)
        index = Index(db)
        stats = index.sync(made)
        assert stats.failed == 0
        contents[mode] = sorted(tuple(r) for r in index.db.execute(
            "SELECT f.path, c.title, c.body FROM chunks c JOIN files f ON f.id = c.file_id"))
        index.close()
    assert contents["fast"] == contents["office"]  # the one left to Word, Word read


def test_a_piece_of_text_whose_attached_properties_change_the_text_is_left_to_word():
    rsid = bytes([0x16, 0x68]) + b"\x01\x02\x03\x04"  # sprmCRsidText: who typed it, not what it is
    deleted = bytes([0x00, 0x08, 1])
    msdoc._check_piece_properties(0, [])
    msdoc._check_piece_properties(1 | 0 << 1, [rsid])  # Prm1 naming a list of harmless properties
    with pytest.raises(msdoc.Unsupported):
        msdoc._check_piece_properties(1 | 1 << 1, [rsid, deleted])
    with pytest.raises(msdoc.Unsupported):
        msdoc._check_piece_properties(1 | 5 << 1, [rsid])  # a list that is not there
    with pytest.raises(msdoc.Unsupported):
        msdoc._check_piece_properties(0x22 << 1, [])  # Prm0: one property this reader does not map


def test_crafted_fields_end_quickly():
    import time

    with pytest.raises(msdoc.Unsupported, match="nested deeper"):
        _blanked("\x13" * 10000 + "x" + "\x15" * 10000)
    ruby = '\x13EQ \\* jc2 \\* "Font:x" \\* hps11 \\o\\ad(\\s\\up 10(な),鍋)\x15'
    started = time.perf_counter()
    assert _blanked((ruby + "と") * 20000) == "な鍋と" * 20000  # rebuilt in one pass
    assert time.perf_counter() - started < 5
