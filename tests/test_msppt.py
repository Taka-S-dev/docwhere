"""Reading .ppt without PowerPoint (extractors.msppt). Where PowerPoint is installed, presentations
it makes are read both ways and compared; the rest runs anywhere."""

import datetime
import io
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from docwhere import doccheck, extractors
from docwhere.extractors import DOC_READER_ENV, is_serial, msppt, release
from docwhere.extractors import legacy as legacy_module
from docwhere.model import Chunk, table_lines

# ---- anywhere ----------------------------------------------------------------------------------

def test_a_file_that_is_not_a_powerpoint_97_presentation_is_left_to_powerpoint(tmp_path):
    cases = {
        "html.ppt": "<html><body>表</body></html>".encode(),
        "cut.ppt": bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 100,
        "empty.ppt": b"",
    }
    for name, data in cases.items():
        (tmp_path / name).write_bytes(data)
        with pytest.raises(msppt.Unsupported):
            msppt.extract(tmp_path / name)


def test_a_record_running_past_what_holds_it_is_refused():
    data = bytes([0x0F, 0x00, 0xF0, 0x0F]) + (100).to_bytes(4, "little") + b"\0" * 20
    with pytest.raises(msppt.Unsupported):
        list(msppt._records(data))


def test_a_field_shows_what_powerpoint_writes_in_its_place():
    pres = object.__new__(msppt._Presentation)
    pres.slide_number, pres.budget = 7, msppt.MAX_DOCUMENT_CHARS
    pres.footers = {msppt._FOOTER_FIELD: "台所", msppt._DATE_FIELD: "春", msppt._FLAGS: 0x01}
    fields = [(5, msppt._SLIDE_NUMBER), (9, msppt._FOOTER_FIELD)]
    assert pres._with_fields("page * / * end", fields) == "page 7 / 台所 end"
    # a place counts UTF-16 code units: 𠮷 takes two
    assert pres._with_fields("𠮷 *", [(3, msppt._SLIDE_NUMBER)]) == "𠮷 7"
    assert pres._with_fields("* *", [(0, msppt._GENERIC_DATE), (2, msppt._DATE_FIELD)]) == "春 "
    pres.footers[msppt._FLAGS] = 0x03  # the date is the day the file is read: left out
    assert pres._with_fields("*", [(0, msppt._GENERIC_DATE)]) == ""
    with pytest.raises(msppt.Unsupported):
        pres._with_fields("no mark", [(0, msppt._SLIDE_NUMBER)])
    # a long footer in many fields would grow past the limit on text read: refused before it grows
    pres.footers[msppt._FOOTER_FIELD] = "長" * 100_000
    many = "*" * 1000
    with pytest.raises(ValueError, match="limit"):
        pres._with_fields(many, [(i, msppt._FOOTER_FIELD) for i in range(1000)])
    # and many shapes pointing at the same text count each time, against one budget for the file
    pres.budget = msppt.MAX_DOCUMENT_CHARS
    outline = [("鍋" * 1_000_000, [])]
    with pytest.raises(ValueError, match="limit"):
        for _ in range(60):
            pres._outline_text(msppt._Shape(), 0, outline)


def _blob(xml: str, name="drs/shapexml.xml") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("drs/downrev.xml", "<x/>")
        if name:
            z.writestr(name, xml)
    return out.getvalue()


