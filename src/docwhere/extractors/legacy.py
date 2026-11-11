"""Legacy binary Office files (.doc .xls .ppt): converted to OOXML by Office itself, then read
by the regular extractors.

Needs Microsoft Office on Windows. One instance of each application is started on first use,
reused for every file in the sync and closed by ``release()``. Macros are force-disabled and
alerts suppressed so an unattended conversion never runs document code or waits on a dialog;
a password-protected file fails instead of prompting. Some dialogs still appear (Word asks
whether to update a document's links to other files), so each application's process is
reported: the caller stops it when a file takes too long.
"""

from __future__ import annotations

import atexit
import os
import tempfile
from pathlib import Path

from docwhere.extractors import on_release, process_started, register
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
