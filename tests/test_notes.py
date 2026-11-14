"""The notes map as the index window reads and writes it: what the user wrote by hand survives."""

from pathlib import Path

import pytest

from docwhere import manage, notes
from docwhere.index import Index

HAND_WRITTEN = (
    "path,section,kind,note,owner\r\n"
    "# 設計書には画面遷移も含む\r\n"
    'D:\\docs\\仕様書,,仕様書,"通信, プロトコル",山田\r\n'
    "wiki:社内/OPS,,運用,\"二行の\r\nメモ\"\r\n"
)


def test_comments_extra_columns_and_quoted_fields_survive_a_save(tmp_path):
    db = tmp_path / "index.db"
    notes.notes_path(db).write_bytes(("\ufeff" + HAND_WRITTEN).encode("utf-8"))
    loaded = notes.load(db)
    assert loaded["header"] == ["path", "section", "kind", "note", "owner"]
    assert [list(it) for it in loaded["items"]] == [["comment"], ["cells"], ["cells"]]
    assert loaded["items"][1]["cells"]["note"] == "通信, プロトコル"
    assert loaded["items"][2]["cells"]["note"] == "二行の\r\nメモ"
    notes.save(db, loaded["items"], loaded["version"])
    assert notes.notes_path(db).read_bytes() == ("\ufeff" + HAND_WRITTEN).encode("utf-8")  # unchanged byte for byte


def test_a_save_does_not_overwrite_an_edit_made_meanwhile(tmp_path):
    db = tmp_path / "index.db"
    version = notes.save(db, [{"cells": {"path": "D:\\a", "kind": "仕様書"}}], None)
    assert notes.load(db)["items"][0]["cells"]["path"] == "D:\\a"
    path = notes.notes_path(db)
    path.write_text(path.read_text(encoding="utf-8-sig") + "D:\\b,,設計書,\r\n", encoding="utf-8-sig")  # Excel
    with pytest.raises(notes.Changed):
        notes.save(db, [], version)
    with pytest.raises(notes.Changed):
        notes.save(db, [], None)  # a window that saw no file must not replace one made since


def test_the_window_marks_each_row_and_reaches_only_paths_in_the_map(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    (docs / "spec").mkdir(parents=True)
    (docs / "spec" / "a.md").write_text("# a\n", encoding="utf-8")
    db = tmp_path / "db" / "index.db"
    Index(db).sync(docs)
    other = tmp_path / "other"
    other.mkdir()
    api = manage.ManageApi(db)
    api._trusted = True
    out = api.save_notes([
        {"comment": "# メモ"},
        {"cells": {"path": str(docs / "spec"), "kind": "仕様書"}},
        {"cells": {"path": str(other), "kind": "設計書"}},
        {"cells": {"path": str(tmp_path / "gone"), "kind": "設計書"}},
        {"cells": {"path": "wiki:社内", "kind": "運用"}},
    ], None)
    assert [it.get("status") for it in out["items"]] == [
        None, {"kind": "folder", "indexed": True}, {"kind": "folder", "indexed": False}, {"kind": "missing"},
        {"kind": "wiki", "known": False},
    ]
    queued = []
    monkeypatch.setattr(manage.background, "request", lambda index, root: queued.append(Path(root)) or True)
    api.index_note(str(other))
    assert queued == [other.resolve()]
    with pytest.raises(LookupError):
        api.index_note(str(tmp_path))  # not written in the map
    with pytest.raises(RuntimeError, match="ほかで変更"):
        api.save_notes([], None)
