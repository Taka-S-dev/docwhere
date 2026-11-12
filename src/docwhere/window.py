"""The docwhere window: viewer pages in a dedicated WebView2 window instead of a browser.

A browser exposes the page to its extensions (company-installed ones included), its history
and its sync; a WebView2 window built here loads no extensions and keeps no profile. The page
itself stays the static file with scripts forbidden by its CSP; the reader script (APP_JS)
is injected from outside after each load and talks to Python through pywebview's bridge, so
no port is opened. One window serves every view: a later ``docwhere view`` hands its page to
the running window over a named pipe that only callers holding the key file can use.

What the page may ask of Python is the ``Api`` class: render a page or list its links for a
PDF that is in the index (or one docwhere exported), open an indexed file at a spot, copy
text, keep the window on top. Nothing else is reachable.
"""

from __future__ import annotations

import functools
import getpass
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Client, Listener
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from docwhere.settings import load_settings, private, save_settings

START_TIMEOUT = 15.0
DEFAULT_TITLE = "Reference"  # what others see on screen and in the taskbar: no tool name
DEFAULT_SIZE = (1280, 900)
MAX_TEXT = 300


# ---- settings ----------------------------------------------------------------------------


def mode(db: Path) -> str:
    """"window" (the default) or "browser": where view pages open."""
    return "browser" if load_settings(db).get("viewer") == "browser" else "window"


def set_mode(db: Path, value: str) -> None:
    save_settings(db, viewer=value)


def title(db: Path) -> str:
    """The window's title, as set with ``viewer title``; the default names no tool."""
    return str(load_settings(db).get("title") or DEFAULT_TITLE)


def set_title(db: Path, value: str) -> None:
    save_settings(db, title=value.strip() or DEFAULT_TITLE)


def pdf_app(db: Path) -> dict:
    """{"command": [program, arg, ...]} as set with ``viewer pdf``, or {} for the default."""
    setting = load_settings(db).get("pdf_app") or {}
    return setting if isinstance(setting, dict) and isinstance(setting.get("command"), list) else {}


def set_pdf_app(db: Path, command: list[str] | None) -> None:
    """Use ``command`` to open PDFs ({page} and {file} are filled in); None goes back to the
    default (Acrobat if installed, else Windows' default PDF app)."""
    save_settings(db, pdf_app={"command": checked_command(command)} if command else {})


# Programs that run commands or scripts given as arguments: as the "PDF app" they would turn a
# settings change into running anything, so they are refused whatever their folder.
REFUSED_PROGRAMS = {
    "cmd", "powershell", "powershell_ise", "pwsh", "wscript", "cscript", "mshta", "rundll32", "regsvr32",
    "msiexec", "wmic", "bash", "sh", "wsl", "node", "java", "javaw", "certutil", "bitsadmin", "schtasks",
    "reg", "explorer", "conhost", "forfiles", "pcalua", "msbuild", "installutil", "regasm", "regsvcs",
}
REFUSED_PREFIXES = ("python", "py", "perl", "ruby", "php")


def checked_command(command: list[str]) -> list[str]:
    """``command`` with its program verified to be an existing .exe that is not a shell or
    script host, and {file} ensured."""
    program = Path(command[0])
    stem = program.stem.lower()
    if stem in REFUSED_PROGRAMS or stem.startswith(REFUSED_PREFIXES):
        raise PermissionError(f"このプログラムは PDF アプリに指定できません: {program.name}")
    if program.suffix.lower() != ".exe" or not program.is_file():
        raise FileNotFoundError(f"プログラムが見つかりません: {command[0]}")
    args = [str(a) for a in command[1:]] or ["{file}"]
    if not any("{file}" in arg for arg in args):
        args.append("{file}")
    return [str(program.resolve()), *args]


# Arguments that open a page in viewers people commonly use; any other program gets
# "{file}" and its arguments are the user's to write.
PDF_PRESETS = {
    "sumatrapdf": "-reuse-instance -page {page} {file}",
    "acrobat": "/A page={page} {file}",
    "acrord32": "/A page={page} {file}",
}


def preset_args(program: str) -> str:
    """The preset for a program, matched by the start of its file name, so versioned portable
    builds (SumatraPDF-3.5.2-64.exe) are recognised too."""
    stem = Path(program).stem.lower()
    return next((args for name, args in PDF_PRESETS.items() if stem.startswith(name)), "{file}")


def _split_args(args: str) -> list[str]:
    import shlex

    return shlex.split(args.replace("\\", "\\\\"), posix=True)


