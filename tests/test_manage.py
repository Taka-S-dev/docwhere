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


def test_read_times_can_be_limited_to_one_folder(tmp_path, folder):
    other = tmp_path / "other"
    other.mkdir()
    (other / "x.txt").write_text("x", encoding="utf-8")
    index = Index(tmp_path / "index.db")
    index.sync(folder)
    index.sync(other)
    assert set(index.read_times(folder)) == {".md", ".docx"}
    assert set(index.read_times(other)) == {".txt"}
    assert set(index.read_times()) == {".md", ".docx", ".txt"}


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


def test_a_folder_searched_once_shows_when_it_leaves_and_can_be_kept(api, folder, tmp_path):
    index = Index(tmp_path / "db" / "index.db")
    index.mark_once(folder, 2_000_000_000.0)
    index.close()
    [root] = api.overview()["roots"]
    assert root["once_until"] == 2_000_000_000.0
    assert api.keep(str(folder)) == {"kept": [str(folder)]}
    assert api.overview()["roots"][0]["once_until"] is None
    api.update([str(folder)])  # updating by hand keeps it too
    index = Index(tmp_path / "db" / "index.db")
    index.mark_once(folder, 1.0)  # past due: dropped when the window looks again
    index.close()
    api.queued.clear()
    assert api.overview()["roots"] == []


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


def test_times_tells_where_reading_went_for_a_folder_and_the_whole_index(api, folder, tmp_path):
    index = Index(tmp_path / "db" / "index.db")
    with index.db:
        index.db.execute("UPDATE files SET read_ms = 100")
        index.db.execute("UPDATE files SET read_ms = 180000, error = 'timed out after 180s and was stopped'"
                         " WHERE path LIKE '%broken.docx'")
    index.close()
    out = api.times(str(folder))
    assert out["total"] == 180.4
    assert [(k["ext"], k["files"]) for k in out["kinds"]] == [(".docx", 1), (".md", 4)]  # the most time first
    assert out["slowest"][0] == {"path": str(folder / "broken.docx"), "seconds": 180.0,
                                 "error": "timed out after 180s and was stopped"}
    assert len(out["slowest"]) == 5
    whole = api.times(None)
    assert whole["total"] == 180.4 and whole["slowest"] == []
    with pytest.raises(LookupError):
        api.times(str(tmp_path))


def test_manage_update_hands_folders_over_before_opening_the_window(tmp_path, folder, monkeypatch, capsys):
    from docwhere.cli import main

    db = tmp_path / "db" / "index.db"
    index = Index(db)
    index.sync(folder)
    index.close()
    queued, opened = [], []
    monkeypatch.setattr(background, "request", lambda index, root: queued.append(Path(root)) or True)
    monkeypatch.setattr(manage, "open_manager", lambda path, tab="folders": opened.append(list(queued)) or True)
    new = tmp_path / "new"
    new.mkdir()
    assert main(["--db", str(db), "manage", "--update", str(new)]) == 0
    assert queued == [new] and opened == [[new]]  # handed over first, then the window
    assert main(["--db", str(db), "manage", "--update"]) == 0  # no folder: every indexed one
    assert queued[-1] == folder
    assert main(["--db", str(db), "manage", "--update", str(tmp_path / "missing")]) == 2
    monkeypatch.setattr(manage, "open_manager", lambda path, tab="folders": False)
    assert main(["--db", str(db), "manage", "--update", str(folder)]) == 0  # no window, but the update runs
    assert main(["--db", str(db), "manage"]) == 1
    assert "docwhere status" in capsys.readouterr().err


def test_a_folder_inside_another_is_checked_with_it_and_forgotten_without_its_files(api, folder, tmp_path):
    index = Index(tmp_path / "db" / "index.db")
    index.sync(folder / "old")
    index.close()
    _redate(folder / "old" / "d.md", "# d\nchanged\n")
    out = api.changes(str(folder))
    assert out["changed"] == 1 and out["inside"] == {str(folder / "old"): {"exists": True, "files": 1, "new": 0, "changed": 1, "gone": 0}}
    # forgetting the inner folder leaves its files in the outer one
    assert api.forget(str(folder / "old"))["kept_by"] == str(folder)
    [root] = api.overview()["roots"]
    assert root["files"] == 5


def test_forgetting_a_folder_forgets_the_folders_inside_it(api, folder, tmp_path):
    index = Index(tmp_path / "db" / "index.db")
    index.sync(folder / "old")
    index.close()
    assert api.forget(str(folder)) == {"forgotten": str(folder), "inside": [str(folder / "old")], "kept_by": None}
    assert api.overview()["roots"] == []


def test_the_search_tab_searches_like_the_agent_and_opens_only_its_own_hits(api, folder, tmp_path, monkeypatch):
    from docwhere import notes, present

    out = api.search({"text": "timeout -old"})
    assert {Path(h["path"]).name for h in out["hits"]} == {"a.md", "b.md", "c.md"}  # old/d.md left out
    assert out["exclude"] == ["old"] and out["roots"] == [str(folder)]
    assert [Path(h["path"]).name for h in api.search({"text": "d.md", "name": True})["hits"]] == ["d.md"]
    with pytest.raises(ValueError, match="正規表現"):
        api.search({"text": "time(", "regex": True})
    with pytest.raises(LookupError):
        api.search({"text": "timeout", "scope": str(tmp_path)})  # not a folder of the index
    # a kind of the notes map: its folders, and how many of them were never read
    notes.save(tmp_path / "db" / "index.db", [{"cells": {"path": str(folder / "old"), "kind": "仕様書"}},
                                              {"cells": {"path": str(tmp_path), "kind": "仕様書"}}], None)
    assert api.search_scopes()["kinds"] == [{"kind": "仕様書", "folders": 2}]
    out = api.search({"text": "timeout", "scope": "kind:仕様書"})
    assert [Path(h["path"]).name for h in out["hits"]] == ["d.md"] and out["missing"] == 1
    shown = []
    monkeypatch.setattr(present, "show_page", lambda db, build, title, options, open_it: shown.append(title) or "page")
    assert api.open_hits([1], "タイムアウト") == "page" and shown == ["タイムアウト"]
    with pytest.raises(LookupError):
        api.open_hits([5], "")  # only the hits this window found


