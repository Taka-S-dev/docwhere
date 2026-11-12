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
import zipfile
from collections.abc import Callable
from pathlib import Path

from docwhere.model import Chunk

# Bump on every extractor change: files that could not be read are tried again, since the
# change may let them be read.
EXTRACTOR_VERSION = 7
# The oldest version whose output is still current. Set it to EXTRACTOR_VERSION when the
# output changes, so that every file is re-extracted.
OUTPUT_VERSION = 5

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
    return path.suffix.lower() in _SERIAL


def extract(path: Path) -> list[Chunk]:
    _load()
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