def available() -> bool:
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return True


# ---- handing pages to the window -----------------------------------------------------------


def _address(db: Path, kind: str = "") -> str:
    """The pipe of the window of ``kind`` ("" the viewer, "manage" the index manager)."""
    digest = hashlib.sha1(str(db.resolve()).lower().encode()).hexdigest()[:16]
    suffix = f"-{kind}" if kind else ""
    return rf"\\.\pipe\docwhere{suffix}-{getpass.getuser()}-{digest}"


KEY_BYTES = 32
MAX_MESSAGE = 4096


def _key(db: Path) -> bytes:
    """The pipe's shared secret, readable by the current user only.

    Created exclusively (a second process racing to create it reads the first one's key) and,
    on Windows, given a DACL that grants the owner alone, so it stays private even when the
    index lives in a shared folder (DOCWHERE_DB).
    """
    path = db.with_name("window.key")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not private(path.parent):
        raise PermissionError(f"{path.parent} can be written by other users; the docwhere window is not used there")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    except FileExistsError:
        # a key someone else put there (a shared DOCWHERE_DB folder) would let them drive the
        # window and forge page signatures: only a key this user owns is used
        if not _owned_by_me(path):
            raise PermissionError(f"{path} belongs to another user; delete it to make a new one") from None
        key = path.read_bytes()
        if len(key) != KEY_BYTES:
            raise PermissionError(f"{path} is not a docwhere key; delete it to make a new one") from None
        _owner_only(path)  # also tightens a key made by an older version
        return key
    key = secrets.token_bytes(KEY_BYTES)
    try:
        _owner_only(path)
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


def _owned_by_me(path: Path) -> bool:
    if sys.platform != "win32":
        return path.stat().st_uid == os.getuid()
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    me = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    owner = win32security.GetFileSecurity(str(path), win32security.OWNER_SECURITY_INFORMATION).GetSecurityDescriptorOwner()
    return owner == me


# ---- page signatures -------------------------------------------------------------------------
#
# The window lets a page call into Python, so it must only do that for pages docwhere wrote.
# Each saved page gets a sidecar <page>.sig holding an HMAC of its bytes under a key derived
# from the user's own key file; anyone able to drop a page into the views folder cannot make
# a matching signature, and a page changed after saving no longer matches.


def _signing_key(db: Path) -> bytes:
    return hmac.new(_key(db), b"docwhere page signature", hashlib.sha256).digest()


def _signature_path(page: Path) -> Path:
    return page.with_name(page.name + ".sig")


def sign_page(db: Path, page: Path) -> None:
    digest = hmac.new(_signing_key(db), page.read_bytes(), hashlib.sha256).hexdigest()
    _signature_path(page).write_text(digest, encoding="ascii")


def page_signed(db: Path, page: Path) -> bool:
    try:
        expected = hmac.new(_signing_key(db), page.read_bytes(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, _signature_path(page).read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False


def _owner_only(path: Path) -> None:
    """Replace the file's inherited permissions with full control for its owner alone."""
    if sys.platform != "win32":
        return
    import ntsecuritycon
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, user)
    win32security.SetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, dacl, None,
    )


def _pack(message: dict) -> bytes:
    # JSON bytes, never pickle: even a caller holding the key can only name a page to show
    return json.dumps(message).encode()


def _unpack(data: bytes) -> dict:
    message = json.loads(data.decode())
    if not isinstance(message, dict):
        raise ValueError("bad message")
    return message


def _send(db: Path, message: dict, kind: str = "") -> bool:
    try:
        conn = Client(_address(db, kind), family="AF_PIPE", authkey=_key(db))
    except Exception:
        return False  # no window running
    try:
        conn.send_bytes(_pack(message))
        return conn.recv_bytes(MAX_MESSAGE) == b"ok"
    except Exception:
        return False
    finally:
        conn.close()


def show(db: Path, page: Path) -> bool:
    """Show ``page`` in the docwhere window, starting the window if none is running.
    False when the window cannot be used (no pywebview, or an index folder others can write
    to); the caller then opens the page in the browser."""
    if not private(db.parent):
        return False
    allow_foreground()
    if _send(db, {"page": str(page)}):
        return True
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    subprocess.Popen(
        [sys.executable, "-m", "docwhere", "--db", str(db), "window", str(page)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags, close_fds=True,
    )
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.3)
        if _send(db, {"ping": True}):
            return True
    return False


# ---- what the page may ask -------------------------------------------------------------------