def test_search_hits_are_counted_by_format_and_folder_and_can_be_narrowed(api, folder):
    (folder / "e.txt").write_text("timeout\n", encoding="utf-8")
    index = Index(api._db)
    index.sync(folder)
    index.close()
    out = api.search({"text": "timeout"})
    assert {x["value"]: x["count"] for x in out["facets"]["ext"]} == {".md": 4, ".txt": 1}
    assert {x["value"] for x in out["facets"]["folder"]} == {"docs", "old"}
    narrowed = api.search({"text": "timeout", "facet": {"ext": ".txt"}})
    assert [Path(h["path"]).name for h in narrowed["hits"]] == ["e.txt"] and narrowed["hits"][0]["n"] == 1
    assert api.preview(1)["sections"][0]["current"]  # the pane shows the hit numbered as listed


def test_search_finds_other_wordings_and_says_why_nothing_was_found(api, folder, monkeypatch):
    (folder / "s.md").write_text("# サーバー\n接続先のサーバー\n", encoding="utf-8")
    index = Index(api._db)
    index.sync(folder)
    index.close()
    out = api.search({"text": "サーバ", "variants": True})
    assert out["variants"] == ["サーバー"] and out["hits"]
    out = api.search({"text": "timeout -timeout"})
    assert not out["hits"] and any("除外" in hint for hint in out["hints"])
    assert any("1 文字" in hint for hint in api.search({"text": "m"})["hints"])
    assert api.search({"text": "md"})["hints"] == []  # quick enough: nothing to say
    monkeypatch.setattr(manage, "SLOW_SHORT", -1)
    assert any("3 文字以上" in hint for hint in api.search({"text": "md"})["hints"])


def test_the_preview_draws_a_pdf_page_and_waits_to_export_a_word_file(api, folder, tmp_path, monkeypatch):
    import pymupdf

    doc = pymupdf.open()
    for text in ("first page", "the timeout page"):
        doc.new_page().insert_text((72, 72), text)
    doc.save(folder / "guide.pdf")
    doc.close()
    (folder / "spec.docx").write_bytes(b"not a zip")  # unreadable, so found by name only
    index = Index(tmp_path / "db" / "index.db")
    index.sync(folder)
    index.close()
    exported = []
    monkeypatch.setattr("docwhere.rendition._export_apart", lambda src, out, ext: exported.append(src))
    out = api.search({"text": "timeout", "facet": {"ext": ".pdf"}})
    [hit] = out["hits"]
    assert api.preview(hit["n"])["pages"] == "pdf"
    page = api.page_image(hit["n"])
    assert page["src"].startswith("data:image/jpeg;base64,") and (page["page"], page["pages"]) == (2, 2)

    api.search({"text": "spec", "name": True})
    assert api.page_image(1) == {"convert": True, "app": "Word"} and exported == []  # Office waits for a click
    api.page_image(1, True)
    assert exported == [folder / "spec.docx"]
    (folder / "spec.docx").unlink()
    assert "見つかりません" in api.page_image(1)["note"]


def test_a_large_page_is_drawn_small_and_a_page_asked_for_meanwhile_wins(api, folder, tmp_path):
    import base64
    import threading
    import time

    import pymupdf

    doc = pymupdf.open()
    doc.new_page(width=2384, height=3370).insert_text((72, 72), "the timeout drawing")  # an A0 sheet
    doc.save(folder / "sheet.pdf")
    doc.close()
    index = Index(tmp_path / "db" / "index.db")
    index.sync(folder)
    index.close()
    [hit] = api.search({"text": "drawing"})["hits"]
    page = api.page_image(hit["n"])
    picture = pymupdf.Pixmap(base64.b64decode(page["src"].split(",", 1)[1]))
    assert max(picture.width, picture.height) <= manage.PREVIEW_MAX_SIDE

    answers = {}
    with manage._PAGES:  # a page being drawn: both requests below wait for it
        first = threading.Thread(target=lambda: answers.update(first=api.page_image(hit["n"])))
        first.start()
        time.sleep(0.3)
        second = threading.Thread(target=lambda: answers.update(second=api.page_image(hit["n"])))
        second.start()
        time.sleep(0.3)
    first.join(10)
    second.join(10)
    assert answers["first"] == {"stale": True} and "src" in answers["second"]


def test_the_panes_choice_of_text_or_page_outlives_the_window(api, tmp_path):
    assert api.preview_mode() == "text"
    assert api.preview_mode("page") == "page"
    again = manage.ManageApi(tmp_path / "db" / "index.db")  # the next window
    again._trusted = True
    assert again.preview_mode() == "page"
    assert again.preview_mode("anything") == "page"  # not a choice: nothing changes
