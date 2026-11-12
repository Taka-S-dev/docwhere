"""Legacy binary Office files (.doc .xls .ppt): converted to OOXML by Office itself, then read
by the regular extractors.

An .xls is read directly instead (xlrd), with no Office and alongside the other files; only an
.xls that xlrd cannot read (an HTML or XML table saved under that name) goes through Excel.
A password-protected .xls is given up at once: Excel would ask for the password and wait.

Needs Microsoft Office on Windows. One instance of each application is started on first use,
reused for every file in the sync and closed by ``release()``. Macros are force-disabled and
alerts suppressed so an unattended conversion never runs document code or waits on a dialog.
Some dialogs still appear (Word asks whether to update a document's links to other files, Excel
asks for the password of a protected workbook), so each application's process is reported: the
caller stops it when a file takes too long.
"""

from __future__ import annotations

import atexit
import os
import tempfile
from pathlib import Path

from docwhere.extractors import (
    NeedsApplication,
    may_start_application,
    on_release,
    process_started,
    register,
)
from docwhere.model import Chunk

_FORCE_DISABLE_MACROS = 3  # msoAutomationSecurityForceDisable
# Ignored by unprotected files; passed so that a protected file is refused rather than asked
# for, though an application that still asks is stopped at the timeout.
_DUMMY_PASSWORD = "docwhere"
_apps: dict[str, object] = {}


def _app(progid: str):
    if progid not in _apps:
        try:
            import pythoncom
            import win32com.client
        except ImportError as e:
            raise RuntimeError("legacy Office formats need Microsoft Office on Windows") from e
        pythoncom.CoInitialize()
        before = _running(_IMAGES[progid])
        app = win32com.client.DispatchEx(progid)  # a private instance, never the user's open one
        if pid := _pid(app, progid, before):
            process_started(pid)
        app.AutomationSecurity = _FORCE_DISABLE_MACROS
        if progid == "PowerPoint.Application":
            app.DisplayAlerts = 1  # ppAlertsNone
        else:
            app.Visible = False
            app.DisplayAlerts = 0  # wdAlertsNone / False
        _apps[progid] = app
    return _apps[progid]


_IMAGES = {"Word.Application": "WINWORD.EXE", "Excel.Application": "EXCEL.EXE", "PowerPoint.Application": "POWERPNT.EXE"}


def _pid(app, progid: str, before: set[int]) -> int | None:
    """The process of an application just started, or None when it cannot be told apart."""
    import pywintypes
    import win32gui
    import win32process

    try:
        if progid == "Word.Application":
            app.Caption = f"conversion {os.getpid()}-{id(app)}"  # Word has no window handle property; find its window by title
            hwnd = win32gui.FindWindow("OpusApp", app.Caption)
        elif progid == "Excel.Application":
            hwnd = app.Hwnd
        else:
            # PowerPoint runs as one instance per user and has no handle without a window:
            # it is ours only when none was running before.
            started = _running(_IMAGES[progid]) - before
            return started.pop() if not before and len(started) == 1 else None
        return win32process.GetWindowThreadProcessId(hwnd)[1] if hwnd else None
    except (pywintypes.error, pywintypes.com_error):  # no such window, or the application refused
        return None


def _running(image: str) -> set[int]:
    try:
        import pywintypes
        import win32api
        import win32con
        import win32process
    except ImportError:
        return set()
    found = set()
    for pid in win32process.EnumProcesses():
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except pywintypes.error:
            continue  # another user's or a system process
        try:
            name = win32process.GetModuleFileNameEx(handle, None)
        except pywintypes.error:
            continue  # exited in the meantime
        finally:
            win32api.CloseHandle(handle)
        if name.lower().endswith("\\" + image.lower()):
            found.add(pid)
    return found


@on_release
@atexit.register
def _quit_all() -> None:
    for app in _apps.values():
        try:
            app.Quit()
        except Exception:
            pass  # already gone
    _apps.clear()


def open_word_document(path: Path):
    """Open a document read-only in the private Word instance, without any prompt Word can skip."""
    app = _app("Word.Application")
    # A document with links to other files otherwise asks, despite DisplayAlerts, whether to
    # update them. The option is the user's own Word setting, so it is put back.
    links = app.Options.UpdateLinksAtOpen
    app.Options.UpdateLinksAtOpen = False
    try:
        return app.Documents.Open(
            str(path.resolve()), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False,
            PasswordDocument=_DUMMY_PASSWORD, Visible=False,
        )
    finally:
        app.Options.UpdateLinksAtOpen = links


@register(".doc", serial=True)
def extract_doc(path: Path) -> list[Chunk]:
    from docwhere.extractors.word import extract as from_docx

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp, "converted.docx")
        doc = open_word_document(path)
        try:
            doc.SaveAs2(str(out), FileFormat=16)  # wdFormatDocumentDefault
        finally:
            doc.Close(SaveChanges=0)
        return from_docx(out)


@register(".xls")
def extract_xls(path: Path) -> list[Chunk]:
    import xlrd
    from xlrd.compdoc import CompDocError

    from docwhere.extractors.excel import sheet_chunks

    try:
        book = xlrd.open_workbook(str(path), on_demand=True)
    except (xlrd.XLRDError, CompDocError) as e:
        if "encrypted" in str(e).lower():
            raise ValueError("password-protected; save it without a password to search it") from e
        if not may_start_application():
            raise NeedsApplication from e
        return _xls_through_excel(path)
    try:
        chunks: list[Chunk] = []
        for i in range(book.nsheets):
            chunks += sheet_chunks(_XlsSheet(book.get_sheet(i), book.datemode))
            book.unload_sheet(i)
        return chunks
    finally:
        book.release_resources()


class _XlsCell:
    __slots__ = ("column_letter", "row", "value")

    def __init__(self, value, row: int, column_letter: str) -> None:
        self.value, self.row, self.column_letter = value, row, column_letter


class _XlsSheet:
    """An xlrd sheet seen through the parts of an openpyxl worksheet that ``sheet_chunks``
    uses, with cell values as openpyxl gives them (30, not 30.0; dates as datetimes)."""

    def __init__(self, sheet, datemode: int) -> None:
        self.sheet, self.datemode, self.title = sheet, datemode, sheet.name

    def iter_rows(self):
        from openpyxl.utils import get_column_letter

        letters = [get_column_letter(c + 1) for c in range(self.sheet.ncols)]
        for r in range(self.sheet.nrows):
            yield [_XlsCell(self._value(cell), r + 1, letters[c]) for c, cell in enumerate(self.sheet.row(r))]

    def _value(self, cell):
        import xlrd

        kind, value = cell.ctype, cell.value
        if kind in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
            return None
        if kind == xlrd.XL_CELL_NUMBER:
            return int(value) if float(value).is_integer() else value
        if kind == xlrd.XL_CELL_BOOLEAN:
            return bool(value)
        if kind == xlrd.XL_CELL_ERROR:
            return xlrd.error_text_from_code.get(value, "#VALUE!")
        if kind == xlrd.XL_CELL_DATE:
            try:
                parts = xlrd.xldate_as_tuple(value, self.datemode)
            except xlrd.xldate.XLDateError:
                return value
            if parts[:3] == (0, 0, 0):
                import datetime

                return datetime.time(*parts[3:])
            return xlrd.xldate_as_datetime(value, self.datemode)
        return value


def _xls_through_excel(path: Path) -> list[Chunk]:
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
