"""Legacy binary Office files (.doc .xls .ppt): converted to OOXML by Office itself, then read
by the regular extractors.

Needs Microsoft Office on Windows. One instance of each application is started on first use,
reused for every file in the sync and closed by ``release()``. Macros are force-disabled and
alerts suppressed so an unattended conversion never runs document code or waits on a dialog;
a password-protected file fails instead of prompting.
"""

from __future__ import annotations

import atexit
import tempfile
from pathlib import Path

from docwhere.extractors import on_release, register
from docwhere.model import Chunk

_FORCE_DISABLE_MACROS = 3  # msoAutomationSecurityForceDisable
_DUMMY_PASSWORD = "docwhere"  # ignored by unprotected files; makes protected ones fail fast
_apps: dict[str, object] = {}


def _app(progid: str):
    if progid not in _apps:
        try:
            import pythoncom
            import win32com.client
        except ImportError as e:
            raise RuntimeError("legacy Office formats need Microsoft Office on Windows") from e
        pythoncom.CoInitialize()
        app = win32com.client.DispatchEx(progid)  # a private instance, never the user's open one
        app.AutomationSecurity = _FORCE_DISABLE_MACROS
        if progid == "PowerPoint.Application":
            app.DisplayAlerts = 1  # ppAlertsNone
        else:
            app.Visible = False
            app.DisplayAlerts = 0  # wdAlertsNone / False
        _apps[progid] = app
    return _apps[progid]


@on_release
@atexit.register
def _quit_all() -> None:
    for app in _apps.values():
        try:
            app.Quit()
        except Exception:
            pass  # already gone
    _apps.clear()


@register(".doc", serial=True)
def extract_doc(path: Path) -> list[Chunk]:
    from docwhere.extractors.word import extract as from_docx

    app = _app("Word.Application")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp, "converted.docx")
        doc = app.Documents.Open(
            str(path.resolve()), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False,
            PasswordDocument=_DUMMY_PASSWORD, Visible=False,
        )
        try:
            doc.SaveAs2(str(out), FileFormat=16)  # wdFormatDocumentDefault
        finally:
            doc.Close(SaveChanges=0)
        return from_docx(out)


@register(".xls", serial=True)
def extract_xls(path: Path) -> list[Chunk]:
    from docwhere.extractors.excel import extract as from_xlsx

    app = _app("Excel.Application")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp, "converted.xlsx")
        wb = app.Workbooks.Open(
            str(path.resolve()), UpdateLinks=0, ReadOnly=True, Password=_DUMMY_PASSWORD, AddToMru=False
        )
        try:
            wb.SaveAs(str(out), FileFormat=51)  # xlOpenXMLWorkbook
        finally:
            wb.Close(SaveChanges=False)
        return from_xlsx(out)


@register(".ppt", serial=True)
def extract_ppt(path: Path) -> list[Chunk]:
    from docwhere.extractors.powerpoint import extract as from_pptx

    app = _app("PowerPoint.Application")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp, "converted.pptx")
        pres = app.Presentations.Open(str(path.resolve()), ReadOnly=True, Untitled=False, WithWindow=False)
        try:
            pres.SaveAs(str(out), 24)  # ppSaveAsOpenXMLPresentation
        finally:
            pres.Close()
        return from_pptx(out)
