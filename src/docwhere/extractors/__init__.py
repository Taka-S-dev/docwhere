"""Format-specific text extractors.

Each extractor turns one file into a list of Chunks. To support a new format, add a module
to ``_MODULES`` and decorate its entry point with ``@register(".ext")``. Keep third-party
imports inside the function so commands that never extract (open, show) start fast.

Extractors that drive a desktop application (``serial=True``) run one file at a time in one
process and may hold the application open between files; ``release()`` closes it. They report
the application's process through ``process_started`` so that a file stuck in it can be
stopped together with the application.

An extractor that reads most files on its own but needs the application for some (``.xls``)
is registered without ``serial`` and raises ``NeedsApplication`` for those when it runs in a
pooled worker; the file is then read again in the application's worker.
"""

from __future__ import annotations

import importlib
import os
import zipfile
from collections.abc import Callable
from pathlib import Path

from docwhere.model import Chunk

# Bump on every extractor change: files that could not be read are tried again, since the
# change may let them be read.
EXTRACTOR_VERSION = 16
# The oldest version whose output is still current. Set it to EXTRACTOR_VERSION when the
# output changes, so that every file is re-extracted.
# 11: dashes and the wave dash are folded to one form each (model.norm), in every format
# 12: a CSV row is labelled by its header; UTF-16 text is read
OUTPUT_VERSION = 12
# The same for one format, when only its output changed: those files alone are read again.
# 9: a PDF's pages that are pictures without text are noted (``note``)
# 10: a bookmarked PDF's running headers and footers are left out of each page's text
# 14: a bookmarked PDF's table rows and wrapped sentences are one line each, its symbol font's
#     marks are the marks they show, and the page number and chapter tab are left out
# 15: a PDF's pictures are counted page by page (``pictures``); its text is as in 14
# 16: a .doc may be read without Word (extractors.msdoc), as Word reads it: a .doc Word could
#     not read is tried again
# .docx 13: text in tracked insertions, content controls and text boxes is read
OUTPUT_VERSION_BY_EXT = {".pdf": 15, ".docx": 13, ".docm": 13}

# Office Open XML files are zip archives that the parsers inflate in memory. A small file can
# declare gigabytes of content, so the declared total is checked before any parser touches it.
MAX_UNZIPPED_BYTES = 1 << 30
_ZIP_BASED = {".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm"}

Extractor = Callable[[Path], list[Chunk]]

_MODULES = ("pdf", "word", "excel", "powerpoint", "text", "html", "legacy")
_REGISTRY: dict[str, Extractor] = {}
_SERIAL: set[str] = set()
_RELEASERS: list[Callable[[], None]] = []
_PROCESS_LISTENERS: list[Callable[[int], None]] = []
_worker: str | None = None  # "pooled" or "serial" inside a worker process, None outside one


class NeedsApplication(Exception):
    """This file can only be read through a desktop application: read it in the serial worker."""


def set_worker(kind: str | None) -> None:
    global _worker
    _worker = kind


def may_start_application() -> bool:
    """False in a pooled worker, where several applications would run at once."""
    return _worker != "pooled"


def register(*exts: str, serial: bool = False) -> Callable[[Extractor], Extractor]:
    def deco(fn: Extractor) -> Extractor:
        for ext in exts:
            _REGISTRY[ext.lower()] = fn
            if serial:
                _SERIAL.add(ext.lower())
        return fn

    return deco


def on_release(fn: Callable[[], None]) -> Callable[[], None]:
    _RELEASERS.append(fn)
    return fn


_loaded = False


def _load() -> None:
    # A flag, not "is the registry empty": importing one extractor directly (html_chunks for
    # wiki pages) registers that module alone and must not hide the others.
    global _loaded
    if not _loaded:
        for name in _MODULES:
            importlib.import_module(f"{__name__}.{name}")
        _loaded = True


def supported_extensions() -> frozenset[str]:
    _load()
    return frozenset(_REGISTRY)


def is_serial(path: Path) -> bool:
    _load()
    ext = path.suffix.lower()
    if ext in (".doc", ".ppt") and reading_without_office():
        return False  # read alongside the other files; the few it leaves to Word are handed on
    return ext in _SERIAL


_notes: list[str] = []


def note(text: str) -> None:
    """Say something of the file being read that its sections do not show (part of it could not
    be read, say); kept with the file in the index."""
    _notes.append(text)


def notes() -> list[str]:
    """What ``note`` was told while the last file was read."""
    return list(_notes)


COUNT_PICTURES_ENV = "DOCWHERE_COUNT_PICTURES"  # "0": pictures are not counted (the setting, see Index.extract)
_pictures: dict[int, tuple[int, float]] = {}


def counting_pictures() -> bool:
    return os.environ.get(COUNT_PICTURES_ENV) != "0"


DOC_READER_ENV = "DOCWHERE_DOC_READER"  # "fast": .doc and .ppt read without Office (the setting, see Index.extract)


def reading_without_office() -> bool:
    return os.environ.get(DOC_READER_ENV) == "fast"



def pictures(found: dict[int, tuple[int, float]] | None = None) -> dict[int, tuple[int, float]]:
    """The pictures of the file being read whose text no search reaches: page -> (how many, the
    share of the page they cover). Told by a reader with ``found``; asked without."""
    if found is not None:
        _pictures.update(found)
    return dict(_pictures)


def extract(path: Path) -> list[Chunk]:
    _load()
    _notes.clear()
    _pictures.clear()
    if path.suffix.lower() in _ZIP_BASED:
        _check_unzipped_size(path)
    return _REGISTRY[path.suffix.lower()](path)


def _check_unzipped_size(path: Path) -> None:
    with zipfile.ZipFile(path) as zf:
        total = sum(info.file_size for info in zf.infolist())
    if total > MAX_UNZIPPED_BYTES:
        raise ValueError(f"expands to {total >> 20} MB, over the {MAX_UNZIPPED_BYTES >> 20} MB limit")


def release() -> None:
    """Close applications that serial extractors kept open."""
    for fn in _RELEASERS:
        fn()


def on_process_started(fn: Callable[[int], None]) -> None:
    """Call ``fn(pid)`` when an extractor starts an application of its own."""
    _PROCESS_LISTENERS.append(fn)


def process_started(pid: int) -> None:
    for fn in _PROCESS_LISTENERS:
        fn(pid)
