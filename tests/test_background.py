from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from docwhere import background
from docwhere.cli import main
from docwhere.index import Index, Stopped


@pytest.fixture
def spawned(monkeypatch) -> list[Path]:
    """Records reader starts instead of starting a process."""
    calls: list[Path] = []
    monkeypatch.setattr(background, "_spawn", calls.append)
    return calls


def _folder(tmp_path: Path, n: int = 5) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    for i in range(n):
        (root / f"f{i}.md").write_text(f"# 手順 {i}\nタイムアウトは{i}秒\n", encoding="utf-8")
    return root


def test_one_reader_serves_every_request_and_exits_when_done(tmp_path, index, spawned):
    root = _folder(tmp_path)
    assert background.request(index, root) is True
    assert background.request(index, root) is False  # already starting: not a second one
    assert background.request(index, root / ".." / "docs") is False
    assert spawned == [index.path]

    assert background.serve(Index(index.path)) == 0
    assert len(index.files_in([root])) == 5
    assert background.state(index) is None  # the reader is gone
    assert index.db.execute("SELECT count(*) FROM queue").fetchone()[0] == 0
    assert "5/5 files" in index.path.with_name(background.LOG_NAME).read_text(encoding="utf-8")


def test_a_reader_that_stopped_beating_is_replaced(tmp_path, index, spawned):
    root = _folder(tmp_path)
    background.request(index, root)
    with index.db:
        index.db.execute("UPDATE reader SET pid = 4242, heartbeat = ?", (time.time() - background.STALE - 1,))
    assert background.state(index) is None
    assert background.request(index, root) is True
    assert len(spawned) == 2


def test_stop_empties_the_queue_and_a_stopped_read_keeps_what_it_read(tmp_path, index, spawned):
    root = _folder(tmp_path)
    background.request(index, root)
    background.stop(index)
    background.serve(Index(index.path))
    assert index.files_in([root]) == []  # nothing left to read

    scan = index.scan(root)
    with pytest.raises(Stopped):
        index.extract(scan, should_stop=lambda: True)
    assert len(index.pending(scan.todo)) == 5


def test_a_search_answers_before_reading_finishes_and_says_so(tmp_path, spawned, monkeypatch, capsys):
    root = _folder(tmp_path)
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "db" / "index.db"))
    main(["search", "タイムアウト", "--in", str(root), "--wait", "0", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["reading"] == {
        "complete": False, "files": 5, "read": 0, "remaining": 5, "remaining_types": {".md": 5},
        "about_seconds_left": out["reading"]["about_seconds_left"], "background": True,  # starting
    }
    assert out["hits"] == []

    main(["search", "タイムアウト", "--in", str(root), "--wait", "0"])
    err = capsys.readouterr().err
    assert "not finished reading: this searched 0 of 5 files; 5 are still being read" in err
    assert "--wait-all" in err

    from docwhere.viewer import _reading_html

    band = _reading_html(json.loads((tmp_path / "db" / "last_search.json").read_text(encoding="utf-8"))["reading"])
    assert "5 件中 0 件を読んだ時点" in band
    assert _reading_html({"complete": True}) == ""


def test_a_small_change_is_read_by_the_search_itself(tmp_path, spawned, monkeypatch, capsys):
    root = _folder(tmp_path, 2)
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "db" / "index.db"))
    main(["search", "タイムアウト", "--in", str(root), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["reading"] == {"complete": True, "files": 2, "read": 2, "remaining": 0}
    assert len(out["hits"]) == 2 and spawned == []


def test_index_stop_and_status_report_the_reader(tmp_path, index, spawned, monkeypatch, capsys):
    root = _folder(tmp_path)
    background.request(index, root)
    monkeypatch.setenv("DOCWHERE_DB", str(index.path))
    main(["status", "--json"])
    assert json.loads(capsys.readouterr().out)["background"] == {"folder": None, "read": 0, "files": 0, "queued": [str(root)], "about_seconds_left": None}
    main(["index", "--stop"])
    assert "background reading stopped" in capsys.readouterr().out
    assert main(["index"]) == 2


def test_a_folder_searched_once_leaves_the_index_a_day_later(tmp_path, spawned, monkeypatch, capsys):
    root = _folder(tmp_path, 2)
    db = tmp_path / "db" / "index.db"
    monkeypatch.setenv("DOCWHERE_DB", str(db))
    main(["search", "タイムアウト", "--in", str(root), "--once", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert len(out["hits"]) == 2
    assert out["once"]["folders"] == [str(root.resolve())] and out["once"]["kept"] == []
    index = Index(db)
    assert index.lifetime(root) == "once"
    assert main(["show", "1"]) == 0  # the hits can still be shown
    assert background.expire(index) == []  # not yet

    later = time.time() + background.ONCE_KEEP + 1
    background.request(index, root)  # still queued for reading: left for a later call
    assert background.expire(index, now=later) == []
    index.db.execute("DELETE FROM queue")
    index.db.commit()
    assert background.expire(index, now=later) == [str(root.resolve())]
    assert index.roots() == [] and index.files_in([root]) == []


def test_once_keeps_a_folder_indexed_before_and_a_normal_search_keeps_a_once_folder(tmp_path, spawned, monkeypatch, capsys):
    root = _folder(tmp_path, 2)
    db = tmp_path / "db" / "index.db"
    monkeypatch.setenv("DOCWHERE_DB", str(db))
    main(["search", "タイムアウト", "--in", str(root), "--json"])
    capsys.readouterr()
    main(["search", "タイムアウト", "--in", str(root / ".."), "--once", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["once"]["folders"] == [str(tmp_path.resolve())]
    main(["search", "タイムアウト", "--in", str(root), "--once"])
    assert f"already indexed, so kept: {root.resolve()}" in capsys.readouterr().err

    index = Index(db)
    assert index.lifetime(root) == "kept"
    (tmp_path / "other.md").write_text("タイムアウト", encoding="utf-8")
    main(["search", "タイムアウト", "--in", str(tmp_path), "--once"])  # reads other.md, outside the kept folder
    capsys.readouterr()
    assert background.expire(index, now=time.time() + background.ONCE_KEEP + 1) == [str(tmp_path.resolve())]
    assert [r["path"] for r in index.roots()] == [str(root.resolve())]  # the folder inside it stays
    assert len(index.files_in([root])) == 2 and index.files_in([tmp_path]) == index.files_in([root])
    main(["search", "タイムアウト", "--in", str(tmp_path), "--once"])
    capsys.readouterr()
    main(["search", "タイムアウト", "--in", str(root)])  # searched for good inside the once folder
    capsys.readouterr()
    assert index.once_roots() == []  # the outer folder is kept too, or dropping it would drop this one
    assert background.expire(index, now=time.time() + background.ONCE_KEEP + 1) == []
