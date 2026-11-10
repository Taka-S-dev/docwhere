"""The user's settings (settings.json beside the index) and the check that guards them.

Settings name programs docwhere starts (the PDF app) and the wikis it talks to, so they are
read only from an index folder no one else can write to; see ``private``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


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
    settings = {**load_settings(db), **changes}
    path = settings_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")


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
