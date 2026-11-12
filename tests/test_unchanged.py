"""A file whose date changed but whose content did not (a checkout, a revert, a copy) keeps what
was read, without being read again."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from docwhere.index import Index


def _redate(path: Path, text: str | None = None) -> None:
    if text is not None:
        path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def _bodies(index: Index) -> dict[str, str]:
    return {
        Path(r["path"]).name: r["body"]
        for r in index.db.execute(
            "SELECT f.path, group_concat(c.body, '|') AS body FROM files f LEFT JOIN chunks c ON c.file_id = f.id GROUP BY f.id"
        )
    }


def test_a_re_dated_file_is_not_read_again_and_a_changed_one_is(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "same.md").write_text("# A\ntimeout 30\n", encoding="utf-8")
    (root / "edited.md").write_text("# B\ntimeout 30\n", encoding="utf-8")  # same size after the edit
    (root / "longer.md").write_text("# C\ntimeout 30\n", encoding="utf-8")
    index = Index(tmp_path / "index.db")
    assert index.sync(root).updated == 3

    _redate(root / "same.md")
    _redate(root / "edited.md", "# B\ntimeout 60\n")
    _redate(root / "longer.md", "# C\ntimeout 30 seconds\n")
    stats = index.sync(root)
    assert (stats.updated, stats.unchanged) == (3, 1)
    bodies = _bodies(index)
    assert "30" in bodies["same.md"] and "60" in bodies["edited.md"] and "seconds" in bodies["longer.md"]
    assert index.scan(root).todo == []  # the new date was recorded


def test_a_file_that_could_not_be_read_stays_unreadable_when_only_re_dated(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "broken.docx").write_bytes(b"not a zip")
    index = Index(tmp_path / "index.db")
    assert index.sync(root).failed == 1
    _redate(root / "broken.docx")
    stats = index.sync(root)
    assert stats.unchanged == 1 and [p for p, _ in index.errors()] == [str(root / "broken.docx")]


def test_an_index_from_before_hashes_reads_a_re_dated_file_once_then_hashes_it(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.md").write_text("# A\nbody\n", encoding="utf-8")
    db = tmp_path / "index.db"
    index = Index(db)
    index.sync(root)
    index.close()
    with sqlite3.connect(db) as raw:
        raw.execute("UPDATE files SET sha256 = NULL")  # as an index made before hashes were kept
    index = Index(db)
    _redate(root / "a.md")
    assert index.sync(root).unchanged == 0  # nothing to compare with yet: read
    _redate(root / "a.md")
    assert index.sync(root).unchanged == 1
