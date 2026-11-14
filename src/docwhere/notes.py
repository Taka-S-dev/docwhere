"""The notes map (``notes.csv`` beside the index), read and written for the index window.

The file is the user's: they edit it by hand or in Excel, and the agent appends to it. So a
save keeps what this module does not use: comment lines (``#``) stay where they were, columns
other than the four known ones keep their values, and the header keeps its order. It is
written as Excel reads it (UTF-8 with BOM, CRLF), and only if the file has not changed since
it was loaded, so an edit made meanwhile in Excel is not overwritten.
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
from pathlib import Path

FIELDS = ("path", "section", "kind", "note")
MAX_ITEMS = 5000
MAX_CELL = 2000


class Changed(Exception):
    """The file changed on disk since it was loaded."""


def notes_path(db: Path) -> Path:
    return db.with_name("notes.csv")


def version_of(path: Path) -> str | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return f"{st.st_mtime_ns}-{st.st_size}"


def load(db: Path) -> dict:
    """``header``, ``items`` (each ``{"comment": text}`` or ``{"cells": {column: value}}``, in
    file order) and the ``version`` a save must match (None: no file yet)."""
    path = notes_path(db)
    version = version_of(path)
    if version is None:
        return {"path": str(path), "version": None, "header": list(FIELDS), "items": []}
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:  # a line break inside a field kept as written
        text = f.read()
    header: list[str] | None = None
    items: list[dict] = []
    pending = ""
    for line in text.splitlines(keepends=True):
        if not pending and line.lstrip().startswith("#"):
            items.append({"comment": line.rstrip("\r\n")})
            continue
        pending += line
        if pending.count('"') % 2:  # a quoted field goes on to the next line
            continue
        record, pending = pending, ""
        if not record.strip():
            continue
        row = next(csv.reader(io.StringIO(record)))
        if header is None:
            header = [h.strip() for h in row]
            continue
        cells = {name: (row[i] if i < len(row) else "") for i, name in enumerate(header)}
        if len(row) > len(header):  # values past the header: kept under numbered names
            cells.update({f"_{i}": v for i, v in enumerate(row[len(header):], len(header))})
        items.append({"cells": cells})
    header = header or list(FIELDS)
    return {"path": str(path), "version": version, "header": header, "items": items}


def save(db: Path, items: list, version: str | None) -> str:
    """Write ``items`` (as ``load`` gives them) if the file is still at ``version``; returns
    the new version. Raises ``Changed`` when it is not, PermissionError when it is locked."""
    path = notes_path(db)
    if version_of(path) != version:
        raise Changed("the notes file was changed elsewhere since it was shown")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} rows")
    header = load(db)["header"] if version is not None else list(FIELDS)
    header += [f for f in FIELDS if f not in header]
    extra = sorted({k for it in items if isinstance(it, dict) for k in (it.get("cells") or {}) if k.startswith("_")},
                   key=lambda k: int(k[1:]) if k[1:].isdigit() else 0)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    for it in items:
        if not isinstance(it, dict):
            raise ValueError("bad row")
        if "comment" in it:
            comment = " ".join(str(it["comment"])[:MAX_CELL].splitlines()).strip()
            out.write((comment if comment.startswith("#") else f"# {comment}") + "\r\n")
            continue
        cells = it.get("cells")
        if not isinstance(cells, dict):
            raise ValueError("bad row")
        row = [str(cells.get(name) or "")[:MAX_CELL] for name in header + extra]
        known = max(header.index(f) for f in FIELDS) + 1
        while len(row) > known and not row[-1]:  # a column of the user's own left empty, as Excel writes it
            row.pop()
        writer.writerow(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".notes-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
            f.write(out.getvalue())
        os.replace(tmp, path)  # refused while Excel holds the file open
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return version_of(path)
