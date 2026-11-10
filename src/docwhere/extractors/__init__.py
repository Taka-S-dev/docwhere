"""Format-specific text extractors.

Each extractor turns one file into a list of Chunks. To support a new format, add a module
to ``_MODULES`` and decorate its entry point with ``@register(".ext")``. Keep third-party
imports inside the function so commands that never extract (open, show) start fast.

Extractors that drive a desktop application (``serial=True``) run one file at a time in the
main process and may hold the application open between files; ``release()`` closes it.
"""

from __future__ import annotations

import importlib
import zipfile
from collections.abc import Callable
from pathlib import Path

from docwhere.model import Chunk

# Bump whenever extractor output changes; files indexed by an older version are re-extracted.
EXTRACTOR_VERSION = 5

# Office Open XML files are zip archives that the parsers inflate in memory. A small file can
# declare gigabytes of content, so the declared total is checked before any parser touches it.
MAX_UNZIPPED_BYTES = 1 << 30
_ZIP_BASED = {".docx", ".docm", ".xlsx", ".xlsm", ".pptx", ".pptm"}

Extractor = Callable[[Path], list[Chunk]]

_MODULES = ("pdf", "word", "excel", "powerpoint", "text", "html", "legacy")
_REGISTRY: dict[str, Extractor] = {}
_SERIAL: set[str] = set()
_RELEASERS: list[Callable[[], None]] = []


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