OPEN_PDFS = 4
MIN_DPI, MAX_DPI = 72, 300
MIN_ZOOM, MAX_ZOOM = 0.6, 2.5
SLOW_CALL = 3.0  # seconds; slower bridge calls are logged
_log = logging.getLogger("docwhere.window")


def _setup_log(db: Path) -> None:
    """Bridge errors and slow calls go to window.log next to the index, for diagnosing a window
    that stops responding; the log is small and rotated."""
    from logging.handlers import RotatingFileHandler

    handler = RotatingFileHandler(db.with_name("window.log"), maxBytes=200_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(threadName)s %(message)s"))
    _log.addHandler(handler)
    _log.setLevel(logging.INFO)


def _logged(method):
    """A bridge method: refused unless the page showing is one docwhere signed, and logged
    when it fails or is slow."""

    @functools.wraps(method)
    def call(self, *args):
        if not self._trusted:
            _log.warning("%s refused: the page showing is not a signed docwhere page", method.__name__)
            raise PermissionError("このページからは操作できません")
        started = time.monotonic()
        try:
            return method(self, *args)
        except Exception:
            _log.exception("%s failed", method.__name__)
            raise
        finally:
            if (elapsed := time.monotonic() - started) > SLOW_CALL:
                _log.warning("%s took %.1fs", method.__name__, elapsed)

    return call


class Api:
    """Methods the reader script may call. Attributes are private so pywebview exposes only these."""

    def __init__(self, db: Path) -> None:
        self._db = db
        self._window = None
        self._pinned = False
        self._trusted = False  # set while a signed docwhere page is showing
        # PyMuPDF is not safe to use from several threads at once, and the bridge calls each
        # method on its own thread: every PDF access goes through this lock. Open documents
        # are kept, so a manual of thousands of pages is not reopened for every page turn.
        self._pdf_lock = threading.Lock()
        self._docs: dict[str, tuple[float, object]] = {}
        self._facts: dict[str, dict] = {}  # per open PDF: printed-page offset and outline

    def _open_pdf(self, pdf: str):
        import pymupdf

        mtime = Path(pdf).stat().st_mtime
        cached = self._docs.get(pdf)
        if cached and cached[0] == mtime:
            return cached[1]
        if cached:
            cached[1].close()
            self._facts.pop(pdf, None)
        if len(self._docs) >= OPEN_PDFS:
            oldest = next(iter(self._docs))
            self._docs.pop(oldest)[1].close()
            self._facts.pop(oldest, None)
        doc = pymupdf.open(pdf)
        self._docs[pdf] = (mtime, doc)
        return doc

    @_logged
    def page(self, pdf: str, page: int, terms: list, quotes: list, dpi: int = 0) -> str:
        """The page as an image; ``dpi`` follows the zoom so enlarged text stays sharp."""
        from docwhere.viewer import VIEW_DPI, _data_uri, render_page

        allowed(self._db, pdf, renditions=True, pdf_only=True)
        dpi = max(MIN_DPI, min(MAX_DPI, int(dpi or VIEW_DPI)))
        with self._pdf_lock:
            doc = self._open_pdf(pdf)
            data = render_page(pdf, int(page), _texts(terms), dpi, "jpeg", _texts(quotes), source=doc)
        return _data_uri(data)

    @_logged
    def zoom(self) -> dict:
        """The page size last chosen in the viewer: {"mode": "width" | "fit", "scale": float}."""
        saved = load_settings(self._db).get("zoom") or {}
        mode = saved.get("mode") if saved.get("mode") in ("width", "fit") else "width"
        try:
            scale = min(MAX_ZOOM, max(MIN_ZOOM, float(saved.get("scale", 1.0))))
        except (TypeError, ValueError):
            scale = 1.0
        return {"mode": mode, "scale": scale}

    @_logged
    def save_zoom(self, mode: str, scale: float) -> dict:
        save_settings(self._db, zoom={"mode": "fit" if mode == "fit" else "width",
                                       "scale": min(MAX_ZOOM, max(MIN_ZOOM, float(scale)))})
        return self.zoom()

    def _facts_of(self, pdf: str, doc) -> dict:
        """Printed-page offset and outline of an open PDF, worked out once."""
        from docwhere.folio import printed_offset

        if pdf not in self._facts:
            self._facts[pdf] = {"offset": printed_offset(doc) or 0, "toc": [[lvl, title, page] for lvl, title, page, *_ in doc.get_toc()]}
        return self._facts[pdf]

    @_logged
    def toc(self, pdf: str) -> list:
        """The PDF's outline as [level, title, page] rows."""
        allowed(self._db, pdf, renditions=True, pdf_only=True)
        with self._pdf_lock:
            return self._facts_of(pdf, self._open_pdf(pdf))["toc"]

    @_logged
    def links(self, pdf: str, page: int) -> dict:
        import pymupdf

        allowed(self._db, pdf, renditions=True, pdf_only=True)
        with self._pdf_lock:
            doc = self._open_pdf(pdf)
            page = int(page)
            if not 1 <= page <= doc.page_count:
                raise LookupError(f"no page {page}")
            p = doc[page - 1]
            w, h = p.rect.width, p.rect.height
            out = []
            for link in p.get_links():
                target = link.get("page", -1)
                if link["kind"] in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) and target is not None and target >= 0:
                    r = link["from"]
                    out.append({"x": r.x0 / w, "y": r.y0 / h, "w": r.width / w, "h": r.height / h, "page": target + 1})
            from docwhere.folio import printed

            facts = self._facts_of(pdf, doc)
            return {"pages": doc.page_count, "links": out, "offset": facts["offset"],
                    "printed": printed(facts["offset"], page), "trail": _outline_trail(facts["toc"], page)}

    @_logged
    def open(self, spec: str) -> str:
        from docwhere.opener import open_at

        body = json.loads(spec)
        path, anchor = str(body.get("path", "")), body.get("anchor") or {}
        if not isinstance(anchor, dict):
            raise ValueError("bad anchor")
        allowed(self._db, path, renditions=False)
        try:
            import pythoncom

            pythoncom.CoInitialize()
        except ImportError:
            pythoncom = None
        try:
            return f"{Path(path).name}: {open_at(path, anchor, pdf_app(self._db))}"
        finally:
            if pythoncom is not None:
                pythoncom.CoUninitialize()

    @_logged
    def open_url(self, url: str) -> str:
        """Open a page of an added wiki in the browser; any other address is refused."""
        import webbrowser

        from docwhere.wiki import origin, safe_url
        from docwhere.wiki.registry import wikis

        url = str(url)
        if not url.startswith(("http://", "https://")) or origin(url) not in {origin(w["url"]) for w in wikis(self._db)}:
            raise PermissionError("登録した wiki のページではありません")
        webbrowser.open(safe_url(url))
        return "ブラウザで開きました"

    @_logged
    def copy(self, text: str) -> bool:
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(str(text)[:20000], win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        return True

    @_logged
    def settings(self) -> dict:
        command = pdf_app(self._db).get("command")
        return {
            "title": title(self._db),
            "viewer": mode(self._db),
            "pdf_program": command[0] if command else "",
            "pdf_args": subprocess.list2cmdline(command[1:]) if command else "",
        }

    @_logged
    def preset(self, program: str) -> str:
        return preset_args(str(program))

    @_logged
    def choose_program(self) -> str | None:
        import webview

        if self._window is None:
            return None
        chosen = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=("プログラム (*.exe)",))
        return str(chosen[0]) if chosen else None

    @_logged
    def save_settings(self, values: dict) -> dict:
        """Validate everything first, then save; a bad entry changes nothing."""
        program = str(values.get("pdf_program") or "").strip()
        command = checked_command([program, *_split_args(str(values.get("pdf_args") or ""))]) if program else None
        viewer = "browser" if values.get("viewer") == "browser" else "window"
        set_pdf_app(self._db, command)
        set_title(self._db, str(values.get("title") or "")[:80])
        set_mode(self._db, viewer)
        _set_window_title(title(self._db))
        return self.settings()

    @_logged
    def try_pdf(self, program: str, args: str, pdf: str, page: int) -> str:
        """Open ``pdf`` at ``page`` with the program being set up, before it is saved."""
        from docwhere.opener import open_at

        allowed(self._db, pdf, renditions=True, pdf_only=True)
        program = str(program or "").strip()
        command = checked_command([program, *_split_args(str(args or ""))]) if program else None
        return f"{Path(pdf).name}: {open_at(pdf, {'page': int(page)}, {'command': command} if command else None)}"

    @_logged
    def pin(self, on: bool) -> bool:
        self._pinned = bool(on)
        _set_topmost(self._pinned)
        save_settings(self._db, pinned=self._pinned)
        return self._pinned

    @_logged
    def pinned(self) -> bool:
        return self._pinned


