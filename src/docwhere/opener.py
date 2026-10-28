"""Open a file at a hit: PDF page in Acrobat, Word/Excel/PowerPoint through COM, text in VS Code.

Every jump falls back to opening the file normally, so a failed jump still gets the user
into the right document.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from docwhere.model import norm

WORD_FIND_LIMIT = 500  # occurrences of the term to try before settling for the first one
_MACROS_BY_UI = 2  # msoAutomationSecurityByUI


def open_at(path: str, anchor: dict) -> str:
    """Open ``path`` positioned at ``anchor``; return a one-line description of what happened."""
    if not Path(path).exists():
        raise FileNotFoundError(path)
    ext = Path(path).suffix.lower()
    handler = {
        ".pdf": _pdf,
        ".docx": _word,
        ".docm": _word,
        ".doc": _word,
        ".xlsx": _excel,
        ".xlsm": _excel,
        ".xls": _excel,
        ".pptx": _powerpoint,
        ".pptm": _powerpoint,
        ".ppt": _powerpoint,
    }.get(ext, _text)
    try:
        return handler(os.path.abspath(path), anchor)
    except Exception as e:
        _startfile(path)
        return f"opened without jumping ({type(e).__name__}: {e})"


def _startfile(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


def _pdf(path: str, anchor: dict) -> str:
    page = anchor.get("page", 1)
    acrobat = _acrobat()
    if acrobat is None:
        _startfile(path)
        return "opened in the default PDF viewer (Acrobat not found, cannot jump to the page)"
    subprocess.Popen([acrobat, "/A", f"page={page}", path])
    return f"opened at p.{page}"


def _acrobat() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    for exe in ("Acrobat.exe", "AcroRd32.exe"):
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                found = winreg.QueryValue(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}")
            except OSError:
                continue
            if found and Path(found.strip('"')).exists():
                return found.strip('"')
    return None


def _office_app(progid: str):
    """The user's running Office application (or a new one), with macros governed by the Trust Center.

    Office opens files for automation with macros enabled and no prompt, unlike a double-click.
    Opening a search hit must behave like a double-click, so macro security is set to follow
    the user's Trust Center settings before any document is opened.
    """
    import win32com.client

    app = win32com.client.Dispatch(progid)
    app.AutomationSecurity = _MACROS_BY_UI
    return app


def _already_open(collection, path: str):
    for item in collection:
        if os.path.normcase(item.FullName) == os.path.normcase(path):
            return item
    return None


def _word(path: str, anchor: dict) -> str:
    app = _office_app("Word.Application")
    app.Visible = True
    doc = _already_open(app.Documents, path) or app.Documents.Open(path)
    doc.Activate()
    found = _find_in_word(doc, anchor.get("term", ""), anchor.get("text", ""))
    if found is not None:
        found.Select()
    app.Activate()
    return "opened at the hit" if found is not None else "opened (hit text not found in Word)"


def _find_in_word(doc, term: str, line: str):
    """The occurrence of ``term`` inside the paragraph the hit came from.

    The index stores normalised paragraph text, so each occurrence's paragraph is normalised
    the same way before comparing; the first occurrence is the fallback.
    """
    if not term:
        return None
    target = norm(line)[:60]
    rng = doc.Content
    find = rng.Find
    find.ClearFormatting()
    find.Text = term.replace("^", "^^")[:255]  # ^ starts a Word special character
    find.Forward = True
    find.Wrap = 0  # wdFindStop
    find.MatchCase = False
    find.MatchWildcards = False
    try:
        find.MatchByte = False  # ignore full-width/half-width differences (Japanese Word only)
    except Exception:
        pass
    first = None
    for _ in range(WORD_FIND_LIMIT):
        if not find.Execute():
            break
        if first is None:
            first = rng.Duplicate
        paragraph = norm(rng.Paragraphs(1).Range.Text.replace("\x07", " "))
        if target and target in paragraph:
            return rng
    return first


def _excel(path: str, anchor: dict) -> str:
    app = _office_app("Excel.Application")
    app.Visible = True
    wb = _already_open(app.Workbooks, path) or app.Workbooks.Open(path)
    wb.Activate()
    if "cell" not in anchor:
        return "opened"
    ws = wb.Worksheets(anchor["sheet"])
    ws.Activate()
    ws.Range(anchor["cell"]).Select()
    _to_front(app.Hwnd)
    return f"opened at {anchor['sheet']}!{anchor['cell']}"


def _powerpoint(path: str, anchor: dict) -> str:
    app = _office_app("PowerPoint.Application")
    pres =_already_open(app.Presentations, path) or app.Presentations.Open(path, WithWindow=True)
    window = pres.Windows(1)
    window.Activate()
    slide = anchor.get("slide", 1)
    window.View.GotoSlide(slide)
    return f"opened at slide {slide}"


def _text(path: str, anchor: dict) -> str:
    code = shutil.which("code")
    if code and "line" in anchor:
        subprocess.Popen([code, "-g", f"{path}:{anchor['line']}"])
        return f"opened in VS Code at line {anchor['line']}"
    _startfile(path)
    return "opened"


def _to_front(hwnd: int) -> None:
    try:
        import win32gui

        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass  # Windows may refuse focus stealing; the window is still open
