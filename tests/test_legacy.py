"""Legacy .doc/.xls/.ppt need Microsoft Office; skipped where it is not installed."""

import sys
from pathlib import Path

import pytest

from docwhere.extractors import extract, is_serial, release
from docwhere.extractors import legacy as legacy_module
from docwhere.index import Index

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="needs Office COM")


def _office(progid: str):
    try:
        import win32com.client

        return win32com.client.DispatchEx(progid)
    except Exception:
        pytest.skip(f"{progid} not available")


def _mixed(path: Path) -> None:
    """Every kind of cell value, to compare reading the .xls directly with reading it through Excel."""
    import datetime

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "仕様"
    ws.append(["項目", "既定値", "単位", "備考"])
    ws.append(["接続タイムアウト", 30, "秒", "整数"])
    ws.append(["比率", 0.25, None, True])
    ws.append(["開始日", datetime.datetime(2024, 4, 1), "", False])
    ws.append(["時刻", datetime.time(9, 30), "x", "=1/0"])
    ws.append(["式", "=B2*2", "大きい数", 12345678901])
    ws.append(["負", -3.5, "全角　空白", "改行\nあり"])
    wb.create_sheet("空")
    wb.create_sheet("Two")["C5"] = "ぽつん"
    wb.save(path)


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
    _mixed(out / "v.xlsx")
    excel = _office("Excel.Application")
    try:
        for name in ("c", "v"):
            wb = excel.Workbooks.Open(str(out / f"{name}.xlsx"))
            wb.SaveAs(str(out / f"{name}.xls"), FileFormat=56)  # xlExcel8
            wb.Close(False)
        excel.DisplayAlerts = False
        wb = excel.Workbooks.Open(str(out / "c.xlsx"))
        wb.SaveAs(str(out / "locked.xls"), FileFormat=56, Password="pw")
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


def test_an_xls_is_read_without_excel_as_excel_would_read_it(legacy):
    assert not is_serial(legacy / "v.xls")  # read alongside the other files
    try:
        through_excel = legacy_module._xls_through_excel(legacy / "v.xls")
    finally:
        release()
    direct = extract(legacy / "v.xls")
    assert [(c.title, c.lines, c.anchors) for c in direct] == [(c.title, c.lines, c.anchors) for c in through_excel]
    assert "接続タイムアウト\t30\t秒\t整数" in direct[0].lines


def test_an_xls_that_is_really_html_is_read_through_excel(legacy, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "real.xls").write_bytes((legacy / "c.xls").read_bytes())
    (folder / "web.xls").write_text(
        '<html><head><meta http-equiv="Content-Type" content="text/html; charset=utf-8"></head>'
        "<body><table><tr><td>項目</td><td>値</td></tr><tr><td>再送回数</td><td>3</td></tr></table></body></html>",
        encoding="utf-8",
    )
    index = Index(tmp_path / "index.db")
    stats = index.sync(folder, workers=2)
    assert stats.updated == 2 and stats.failed == 0, index.errors()
    bodies = {r["path"]: r["body"] for r in index.db.execute(
        "SELECT f.path, group_concat(c.body) AS body FROM files f JOIN chunks c ON c.file_id = f.id GROUP BY f.id")}
    assert "再送回数" in bodies[str(folder / "web.xls")]


def test_a_password_protected_xls_is_given_up_at_once(legacy, tmp_path):
    # through Excel it would wait on the password prompt until the timeout
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "locked.xls").write_bytes((legacy / "locked.xls").read_bytes())
    index = Index(tmp_path / "index.db")
    stats = index.sync(folder, workers=1, timeout=30)
    assert stats.failed == 1 and not stats.timed_out
    [(_, error)] = index.errors()
    assert "password-protected" in error