def allowed(db: Path, path: str, renditions: bool, pdf_only: bool = False) -> None:
    """Only files in the index (and, for reading, docwhere's own PDF exports) may be touched."""
    from docwhere.index import Index, path_key

    if not path:
        raise PermissionError("no file given")
    resolved = Path(path).resolve()
    if pdf_only and resolved.suffix.lower() != ".pdf":
        raise PermissionError("not a PDF")
    if renditions and resolved.parent == db.with_name("renditions").resolve():
        return
    index = Index(db)
    try:
        known = index.db.execute("SELECT 1 FROM files WHERE key = ?", (path_key(resolved),)).fetchone()
    finally:
        index.close()
    if known is None:
        raise PermissionError("not an indexed file")


def _own_window() -> int | None:
    """This process's top-level window, found through Win32.

    pywebview's own window calls (on_top, restore, show) marshal onto the UI thread and wait
    for it; called from a bridge or pipe thread they can deadlock the window ("not
    responding"). The Win32 calls below only post to the window, so they are safe anywhere.
    """
    import win32gui
    import win32process

    found: list[int] = []

    def visit(hwnd, _):
        if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == os.getpid():
            found.append(hwnd)
        return True

    win32gui.EnumWindows(visit, None)
    return found[0] if found else None


def _placement(hwnd: int) -> dict:
    """The window's restored size and position, and whether it is maximized.

    GetWindowPlacement gives the normal (restored) rectangle even while maximized or
    minimized, so un-maximizing next time returns to the size the user chose, not the screen.
    Its coordinates are workspace-relative, which matches where pywebview places the window
    on a monitor without a taskbar on the left or top; the small offset otherwise is harmless.
    """
    import win32con
    import win32gui

    _, show, _, _, (left, top, right, bottom) = win32gui.GetWindowPlacement(hwnd)
    return {"width": right - left, "height": bottom - top, "x": left, "y": top,
            "maximized": show == win32con.SW_SHOWMAXIMIZED}