P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def test_a_shape_kept_in_drawingml_is_read_as_the_pptx_reader_reads_it():
    sp = (f'<p:sp {P} {A}><p:txBody><a:p><a:r><a:t>鍋で</a:t></a:r><a:br/><a:fld><a:t>3</a:t></a:fld></a:p>'
          '<a:p/></p:txBody></p:sp>')
    assert msppt._drawingml(_blob(sp)) == ["鍋で\v3", ""]
    table = (f'<p:graphicFrame {P} {A}><a:graphic><a:graphicData><a:tbl>'
             '<a:tr><a:tc><a:txBody><a:p><a:r><a:t>材料</a:t></a:r></a:p></a:txBody></a:tc>'
             '<a:tc hMerge="1"/></a:tr>'
             '<a:tr><a:tc><a:txBody><a:p><a:r><a:t>塩</a:t></a:r></a:p></a:txBody></a:tc>'
             '<a:tc><a:txBody><a:p><a:r><a:t>少々</a:t></a:r></a:p></a:txBody></a:tc></a:tr>'
             '</a:tbl></a:graphicData></a:graphic></p:graphicFrame>')
    assert msppt._drawingml(_blob(table, "drs/e2oDoc.xml")) == list(table_lines([["材料"], ["塩", "少々"]]))
    assert msppt._drawingml(_blob("", name=None)) is None  # only how it was drawn: the shape is the drawing


def test_drawingml_that_could_expand_without_end_is_refused():
    bomb = '<!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><p:sp ' + P + '>&b;</p:sp>'
    with pytest.raises(msppt.Unsupported):
        msppt._drawingml(_blob(bomb))
    # the same in UTF-16, where no byte search for "<!DOCTYPE" finds it
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("drs/shapexml.xml", ('<?xml version="1.0" encoding="UTF-16"?>' + bomb).encode("utf-16"))
    with pytest.raises(msppt.Unsupported, match="declares"):
        msppt._drawingml(out.getvalue())
    with pytest.raises(msppt.Unsupported):
        msppt._drawingml(b"PK\x03\x04 not a zip")


def test_doc_check_finds_ppt_too_and_takes_each_kind():
    paths = [Path(f"{i:03d}.doc") for i in range(90)] + [Path(f"{i:03d}.ppt") for i in range(10)]
    picked = doccheck.sample(paths, 10)
    assert len(picked) == 10 and sum(p.suffix == ".ppt" for p in picked) == 1
    assert len(doccheck.sample(paths[:99] + [Path("only.ppt")], 5)) == 5


def test_a_date_powerpoint_fills_in_on_opening_is_not_a_difference():
    today = datetime.date(2026, 11, 19)
    for line in ("November 19, 2026", "2026/11/19", "2026年11月19日木曜日", "11/19/26"):
        assert doccheck._is_today(line, today)
    for line in ("Lecture 10", "2026", "2026/11/18", "2026年11月19日 献立"):
        assert not doccheck._is_today(line, today)


def test_the_setting_decides_who_reads_a_ppt_and_in_which_worker(tmp_path, monkeypatch):
    ppt = tmp_path / "a.ppt"
    ppt.write_bytes(b"")
    by_powerpoint = [Chunk("PowerPoint", "PowerPoint", ["read by PowerPoint"], [{"slide": 1}])]
    monkeypatch.setattr(legacy_module, "ppt_through_powerpoint", lambda path: by_powerpoint)

    monkeypatch.setenv(DOC_READER_ENV, "office")
    assert is_serial(ppt)
    assert legacy_module.extract_ppt(ppt) == by_powerpoint

    monkeypatch.setenv(DOC_READER_ENV, "fast")
    assert not is_serial(ppt)
    own = [Chunk("own", "own", ["read without PowerPoint"], [{"slide": 1}])]
    monkeypatch.setattr(msppt, "extract", lambda path: own)
    assert legacy_module.extract_ppt(ppt) == own

    def refuse(path):
        raise msppt.Unsupported("password protected")

    monkeypatch.setattr(msppt, "extract", refuse)
    extractors.set_worker("pooled")
    try:
        with pytest.raises(extractors.NeedsApplication):  # handed on to PowerPoint's worker
            legacy_module.extract_ppt(ppt)
    finally:
        extractors.set_worker(None)
    assert legacy_module.extract_ppt(ppt) == by_powerpoint


# ---- with PowerPoint ---------------------------------------------------------------------------

TEXT, TITLE_ONLY, BLANK = 2, 11, 12


