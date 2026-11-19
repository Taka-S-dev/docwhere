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


def test_an_indexed_file_is_shown_selected_in_its_folder(api, folder, monkeypatch):
    from docwhere import window

    started = []
    monkeypatch.setattr(window.subprocess, "Popen", lambda command: started.append(command))
    assert api.reveal(str(folder / "old" / "d.md"))
    # one command line, the path quoted after /select, (a list would quote "/select,..." whole)
    assert started == [f'"{window.Path(os.environ["SystemRoot"]) / "explorer.exe"}" /select,"{(folder / "old" / "d.md").resolve()}"']


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


def test_the_window_has_the_reading_windows_settings(api, folder, tmp_path, monkeypatch):
    from docwhere import opener, window

    monkeypatch.setattr(window, "_set_window_title", lambda text: None)
    exe = Path(os.environ["WINDIR"]) / "notepad.exe"
    api.save_settings({"pdf_program": str(exe), "pdf_args": "-p {file}", "title": "資料", "viewer": "browser"})
    saved = api.settings()
    assert (saved["title"], saved["viewer"], saved["pdf_args"]) == ("資料", "browser", "-p {file}")
    assert saved == window.settings_values(tmp_path / "db" / "index.db")  # the reading window's own file
    # checked as the reading window checks them: a shell is refused and nothing is changed
    with pytest.raises(PermissionError):
        api.save_settings({"pdf_program": str(Path(os.environ["WINDIR"]) / "System32" / "cmd.exe"), "title": "x"})
    assert api.settings()["title"] == "資料"
    # tried on the search tab's chosen hit, at its page, and only on a PDF
    api.search({"text": "timeout"})
    with pytest.raises(ValueError, match="PDF"):
        api.try_pdf(str(exe), "{file}", 1)
    opened = []
    monkeypatch.setattr(opener, "open_at", lambda path, anchor, pdf_app=None: opened.append((anchor, pdf_app)) or "opened")
    api._results = (["x"], [{"path": str(folder / "spec.pdf"), "anchor": {"page": 3}}])
    assert api.try_pdf(str(exe), "{file}", 1) == "spec.pdf: opened"
    assert opened[0][0] == {"page": 3} and opened[0][1]["command"][0].lower().endswith("notepad.exe")


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


def test_hits_collected_across_searches_open_together_as_the_index_has_them(api, folder, tmp_path, monkeypatch):
    import json

    from docwhere import present, viewer

    api.search({"text": "timeout -old"})
    first = api._results[1][0]
    assert api.collect(1)["count"] == 1
    assert api.collect(1)["count"] == 1  # the same section once
    api.search({"text": "d.md"})
    assert api.collect(1)["count"] == 2
    kept = api.collected()
    assert [i["query"] for i in kept["items"]] == ["timeout -old", "d.md"]
    again = manage.ManageApi(tmp_path / "db" / "index.db")  # the next window
    again._trusted = True
    assert again.collected() == kept

    built = []
    monkeypatch.setattr(viewer, "build_html", lambda index, terms, hits, *a, **k: built.append(hits) or "<html>")
    monkeypatch.setattr(present, "show_page", lambda db, build, title, options, open_it: build(True) and title)
    # the file comes from the index, whatever the saved list says; a section gone is left out
    saved = tmp_path / "db" / manage.COLLECTED_NAME
    items = json.loads(saved.read_text(encoding="utf-8"))["items"]
    items[0]["path"] = r"C:\Windows\System32\drivers\etc\hosts"
    items.append({**items[1], "chunk_id": 999999})
    saved.write_text(json.dumps({"version": 1, "items": items}), encoding="utf-8")
    assert api.open_collected("保存の調査") == "保存の調査"
    assert [h["path"] for h in built[0]] == [first["path"], str(folder / "old" / "d.md")]
    assert [h["query"] for h in built[0]] == ["timeout -old", "d.md"]

    assert api.uncollect(items[0]["chunk_id"])["count"] == 2  # the stale entry stays until cleared
    assert api.clear_collected()["count"] == 0
    with pytest.raises(LookupError):
        api.open_collected("")