def _usable_geometry(geometry: dict) -> dict:
    """``geometry`` if its title bar would land on a connected monitor, else size only.

    A position saved on a monitor that has since been unplugged would open the window out of
    sight; then the system default position is used instead.
    """
    if geometry.get("x") is None or geometry.get("y") is None:
        return geometry
    try:
        import win32api
        import win32con

        point = (int(geometry["x"]) + 40, int(geometry["y"]) + 15)  # a spot inside the title bar
        if win32api.MonitorFromPoint(point, win32con.MONITOR_DEFAULTTONULL):
            return geometry
    except Exception:
        pass
    return {k: v for k, v in geometry.items() if k not in ("x", "y")}


def _set_window_title(text: str) -> None:
    import win32gui

    if (hwnd := _own_window()) is not None:
        win32gui.SetWindowText(hwnd, text)


def _set_topmost(on: bool) -> None:
    import win32con
    import win32gui

    if (hwnd := _own_window()) is None:
        return
    flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE | win32con.SWP_ASYNCWINDOWPOS
    win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST if on else win32con.HWND_NOTOPMOST, 0, 0, 0, 0, flags)


def _bring_to_front(pinned: bool) -> None:
    """Raise and activate the window when it opens or is handed a new page: the answer was
    just asked for.

    Windows lets a background process take the focus only in some cases. The sender grants
    it (allow_foreground) and the window's UI thread is briefly attached to the foreground
    window's input so SetForegroundWindow is honoured. Should activation still be refused,
    being topmost for a moment at least puts the window on top of the others, where it stays
    after ``pinned`` is restored.
    """
    import win32con
    import win32gui
    import win32process

    if (hwnd := _own_window()) is None:
        return
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindowAsync(hwnd, win32con.SW_RESTORE)
    _set_topmost(True)
    time.sleep(0.2)
    _set_topmost(pinned)
    foreground = win32gui.GetForegroundWindow()
    if foreground == hwnd:
        return
    ours = win32process.GetWindowThreadProcessId(hwnd)[0]
    theirs = win32process.GetWindowThreadProcessId(foreground)[0] if foreground else 0
    attached = False
    try:
        if theirs and theirs != ours:
            attached = bool(win32process.AttachThreadInput(ours, theirs, True))
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except Exception as e:  # refused: the window is still on top, only not focused
        _log.info("could not activate the window: %s", e)
    finally:
        if attached:
            win32process.AttachThreadInput(ours, theirs, False)


def allow_foreground() -> None:
    """Let the viewer process take the focus. Called by the sender, which runs in the terminal
    the user is working in and so may grant it; harmless when it may not."""
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY


