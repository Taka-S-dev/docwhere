"""Open a file at a hit: PDF page in Acrobat, Word/Excel/PowerPoint through COM, text in VS Code.

Every jump falls back to opening the file normally, so a failed jump still gets the user
into the right document.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from docwhere.model import norm

WORD_FIND_LIMIT = 500  # occurrences of the term to try before settling for the first one
WORD_FIND_SECONDS = 3.0  # and the longest that trying may take (each costs Word tens of ms)
WORD_AROUND = (24, 8)  # characters each side of the term to look for in Word, then fewer
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

    The words around the term in that paragraph are looked for first, in one search: a common
    term can occur hundreds of times before the hit, and each occurrence checked costs Word
    tens of milliseconds. When Word does not hold them as the index wrote them (a circled
    digit the index folds to a plain one), the paragraph is found in the document's text,
    read once. Only when that cannot be placed either is each occurrence's paragraph compared,
    for a few seconds at most; the first occurrence is the fallback.

    A table of contents repeats each heading's text (with its number) near the top, so a
    place found inside one is passed over: the hit is the heading itself. Only when the words
    are nowhere else (the hit was the table of contents' own line) is the place in it taken.
    """
    if not term:
        return None
    line = norm(line)
    in_contents = _in_contents(doc)
    in_a_contents = None
    for around in _words_around(line, term):
        rng = doc.Content
        find = rng.Find
        found = _search(find, around)
        while found and in_contents(rng):
            if in_a_contents is None:
                in_a_contents = rng.Duplicate
            found = find.Execute()
        if found and _search(rng.Find, term):  # the term, inside the words found
            return rng
    rng = _paragraph_holding(doc, line, term, in_contents)
    if rng is not None and _search(rng.Find, term):
        return rng
    if in_a_contents is not None and _search(in_a_contents.Find, term):
        return in_a_contents
    target = line[:60]
    rng = doc.Content
    find = rng.Find
    if not _search(find, term):
        return None
    first = rng.Duplicate
    until = time.monotonic() + WORD_FIND_SECONDS
    for _ in range(WORD_FIND_LIMIT):
        if not in_contents(rng):
            paragraph = norm(rng.Paragraphs(1).Range.Text.replace("\x07", " "))
            if target and target in paragraph:
                return rng
            if rng.Information(WD_WITH_IN_TABLE) and paragraph and paragraph in line:
                return rng  # a table row is indexed as one line, so the cell is a part of it
        if time.monotonic() > until or not find.Execute():
            break
    return first


def _in_contents(doc):
    """A test of whether a range starts inside one of the document's tables of contents."""
    try:
        spans = [(t.Range.Start, t.Range.End) for t in doc.TablesOfContents]
    except Exception:  # a document Word gives no such collection for
        spans = []
    return lambda rng: any(start <= rng.Start < end for start, end in spans) if spans else False


def _paragraph_holding(doc, line: str, term: str, in_contents=lambda rng: False):
    """The range of the paragraph ``line`` was indexed from, found in the document's text read
    in one call (passing over one in a table of contents); None when no paragraph is it, or
    the place found does not hold it.

    The text ends each paragraph with \\r, a table cell and a row with \\r\\x07, so the i-th
    piece of it is the i+1-th paragraph. A place counted in the text is quicker to reach than
    a paragraph by number (Word walks to it), so that is tried first: \\x07 takes no place of
    its own, but a field (a table of contents, a link) takes places for its code that its
    shown text does not, so each range found is read back and compared."""
    target, low_term = line[:60], term.lower()
    pieces = doc.Content.Text.split("\r")
    at = 0
    for i, raw in enumerate(pieces):
        text = raw.replace("\x07", "")
        here = norm(text)
        in_cell = i + 1 < len(pieces) and pieces[i + 1].startswith("\x07")
        # a table row is indexed as one line, so a cell is a part of it
        if here and ((target and target in here) or (in_cell and low_term in here.lower() and here in line)):
            rng = next((r for r in (reach() for reach in (lambda: doc.Range(at, at + len(text)),
                                                           lambda: doc.Paragraphs(i + 1).Range))
                        if norm(r.Text.replace("\x07", "")) == here), None)
            if rng is None:
                return None  # the places do not line up: leave it to comparing each occurrence
            if not in_contents(rng):
                return rng
        at += len(text) + 1
    return None


def _words_around(line: str, term: str) -> list[str]:
    """What to look for in Word to land on ``term`` where ``line`` holds it: the words around
    it, then fewer of them.

    The index joins a table row's cells as "label: text / label: text", the label being the
    header's cell, which is not in the row. So the line is taken a cell at a time, its text
    without the label first (the term in a header's label, as 設定 in 設定値, is not the
    hit), and only then whole, for text that holds ": " or " / " of its own."""
    low_term = term.lower()
    parts = []
    for cell in line.split(" / "):
        label, sep, text = cell.partition(": ")
        if sep and low_term in text.lower():
            parts.append(text)
    parts += [cell for cell in line.split(" / ") if low_term in cell.lower()] + [line]
    out = []
    for part in parts:
        at = part.lower().find(low_term)
        if at < 0:
            continue
        for reach in WORD_AROUND:
            around = part[max(0, at - reach):at + len(term) + reach].strip()
            if around.lower() != low_term and around not in out:
                out.append(around)
    return out


def _search(find, text: str) -> bool:
    """Look for ``text`` from the find's range on, matching any run of whitespace for a space
    (the index folds runs to one) and full- or half-width alike, as the index does."""
    pattern = "^w".join(part.replace("^", "^^") for part in text.split(" "))  # ^ starts Word's specials
    if len(pattern) > 255:  # what Word's Find takes
        return False
    find.ClearFormatting()
    find.Text = pattern
    find.Forward = True
    find.Wrap = 0  # wdFindStop
    find.MatchCase = False
    find.MatchWildcards = False
    try:
        find.MatchByte = False  # ignore full-width/half-width differences (Japanese Word only)
    except Exception:
        pass
    return bool(find.Execute())


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