def test_every_match_is_counted_and_the_list_reads_on_past_a_few_of_each_file(api, folder, tmp_path):
    (folder / "long.md").write_text("".join(f"# part {i}\ntimeout here\n\n" for i in range(5)), encoding="utf-8")
    index = Index(tmp_path / "db" / "index.db")
    index.sync(folder)
    index.close()
    out = api.search({"text": "timeout"})
    names = [Path(h["path"]).name for h in out["hits"]]
    assert out["total"] == 9 and out["files"] == 5  # a.md, b.md, c.md, old/d.md and long.md's five
    assert out["first"] == 7 and names[:7].count("long.md") == manage.FIRST_PER_FILE  # a few of each first
    assert names[7:] == ["long.md", "long.md"]  # then the rest, in rank order
    assert [h["n"] for h in out["hits"]] == list(range(1, 10))  # numbered as listed, so each can be opened
    narrowed = api.search({"text": "timeout", "facet": {"folder": "old"}})
    assert (narrowed["total"], narrowed["files"]) == (1, 1)  # counted from what the narrowing keeps


def test_overview_says_when_the_index_drive_is_too_full_to_go_on_reading(api, monkeypatch):
    import docwhere.index as index_module

    assert api.overview()["disk_low"] is None or api.overview()["disk_low"] < index_module.MIN_FREE_BYTES
    monkeypatch.setattr(index_module, "free_bytes", lambda path: 10 * 1024**3)
    assert api.overview()["disk_low"] is None
    monkeypatch.setattr(index_module, "free_bytes", lambda path: 1024**3)
    out = api.overview()
    assert out["disk_low"] == 1024**3 and out["disk_min"] == index_module.MIN_FREE_BYTES and out["disk"]


def test_a_search_that_finishes_late_does_not_replace_the_hits_being_shown(api, folder):
    newest = api.search({"text": "timeout", "seq": 2})
    assert newest["hits"]
    api.search({"text": "d.md", "name": True, "seq": 1})  # typed earlier, answered later
    # the buttons still act on the hits of the search the tab shows
    assert api.preview(1)["path"] == newest["hits"][0]["path"]
    api.search({"text": "d.md", "name": True, "seq": 3})
    assert Path(api._results[1][0]["path"]).name == "d.md"


def test_enter_in_the_settings_saves_and_enter_on_stale_hits_searches_again():
    from docwhere.manage import BODY as MANAGE_BODY, MANAGE_JS

    # the only submit button of the settings form is 保存: with any other first, Enter in a text field cancels
    assert '<button type="submit" class="btn primary" id="settings-save">' in MANAGE_BODY
    assert '<button type="button" class="btn" id="settings-cancel">' in MANAGE_BODY
    assert "queryKey() !== shownFor" in MANAGE_JS


def test_other_wordings_are_searched_with_every_term_required_too(api, folder, tmp_path):
    (folder / "srv.md").write_text("# 接続\nサーバのアドレスと timeout を決める。\n", encoding="utf-8")
    index = Index(tmp_path / "db" / "index.db")
    index.sync(folder)
    index.close()
    out = api.search({"text": "サーバー timeout", "all": True, "variants": True})
    assert [Path(h["path"]).name for h in out["hits"]] == ["srv.md"]  # the document writes サーバ
    assert api.search({"text": "サーバー timeout", "all": True, "variants": False})["hits"] == []


def test_the_window_asks_before_closing_over_unsaved_map_edits(api):
    asked = []
    assert manage.may_close(api, lambda: asked.append(1) or False)  # nothing unsaved: not asked
    api.map_unsaved(True)
    assert not manage.may_close(api, lambda: asked.append(1) or False)  # "no": the window stays
    assert manage.may_close(api, lambda: True)  # "yes": the edits are given up
    api.map_unsaved(False)
    assert manage.may_close(api, lambda: False) and asked == [1]


