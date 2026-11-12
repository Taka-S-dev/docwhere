"""The index window's Python side: what it reports, what it may change, and the page it shows."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from docwhere import background, manage
from docwhere.index import Index


def _redate(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))


@pytest.fixture
def folder(tmp_path) -> Path:
    root = tmp_path / "docs"
    (root / "old").mkdir(parents=True)
    for name in ("a.md", "b.md", "c.md", "old/d.md"):
        (root / name).write_text(f"# {name}\ntimeout\n", encoding="utf-8")
    (root / "broken.docx").write_bytes(b"not a zip")
    return root


@pytest.fixture
def api(tmp_path, folder, monkeypatch):
    index = Index(tmp_path / "db" / "index.db")
    index.sync(folder)
    index.close()
    queued = []
    monkeypatch.setattr(background, "request", lambda index, root: queued.append(Path(root)) or True)
    a = manage.ManageApi(tmp_path / "db" / "index.db")
    a._trusted = True
    a.queued = queued
    return a


def test_changes_counts_new_changed_and_gone_files_without_touching_the_index(tmp_path, folder):
    index = Index(tmp_path / "index.db")
    index.sync(folder)
    seen = index.db.execute("SELECT synced_at FROM roots").fetchone()[0]
    _redate(folder / "a.md", "# a\nchanged\n")
    (folder / "new.md").write_text("# new\n", encoding="utf-8")
    (folder / "b.md").unlink()
    assert index.changes(folder) == {"exists": True, "files": 5, "new": 1, "changed": 1, "gone": 1}
    # read only: the visit is not recorded and the gone file is still in the index
    assert index.db.execute("SELECT synced_at FROM roots").fetchone()[0] == seen
    assert index.db.execute("SELECT count(*) FROM files").fetchone()[0] == 5
    assert index.changes(folder / "missing") == {"exists": False}


def test_the_reader_records_the_files_it_is_reading(tmp_path):
    from docwhere.index import SyncProgress

    index = Index(tmp_path / "index.db")
    with index.db:
        index.db.execute("INSERT INTO reader(id, pid, heartbeat, started) VALUES (1, 1, strftime('%s','now'), 0)")
    background._Progress(index, lambda p: None)(SyncProgress(3, 10, [(Path("big.doc"), 12.34)]))
    state = background.state(index)
    assert (state.done, state.total, state.reading) == (3, 10, [(str(Path("big.doc")), 12.3)])


def test_every_call_is_refused_unless_the_signed_page_is_showing(api):
    api._trusted = False
    with pytest.raises(PermissionError):
        api.overview()


def test_overview_lists_folders_with_their_state(api, folder):
    out = api.overview()
    [root] = out["roots"]
    assert (root["path"], root["name"], root["files"], root["errors"], root["exists"]) == (str(folder), "docs", 5, 1, True)
    assert root["parent"] is None and out["reader"] is None and out["pending"] == []
    assert api.changes(str(folder))["changed"] == 0


def test_a_nested_folder_names_its_parent(api, folder, tmp_path):
    index = Index(tmp_path / "db" / "index.db")
    index.sync(folder / "old")
    index.close()
    nested = {r["path"]: r["parent"] for r in api.overview()["roots"]}
    assert nested == {str(folder): None, str(folder / "old"): str(folder)}


def test_actions_only_reach_folders_in_the_index(api, folder, tmp_path):
    for call in (lambda: api.update([str(tmp_path)]), lambda: api.forget(str(tmp_path)),
                 lambda: api.add_exclude(str(tmp_path), "x"), lambda: api.reveal(str(tmp_path / "x.md"))):
        with pytest.raises(LookupError):
            call()
    assert api.queued == []


def test_update_rebuild_and_forget(api, folder, tmp_path):
    assert api.update([str(folder)]) == {"queued": [str(folder)]}
    assert api.rebuild(str(folder)) == {"queued": str(folder)}
    assert api.queued == [folder, folder]
    index = Index(tmp_path / "db" / "index.db")
    assert index.db.execute("SELECT count(*) FROM files").fetchone()[0] == 0  # rebuilt: read again from nothing
    index.sync(folder)
    index.close()
    api.forget(str(folder))
    assert api.overview()["roots"] == []


def test_exclusions_can_be_added_and_undone(api, folder):
    assert api.add_exclude(str(folder), "old") == {"added": ["old"], "dropped": 1}
    assert [x["pattern"] for x in api.overview()["roots"][0]["excludes"]] == ["old"]
    assert api.remove_exclude(str(folder), "old") == {"removed": ["old"]}
    with pytest.raises(ValueError):
        api.add_exclude(str(folder), "a/../b")


def test_unreadable_files_are_listed_with_their_reason(api, folder):
    out = api.unreadable(str(folder))
    assert out["count"] == 1 and out["files"][0]["path"] == str(folder / "broken.docx")
    assert out["files"][0]["error"]


def test_the_page_is_signed_and_runs_no_script_of_its_own(tmp_path):
    from docwhere import window

    db = tmp_path / "index.db"
    page = manage.write_page(db)
    text = page.read_text(encoding="utf-8")
    assert window.page_signed(db, page)
    assert "<script" not in text.lower()
    assert re.search(r"Content-Security-Policy\" content=\"default-src 'none'", text)
    page.write_text(text + "<!-- changed -->", encoding="utf-8")
    assert not window.page_signed(db, page)