@pytest.fixture(scope="module")
def made(tmp_path_factory) -> Path:
    """.ppt files made by PowerPoint, of the kinds this reader must read as PowerPoint does.
    Made-up cooking text. PowerPoint runs once per user: one already running is left running."""
    if sys.platform != "win32":
        pytest.skip("needs PowerPoint")
    running = b"POWERPNT.EXE" in subprocess.run(["tasklist"], capture_output=True).stdout.upper()
    try:
        import win32com.client

        app = win32com.client.DispatchEx("PowerPoint.Application")
    except Exception:
        pytest.skip("PowerPoint not available")
    out = tmp_path_factory.mktemp("made")
    try:
        pres = app.Presentations.Add(WithWindow=False)
        pres.PageSetup.FirstSlideNumber = 3
        s = pres.Slides.Add(1, TEXT)
        s.Shapes.Placeholders(1).TextFrame.TextRange.Text = "献立"
        s.Shapes.Placeholders(2).TextFrame.TextRange.Text = "𠮷野家と🍳\r鍋を温める" + chr(11) + "中火で"
        s.NotesPage.Shapes.Placeholders(2).TextFrame.TextRange.Text = "ノートの一行目\rノートの二行目"
        hf = s.HeadersFooters
        hf.Footer.Visible, hf.Footer.Text, hf.SlideNumber.Visible = True, "台所の記録", True
        s = pres.Slides.Add(2, TITLE_ONLY)
        s.Shapes.Placeholders(1).TextFrame.TextRange.Text = "材料"
        t = s.Shapes.AddTable(3, 3, 50, 120, 600, 200).Table
        for i, row in enumerate([["材料", "分量", "メモ"], ["塩", "少々", ""], ["砂糖", "大さじ1", "最後に"]], 1):
            for j, text in enumerate(row, 1):
                t.Cell(i, j).Shape.TextFrame.TextRange.Text = text
        t.Cell(1, 2).Merge(t.Cell(1, 3))
        s = pres.Slides.Add(3, BLANK)
        a = s.Shapes.AddTextbox(1, 50, 50, 150, 40)
        a.TextFrame.TextRange.Text = "箱一"
        b = s.Shapes.AddShape(5, 250, 50, 150, 40)
        b.TextFrame.TextRange.Text = "角丸"
        s.Shapes.Range([a.Name, b.Name]).Group()
        pres.SaveAs(str(out / "read.ppt"), 1)
        pres.Close()
        pres = s = t = a = b = hf = None  # let go of PowerPoint's objects before it quits
    finally:
        if not running:
            app.Quit()
    return out


def _flat(chunks):
    return [(c.title, c.crumb, c.lines) for c in chunks]


def test_a_ppt_is_read_without_powerpoint_as_powerpoint_reads_it(made):
    try:
        by_powerpoint = legacy_module.ppt_through_powerpoint(made / "read.ppt")
    finally:
        release()
    assert _flat(msppt.extract(made / "read.ppt")) == _flat(by_powerpoint)


def test_what_the_reader_reads_as_powerpoint_does(made):
    slides = msppt.extract(made / "read.ppt")
    assert [c.title for c in slides] == ["献立", "材料", ""]
    first = slides[0].lines
    assert "𠮷野家と🍳" in first and "ノートの二行目" in first
    assert "台所の記録" in first and "3" in first  # the footer and the slide number, counted from 3
    assert "材料: 塩 / 分量: 少々" in slides[1].lines or any("少々" in line for line in slides[1].lines)
    assert slides[2].lines == ["箱一", "角丸"]  # a group's shapes in its order


def test_doc_check_reads_ppt_both_ways(made, tmp_path, monkeypatch, capsys):
    from docwhere.cli import main

    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "index" / "index.db"))
    assert main(["doc-check", str(made), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["checked"], out["same"], out["differ"], out["office_failed"]) == (1, 1, [], [])