def test_the_settings_dialog_turns_the_picture_count_off_and_leaves_it_alone_when_not_asked(tmp_path):
    from docwhere.settings import count_pictures
    from docwhere.window import save_settings_values, settings_values

    db = tmp_path / "index.db"
    assert settings_values(db)["count_pictures"] is True  # on until turned off
    save_settings_values(db, {"count_pictures": False})
    assert count_pictures(db) is False and settings_values(db)["count_pictures"] is False
    save_settings_values(db, {"title": "Reference"})  # a dialog of an older page: no such box
    assert count_pictures(db) is False


def _deep(folder, api):
    """docs/projects/alpha/design/net.md and its neighbours, each naming "timeout"."""
    for rel in ("projects/alpha/design/net.md", "projects/alpha/design/ui.md", "projects/alpha/plan.md",
                "projects/beta/notes.md"):
        path = folder / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {path.stem}\ntimeout of {path.stem}\nretry of {path.stem}\n", encoding="utf-8")
    index = Index(api._db)
    index.sync(folder)
    index.close()


def test_the_search_goes_down_into_folders_at_any_depth_and_back_up(api, folder, tmp_path):
    _deep(folder, api)
    out = api.search({"text": "timeout"})
    assert out["within"] is None
    # every folder right below is named, the one with most hits first
    assert [(x["name"], x["count"]) for x in out["into"]] == [("projects", 4), ("old", 1)]
    assert out["into"][0]["path"] == str(folder / "projects")

    down = api.search({"text": "timeout", "within": str(folder / "projects" / "alpha")})
    assert sorted(Path(h["path"]).name for h in down["hits"]) == ["net.md", "plan.md", "ui.md"]
    assert down["total"] == 3  # counted as a search of that folder, not as a part of the listed hits
    assert [t["name"] for t in down["within"]["trail"]] == ["docs", "projects", "alpha"]
    assert down["within"]["trail"][1]["path"] == str(folder / "projects") and not down["within"]["file"]
    assert [(x["name"], x["count"]) for x in down["into"]] == [("design", 2)]

    # a folder with no hit is still offered, so a word found elsewhere does not hide the way down
    none = api.search({"text": "plan", "within": str(folder / "projects" / "alpha")})
    assert [(x["name"], x["count"]) for x in none["into"]] == [("design", 0)]
    assert api.preview(1)["sections"][0]["current"]  # the hits open as numbered

    with pytest.raises(LookupError, match="範囲の中にありません"):
        api.search({"text": "timeout", "within": str(tmp_path)})  # outside the index
    with pytest.raises(LookupError, match="範囲の中にありません"):
        api.search({"text": "timeout", "scope": str(folder), "within": str(folder.parent / "docs2" / "x")})


def test_the_search_can_be_kept_to_one_file(api, folder):
    _deep(folder, api)
    one = folder / "projects" / "alpha" / "design" / "net.md"
    out = api.search({"text": "timeout retry", "within": str(one)})
    assert {Path(h["path"]).name for h in out["hits"]} == {"net.md"} and out["files"] == 1
    assert out["within"]["file"] and [t["name"] for t in out["within"]["trail"]][-2:] == ["design", "net.md"]
    assert out["into"] == []  # nothing below a file
    # another word, the same file: the place stays until it is given up
    assert api.search({"text": "ui", "within": str(one)})["hits"] == []
    assert [Path(h["path"]).name for h in api.search({"text": "ui"})["hits"]] == ["ui.md"]


