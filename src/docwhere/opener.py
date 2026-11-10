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
WD_WITH_IN_TABLE = 12  # Range.Information: whether the range is inside a table
_MACROS_BY_UI = 2  # msoAutomationSecurityByUI


def open_at(path: str, anchor: dict, pdf_app: dict | None = None) -> str:
    """Open ``path`` positioned at ``anchor``; return a one-line description of what happened.

    ``pdf_app`` is the PDF application set with ``viewer pdf`` ({"command": [...]}), if any.
    """
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
        if handler is _pdf:
            return _pdf(os.path.abspath(path), anchor, pdf_app)
        return handler(os.path.abspath(path), anchor)
    except Exception as e:
        start_file(path)
        return f"opened without jumping ({type(e).__name__}: {e})"


def start_file(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


def _pdf(path: str, anchor: dict, pdf_app: dict | None = None) -> str:
    """Open a PDF at its page.

    With a command set by ``viewer pdf`` ([program, arg, ...] where {page} and {file} are
    filled in), that program runs, without a shell. Otherwise Acrobat if it is installed, else
    Windows' default PDF app, which cannot be told a page.
    """
    page = int(anchor.get("page", 1))
    command = (pdf_app or {}).get("command")
    if command:
        # checked again here, not only when saved: a settings file edited by hand (or by
        # anyone else) must not turn "open the PDF" into running a shell
        from docwhere.window import checked_command

        try:
            command = checked_command(command)
        except FileNotFoundError:
            if Path(command[0]).exists():  # there, but not an .exe (a .bat, a .lnk)
                command = None
            # else: reported below, with the default app as the fallback
        except (OSError, ValueError):
            command = None
    if command:
        program = command[0]
        if not Path(program).is_file():
            start_file(path)
            return f"opened in the default PDF app ({program} not found, cannot jump to the page)"
        args = [arg.replace("{page}", str(page)).replace("{file}", path) for arg in command[1:]]
        subprocess.Popen([program, *args])
        return f"opened at p.{page} in {Path(program).stem}"
    if acrobat := _acrobat():
        subprocess.Popen([acrobat, "/A", f"page={page}", path])
        return f"opened at p.{page} in Acrobat"
    start_file(path)
    return "opened in the default PDF app (cannot jump to the page; set one with: viewer pdf)"


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
    line = norm(line)
    target = line[:60]
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
        if rng.Information(WD_WITH_IN_TABLE) and paragraph and paragraph in line:
            return rng  # a table row is indexed as one line, so the cell is a part of it
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
    start_file(path)
    return "opened"


def _to_front(hwnd: int) -> None:
    try:
        import win32gui

        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass  # Windows may refuse focus stealing; the window is still open
