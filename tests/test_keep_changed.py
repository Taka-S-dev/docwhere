"""search --keep-changed: files modified since they were read are searched as last read, so the
user can decide whether reading them again is worth the wait."""

from __future__ import annotations

import json
import os
from pathlib import Path

from docwhere.cli import main


def _search(capsys, root: Path, *extra: str) -> dict:
    capsys.readouterr()
    assert main(["search", "timeout", "--in", str(root), "--json", *extra]) == 0
    return json.loads(capsys.readouterr().out)


def _touch_later(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


def test_changed_files_are_kept_as_read_and_new_ones_are_still_read(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "db" / "index.db"))
    root = tmp_path / "docs"
    root.mkdir()
    spec = root / "spec.md"
    spec.write_text("# Spec\ntimeout is 30 seconds\n", encoding="utf-8")
    assert _search(capsys, root)["changed"] == {"count": 0, "kept": False, "files": []}

    _touch_later(spec, "# Spec\ntimeout is 60 seconds\n")
    (root / "new.md").write_text("# New\ntimeout is 90 seconds\n", encoding="utf-8")
    out = _search(capsys, root, "--keep-changed")
    assert out["changed"] == {"count": 1, "kept": True, "files": [str(spec)]}
    snippets = {Path(h["path"]).name: h for h in out["hits"]}
    assert "30" in snippets["spec.md"]["snippet"] and snippets["spec.md"]["changed_since_read"] is True
    assert "changed_since_read" not in snippets["new.md"]  # never read before: read now

    capsys.readouterr()
    main(["search", "timeout", "--in", str(root), "--keep-changed"])
    captured = capsys.readouterr()
    assert "modified since it was read" in captured.out
    assert "1 file(s) modified since they were read were searched as last read" in captured.err

    out = _search(capsys, root)  # without the option the change is read
    assert out["changed"] == {"count": 1, "kept": False, "files": [str(spec)]}
    [hit] = [h for h in out["hits"] if h["path"] == str(spec)]
    assert "60" in hit["snippet"] and "changed_since_read" not in hit
    assert _search(capsys, root, "--keep-changed")["changed"]["count"] == 0

    assert _search(capsys, root, "--no-update")["changed"] is None