def test_the_page_holds_the_place_in_a_box_and_lists_the_folders_under_it():
    from docwhere.manage import MANAGE_JS

    assert "within: withinPath" in MANAGE_JS  # sent with every search
    assert "このファイルの中だけを探す" in MANAGE_JS and "場所の指定をやめる" in MANAGE_JS
    assert 'withinPath = ""; place = null; closePlaces(); showPlace(); runSearch(false);' in MANAGE_JS  # another scope starts at its top
    assert 'id="place"' in manage.BODY and 'id="place-root"' in manage.BODY
    assert "api.search_places({ text, scope: scopeSel.value })" in MANAGE_JS
    # entering the box lists every folder, whatever place it holds; an emptied box is the whole scope
    assert 'const text = placeTyped ? placeIn.value.trim() : "";' in MANAGE_JS
    assert "if (!placeIn.value.trim()) pickPlace(null);" in MANAGE_JS
    assert 'placeIcon(x.file ? fileKind(x.path) : "folder")' in MANAGE_JS  # a row shows what it is before it is read
    # each part the results are drawn with is there: one left out stops every search with an error
    for name in ("renderResults", "renderFacets", "renderPlace", "renderPreview", "showPlace", "askPlaces", "goTo"):
        assert f"const {name} = " in MANAGE_JS, name


def test_the_place_box_lists_every_folder_by_path_and_narrows_as_typed(api, folder, tmp_path):
    _deep(folder, api)

    def listed(text="", **more):
        return [(p["shown"], p["file"]) for p in api.search_places({"text": text, **more})["places"]]

    # nothing typed: every folder at every depth, one list ordered by path, written from below the
    # folder searched (whose name stands before the box)
    assert listed() == [("old", False), ("projects", False), ("projects\\alpha", False),
                        ("projects\\alpha\\design", False), ("projects\\beta", False)]
    # typed: the folders whose path holds every part, in any order and any letter case...
    assert listed("design alpha") == [("projects\\alpha\\design", False)]
    # ...then the files whose own name holds a part typed; a file is not listed for words that
    # are all its folders'
    assert listed("net") == [("projects\\alpha\\design\\net.md", True)]
    assert listed("alpha plan") == [("projects\\alpha\\plan.md", True)]
    assert listed("ALPHA") == [("projects\\alpha", False), ("projects\\alpha\\design", False)]
    assert listed("nowhere") == []
    # a file named as its folder is: the folder, then the file
    (folder / "projects" / "beta" / "beta-plan.md").write_text("# beta plan\ntimeout\n", encoding="utf-8")
    index = Index(api._db)
    index.sync(folder)
    index.close()
    assert listed("beta") == [("projects\\beta", False), ("projects\\beta\\beta-plan.md", True)]
    assert listed("alpha plan") == [("projects\\alpha\\plan.md", True)]  # not the other folder's plan

    # what is listed is what the search then takes, and the search says how the box writes it
    [place] = api.search_places({"text": "design alpha"})["places"]
    assert place["path"] == str(folder / "projects" / "alpha" / "design")
    out = api.search({"text": "timeout", "within": place["path"]})
    assert sorted(Path(h["path"]).name for h in out["hits"]) == ["net.md", "ui.md"]
    assert out["within"]["shown"] == "projects\\alpha\\design"

    # several folders searched at once: each is a place itself, and leads the paths below it
    other = tmp_path / "manuals"
    (other / "v2").mkdir(parents=True)
    (other / "v2" / "m.md").write_text("# m\ntimeout\n", encoding="utf-8")
    index = Index(api._db)
    index.sync(other)
    index.close()
    assert listed()[:2] == [("docs", False), ("docs\\old", False)] and listed()[-2:] == [("manuals", False), ("manuals\\v2", False)]
    assert listed(scope=str(other)) == [("v2", False)]
    assert api.search({"text": "timeout", "within": str(other / "v2")})["within"]["shown"] == "manuals\\v2"
    with pytest.raises(LookupError):
        api.search_places({"text": "x", "scope": str(tmp_path)})  # not a folder of the index