def _outline_trail(toc: list, page: int) -> list[list]:
    """The outline headings that contain ``page``, outermost first, as [title, page] pairs."""
    trail: list[list] = []
    for level, title, start in toc:
        if start > page:
            break
        if start >= 1:
            trail = trail[: level - 1] + [[title, start]]
    return trail


def _texts(values) -> list[str]:
    return [str(v)[:MAX_TEXT] for v in values if isinstance(v, str)][:20] if isinstance(values, list) else []


# ---- the window process ----------------------------------------------------------------------


def fresh_storage(db: Path, name: str = "webview") -> Path:
    """An empty folder for WebView2's own files, next to the index.

    Left to itself, pywebview's private mode hands WebView2 a temporary folder name that is
    deleted at once; WebView2 recreates it and nobody removes it, so each window start left
    several MB in %TEMP%. One folder, emptied at each start, keeps that to one window's worth.
    Only one window runs per index, so nothing is using the old contents by then.
    """
    folder = db.with_name(name)
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def run(db: Path, page: Path) -> None:
    """Open the window on ``page`` and serve later pages handed over the pipe until it closes."""
    import webview

    from docwhere.viewer import APP_JS

    views = db.with_name("views").resolve()
    if not private(db.parent):
        return  # see private(): pages there may have been swapped after they were signed
    try:
        listener = Listener(_address(db), family="AF_PIPE", authkey=_key(db))
    except Exception:
        _send(db, {"page": str(page)})  # another window took the pipe first
        return

    _setup_log(db)
    _log.info("window started on %s", page.name)
    settings = load_settings(db)
    geometry = _usable_geometry(settings.get("window") or {})
    api = Api(db)
    api._pinned = bool(settings.get("pinned"))
    window = webview.create_window(
        title(db), url=page.resolve().as_uri(), js_api=api,
        width=geometry.get("width", DEFAULT_SIZE[0]), height=geometry.get("height", DEFAULT_SIZE[1]),
        x=geometry.get("x"), y=geometry.get("y"), maximized=bool(geometry.get("maximized")),
        on_top=api._pinned, min_size=(640, 480), text_select=True,
    )
    api._window = window

    def distrust() -> None:
        api._trusted = False  # a new page is loading; nothing is trusted until it is checked

    def inject() -> None:
        # the reader script, and with it every bridge call, only for a page docwhere signed
        url = urlparse(window.get_current_url() or "")
        shown = Path(url2pathname(url.path)) if url.scheme == "file" else None
        if shown is not None and shown.resolve().parent == views and page_signed(db, shown):
            api._trusted = True
            window.evaluate_js(APP_JS)
        else:
            _log.warning("not a signed docwhere page, reader left off: %s", url.geturl()[:200])

    def remember() -> None:
        try:
            if (hwnd := _own_window()) is not None:
                save_settings(db, window=_placement(hwnd))
        except Exception:
            pass

    closing = threading.Event()

    def serve() -> None:
        # Nothing may end this loop but the window closing: a dead loop leaves later pages
        # unanswered while the window looks fine.
        while not closing.is_set():
            try:
                conn = listener.accept()
            except Exception:
                if closing.is_set():
                    return
                _log.exception("accepting a page failed")  # e.g. a caller without the key
                time.sleep(0.2)  # never spin if accepting keeps failing
                continue
            try:
                handle(conn)
            except Exception:
                _log.exception("handling a page failed")

    def handle(conn) -> None:
        target = None
        try:
            message = _unpack(conn.recv_bytes(MAX_MESSAGE))
            target = Path(str(message.get("page", ""))) if isinstance(message, dict) else None
            # answer first: the sender (a docwhere view in the terminal) must not wait on the page load
            conn.send_bytes(b"ok")
        finally:
            conn.close()
        if target is None or target.resolve().parent != views or target.suffix != ".html":
            return
        _log.info("showing %s", target.name)
        try:
            _bring_to_front(api._pinned)  # before loading, so the window comes up even if the load is slow
        except Exception:
            _log.exception("raising the window failed")
        window.load_url(target.resolve().as_uri())

    window.events.before_load += distrust
    window.events.loaded += inject
    window.events.shown += lambda: _bring_to_front(api._pinned)  # started behind the terminal
    window.events.closing += remember
    window.events.closing += closing.set
    threading.Thread(target=serve, daemon=True).start()
    try:
        webview.start(private_mode=True, gui="edgechromium", storage_path=str(fresh_storage(db)))
    finally:
        closing.set()
        listener.close()
