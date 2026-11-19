import pytest

from docwhere import opener, window


@pytest.fixture
def launched(monkeypatch):
    calls = []
    monkeypatch.setattr(opener.subprocess, "Popen", lambda args: calls.append(args))
    monkeypatch.setattr(opener, "start_file", lambda path: calls.append(["default", path]))
    return calls


@pytest.fixture
def program(tmp_path):
    exe = tmp_path / "SumatraPDF.exe"  # stands in for a portable viewer
    exe.write_bytes(b"")
    return str(exe)


def test_configured_program_gets_the_page_and_file(launched, program, monkeypatch):
    monkeypatch.setattr(opener, "_acrobat", lambda: r"C:\Acrobat\Acrobat.exe")
    command = [program, "-reuse-instance", "-page", "{page}", "{file}"]
    message = opener._pdf(r"C:\docs\m.pdf", {"page": 120}, {"command": command})
    assert launched == [[program, "-reuse-instance", "-page", "120", r"C:\docs\m.pdf"]]
    assert "SumatraPDF" in message


def test_default_is_acrobat_then_the_default_app(launched, monkeypatch):
    monkeypatch.setattr(opener, "_acrobat", lambda: r"C:\Acrobat\Acrobat.exe")
    opener._pdf("m.pdf", {"page": 2})
    monkeypatch.setattr(opener, "_acrobat", lambda: None)
    opener._pdf("m.pdf", {"page": 2})
    assert launched == [[r"C:\Acrobat\Acrobat.exe", "/A", "page=2", "m.pdf"], ["default", "m.pdf"]]


def test_missing_program_falls_back_to_the_default_app(launched, tmp_path):
    message = opener._pdf("m.pdf", {"page": 7}, {"command": [str(tmp_path / "gone.exe"), "{file}"]})
    assert launched == [["default", "m.pdf"]] and "not found" in message


def test_pdf_app_setting_is_validated(tmp_path, program):
    db = tmp_path / "index.db"
    assert window.pdf_app(db) == {}
    window.set_pdf_app(db, [program, "-page", "{page}"])
    assert window.pdf_app(db)["command"][1:] == ["-page", "{page}", "{file}"]  # {file} added when left out
    window.set_pdf_app(db, None)
    assert window.pdf_app(db) == {}
    with pytest.raises(FileNotFoundError):
        window.set_pdf_app(db, [str(tmp_path / "nope.exe"), "{file}"])


@pytest.mark.parametrize("name", ["cmd.exe", "evil.bat"])
def test_refused_program_in_settings_is_ignored_at_launch(launched, tmp_path, monkeypatch, name):
    monkeypatch.setattr(opener, "_acrobat", lambda: None)
    exe = tmp_path / name
    exe.write_bytes(b"")  # written into the settings file by hand, past set_pdf_app
    opener._pdf("m.pdf", {"page": 2}, {"command": [str(exe), "/c", "{file}"]})
    assert launched == [["default", "m.pdf"]]


# ---- finding the hit in Word -------------------------------------------------------------------

def test_what_is_looked_for_in_word_is_the_words_around_the_term_in_its_cell():
    long = "鍋を洗ってから、最後に、火加減の設定を弱火に戻して蓋をして待つ。"
    assert opener._words_around(long, "設定") == [long, "最後に、火加減の設定を弱火に戻して蓋"]  # then 8 a side
    # a table row: the cell's text before its header's label, which is not in the row
    row = opener._words_around("項目: 塩 / 設定値: 少々 / メモ: 後で設定", "設定")
    assert row[0] == "後で設定"
    assert opener._words_around("設定", "設定") == [] and opener._words_around("鍋", "設定") == []


@pytest.fixture(scope="module")
def word():
    """A Word of its own, hidden: one the user has open is not touched."""
    import sys

    if sys.platform != "win32":
        pytest.skip("needs Word")
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        app = win32com.client.DispatchEx("Word.Application")
    except Exception:
        pytest.skip("Word not available")
    app.Visible = False
    yield app
    app.Quit()


def test_the_hit_is_found_in_word_quickly_past_many_earlier_occurrences(word, tmp_path):
    import time

    from docwhere.extractors import word as word_reader

    doc = word.Documents.Add()
    for i in range(150):  # the term many times before the hits
        doc.Content.InsertAfter(f"前置き {i}: 設定の説明。\r")
    for text in ("番号の付いた見出しの設定", "丸数字①の設定は②に続く"):
        doc.Content.InsertAfter(text + "\r")
    heading = doc.Paragraphs(151).Range
    heading.Style = doc.Styles(-2)  # Heading 1
    heading.ListFormat.ApplyOutlineNumberDefault()
    table = doc.Tables.Add(doc.Paragraphs(doc.Paragraphs.Count).Range, 2, 2)
    for (r, c), text in {(1, 1): "項目", (1, 2): "設定値", (2, 1): "火加減", (2, 2): "中火に設定"}.items():
        table.Cell(r, c).Range.Text = text
    doc.TablesOfContents.Add(doc.Range(0, 0), True, 1, 3)  # repeats the heading at the top
    path = tmp_path / "hits.docx"
    doc.SaveAs2(str(path), 16)
    lines = [line for c in word_reader.extract(path) for line in c.lines if "設定" in line and "前置き" not in line]
    expected = {"番号の付いた見出しの設定": "番号の付いた見出しの設定",  # the heading, not its contents line
                "丸数字": "丸数字①の設定は②に続く",  # the index folds ① to 1; Word does not
                "中火に設定": "中火に設定"}  # the cell, not the header's 設定値
    for line in lines:
        want = next((v for k, v in expected.items() if k in line and not line.endswith(" 11")), None)
        if want is None:
            continue
        started = time.perf_counter()
        found = opener._find_in_word(doc, "設定", line)
        assert time.perf_counter() - started < 1.0, line
        assert found.Paragraphs(1).Range.Text.strip("\r\x07") == want, line
        expected = {k: v for k, v in expected.items() if v != want}
    assert expected == {}  # each case was among the lines
    doc.Close(False)
