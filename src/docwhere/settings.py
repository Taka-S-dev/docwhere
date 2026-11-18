"""The user's settings (settings.json beside the index) and the check that guards them.

Settings name programs docwhere starts (the PDF app) and the wikis it talks to, so they are
read only from an index folder no one else can write to; see ``private``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

SAVE_WAIT = 5.0  # seconds a save waits for another window's save to finish


def settings_path(db: Path) -> Path:
    return db.with_name("settings.json")


def load_settings(db: Path) -> dict:
    """The saved settings; none are read from an index folder others can write to (see
    private()), where a planted file could name the program that opens PDFs."""
    if not private(db.parent):
        return {}
    try:
        return json.loads(settings_path(db).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(db: Path, **changes) -> None:
    """Add ``changes`` to the saved settings, keeping the rest.

    The index window and the reading window both save here, the reading window whenever it is
    moved or resized. Each save reads, adds and writes under a lock, so two at once do not drop
    each other's changes, and writes a whole new file in place of the old, so no one reads a file
    half written. A file that cannot be read is not written over with the change alone: that
    dropped every other setting (the PDF app, the title) the user had chosen.

    Nothing is saved in an index folder others can write to: settings are not read from there
    (see load_settings), and a file they put there in place of the lock or the new file would
    be written through.
    """
    path = settings_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not private(path.parent):
        raise PermissionError(f"ほかの人も書き込めるインデックスのフォルダなので、設定は保存しません（{path.parent}）")
    with _locked(path.with_name("settings.lock")):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = {}
        else:
            try:
                current = json.loads(text)
            except ValueError as e:
                raise ValueError(f"設定ファイルが読めないため、保存しませんでした（{path}: {e}）") from None
            if not isinstance(current, dict):
                raise TypeError(f"設定ファイルの形が違うため、保存しませんでした（{path}）")
        # a new file of its own, under a name no one can foresee, then put in the settings' place
        fd, tmp = tempfile.mkstemp(prefix="settings.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps({**current, **changes}, ensure_ascii=False, indent=1))
            deadline = time.monotonic() + SAVE_WAIT
            while True:
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:  # Windows: someone is reading it right now; a moment later it is free
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.02)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


@contextmanager
def _locked(lock: Path):
    """Hold ``lock`` (a file beside the settings) for one save, across processes and threads."""
    deadline = time.monotonic() + SAVE_WAIT
    with open(lock, "a+b") as f:
        while True:
            try:
                f.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError("設定を保存できませんでした（ほかの窓が保存を続けています）") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            f.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f, fcntl.LOCK_UN)


def read_without_office(db: Path) -> bool:
    """Whether a .doc and a .ppt are read without Office (off unless turned on with ``docwhere
    doc-reader fast``, after ``docwhere doc-check`` found it reads the files of this PC as Word
    and PowerPoint do)."""
    return load_settings(db).get("doc_reader") == "fast"


def count_pictures(db: Path) -> bool:
    """Whether a PDF's pictures are counted when it is read (on unless turned off): the count
    lets a search say that a page holds pictures whose words are not read, and makes reading a
    PDF with many of them take about half as long again."""
    return load_settings(db).get("count_pictures") is not False


# How much of a file's place a source names, as set in the settings dialog: the file's name,
# its path from the indexed folder it lies in (folders often name the document, and the
# indexed folder is where the user chose to start), or the whole path.
CITE_MODES = ("name", "root", "full")


def cite_mode(db: Path) -> str:
    mode = load_settings(db).get("cite_path")
    return mode if mode in CITE_MODES else "root"


def cite_name(path: str, mode: str, roots: list[str] = ()) -> str:
    """The file as a source names it (see CITE_MODES). With "root", the outermost of ``roots``
    the file lies in is named with the path below it; a file in none is named alone."""
    p = Path(path)
    if mode == "full":
        return str(p)
    if mode == "root":
        key = os.path.normcase(str(p))
        inside = [Path(r) for r in roots if key.startswith(os.path.normcase(str(Path(r))).rstrip("\\/") + os.sep)]
        if inside:
            top = min(inside, key=lambda r: len(str(r)))  # a folder inside another: from the outer one
            below = str(p)[len(str(top).rstrip("\\/")):].strip("\\/")  # matched ignoring case above
            return f"{top.name or str(top).rstrip(chr(92))}\\{below}"
    return p.name


# SYSTEM and the local Administrators group can write anywhere anyway.
_TRUSTED_SIDS = {"S-1-5-18", "S-1-5-32-544"}
_INHERIT_ONLY_ACE = 0x08


def private(folder: Path) -> bool:
    """Whether only this user can put files in ``folder`` (the index folder).

    The window trusts what it finds there: the key file, saved pages and their signatures,
    the settings below. In a folder others can write to (a shared DOCWHERE_DB), they could swap a page
    after it was checked, read a key while it is being made, or plant a settings file, so the
    window is not used there and pages open in the browser, where they run no script at all.

    Inside the user's profile the folder must be the user's own; profiles are per-user by
    design, and other principals granted there were granted by the machine's owner. Elsewhere
    no one but the user, SYSTEM and Administrators may hold a right that writes into it.
    """
    folder = folder.resolve()
    if not folder.exists():
        return private(folder.parent) if folder.parent != folder else False
    if sys.platform != "win32":
        st = folder.stat()
        return st.st_uid == os.getuid() and not st.st_mode & 0o022
    import ntsecuritycon as nc
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    me = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    info = win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION
    sd = win32security.GetFileSecurity(str(folder), info)
    if sd.GetSecurityDescriptorOwner() != me:
        return False
    profile = Path(os.environ.get("USERPROFILE", "")).resolve() if os.environ.get("USERPROFILE") else None
    if profile is not None and folder.is_relative_to(profile):
        return True
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:  # a NULL DACL grants everyone everything
        return False
    writes = (nc.FILE_WRITE_DATA | nc.FILE_APPEND_DATA | nc.FILE_WRITE_EA | nc.FILE_WRITE_ATTRIBUTES | nc.DELETE
              | nc.WRITE_DAC | nc.WRITE_OWNER | nc.GENERIC_WRITE | nc.GENERIC_ALL | nc.FILE_DELETE_CHILD)
    for i in range(dacl.GetAceCount()):
        (kind, flags), mask, sid = dacl.GetAce(i)
        if kind != win32security.ACCESS_ALLOWED_ACE_TYPE or flags & _INHERIT_ONLY_ACE or sid == me:
            continue
        if win32security.ConvertSidToStringSid(sid) in _TRUSTED_SIDS:
            continue
        if mask & writes:
            return False
    return True
