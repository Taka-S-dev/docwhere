"""How long each file took to read is kept, so status can say where indexing spends its time."""

from __future__ import annotations

import json

from docwhere import workers
from docwhere.cli import main


def _search(capsys, root, *extra: str) -> dict:
    capsys.readouterr()
    assert main(["search", "timeout", "--in", str(root), "--json", *extra]) == 0
    return json.loads(capsys.readouterr().out)


def _slow(path):
    import time

    time.sleep(0.3)
    return path


def test_the_time_a_file_took_is_measured_in_its_worker():
    [outcome] = workers.run([workers.Task("a", False)], _slow, 1, timeout=60)
    assert 0.3 <= outcome.seconds < 5


def test_status_says_how_long_each_kind_took_to_read(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "db" / "index.db"))
    root = tmp_path / "docs"
    root.mkdir()
    for name in ("a.md", "b.md", "c.txt"):
        (root / name).write_text("# T\ntimeout\n", encoding="utf-8")
    _search(capsys, root)
    main(["status", "--json"])
    times = json.loads(capsys.readouterr().out)["read_times"]
    assert {ext: t["files"] for ext, t in times.items()} == {".md": 2, ".txt": 1}
    assert all(t["seconds"] >= 0 and t["average"] >= 0 for t in times.values())
    main(["status"])
    assert "reading time by kind: " in capsys.readouterr().out
