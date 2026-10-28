"""Legacy .doc/.xls/.ppt need Microsoft Office; skipped where it is not installed."""

import sys
from pathlib import Path

import pytest

from docwhere.extractors import extract, is_serial, release

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Office COM")


def _office(progid: str):
    try:
        import win32com.client

        return win32com.client.DispatchEx(progid)
    except Exception:
        pytest.skip(f"{progid} not available")


@pytest.fixture(scope="module")
def legacy(tmp_path_factory) -> Path:
    """Save the conftest documents in the old binary formats through Office itself."""
    import conftest

    out = tmp_path_factory.mktemp("legacy")
    conftest._word(out / "m.docx")
    conftest._excel(out / "c.xlsx")
    conftest._powerpoint(out / "s.pptx")

    word = _office("Word.Application")
    try:
        doc = word.Documents.Open(str(out / "m.docx"))
        doc.SaveAs2(str(out / "m.doc"), FileFormat=0)  # wdFormatDocument
        doc.Close(0)
    finally:
        word.Quit()
    excel = _office("Excel.Application")
    try:
        wb = excel.Workbooks.Open(str(out / "c.xlsx"))
        wb.SaveAs(str(out / "c.xls"), FileFormat=56)  # xlExcel8
        wb.Close(False)
    finally:
        excel.Quit()
    ppt = _office("PowerPoint.Application")
    try:
        pres = ppt.Presentations.Open(str(out / "s.pptx"), WithWindow=False)
        pres.SaveAs(str(out / "s.ppt"), 1)  # ppSaveAsPresentation
        pres.Close()
    finally:
        ppt.Quit()
    return out


def test_legacy_formats_read_like_their_modern_counterparts(legacy):
    try:
        doc = extract(legacy / "m.doc")
        xls = extract(legacy / "c.xls")
        ppt = extract(legacy / "s.ppt")
    finally:
        release()
    assert [c.crumb for c in doc] == [c.crumb for c in extract(legacy / "m.docx")]
    assert xls[0].lines == extract(legacy / "c.xlsx")[0].lines
    assert [c.title for c in ppt] == ["概要", "config の使い方"]
    assert is_serial(legacy / "m.doc") and not is_serial(legacy / "m.docx")
