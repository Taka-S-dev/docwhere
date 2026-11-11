from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from docwhere.cli import main
from docwhere.excludes import Matcher, normalize
from docwhere.index import Index, path_key


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "share"
    for rel in ("spec.md", "spec_bak.md", "Old/spec.md", "design/archive/plan.md", "design/plan.md", "design/sub/old/x.md"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {rel}\nbody\n", encoding="utf-8")
    return root


def _indexed(index: Index, root: Path) -> set[str]:
    return {Path(r["path"]).relative_to(root).as_posix() for r in index.files_in([root])}


def test_patterns_are_normalised_and_bad_ones_refused():
    assert normalize(" ./design\\archive/ ") == "design/archive"
    for bad in ("", "/", "a/../b", "."):
        with pytest.raises(ValueError):
            normalize(bad)


def test_a_name_matches_at_any_depth_and_a_path_from_the_folder(tmp_path):
    root = path_key(tmp_path)
    m = Matcher([(root, "old"), (root, "design/archive")])
    assert m.match(path_key(tmp_path / "OLD" / "a.md"))  # case is ignored
    assert m.match(path_key(tmp_path / "x" / "y" / "old"))
    assert m.match(path_key(tmp_path / "design" / "archive" / "plan.md"))
    assert not m.match(path_key(tmp_path / "x" / "design" / "archive"))  # a path is anchored to the folder
    assert not m.match(path_key(tmp_path / "older.md"))
    assert not m.match(root)  # a folder's own patterns never leave the folder itself out


def test_sync_leaves_patterns_out_and_counts_what_they_matched(tree, index):
    index.add_excludes(tree, ["old", "*_bak*", "design/archive"])
    stats = index.sync(tree)
    assert _indexed(index, tree) == {"spec.md", "design/plan.md"}
    assert stats.files == 2
    [row] = index.roots()
    assert {e["pattern"]: e["matched"] for e in row["excludes"]} == {"old": 2, "*_bak*": 1, "design/archive": 1}


def test_adding_drops_indexed_files_at_once_and_removing_reads_them_again(tree, index):
    index.sync(tree)
    added, dropped = index.add_excludes(tree, ["old", "OLD"])
    assert added == ["old"] and dropped == 2
    assert "Old/spec.md" not in _indexed(index, tree)
    assert index.roots()[0]["excludes"] == [{"pattern": "old", "matched": None}]  # not counted until a sync

    removed, unknown = index.remove_excludes(tree, ["Old", "nope"])
    assert removed == ["old"] and unknown == ["nope"]
    assert index.sync(tree).updated == 2
    assert "Old/spec.md" in _indexed(index, tree)


def test_patterns_hold_when_a_parent_or_a_subfolder_is_searched(tree, index):
    index.add_excludes(tree, ["old"])
    index.sync(tree / "design")  # a parent's pattern applies in the subfolder
    assert _indexed(index, tree / "design") == {"archive/plan.md", "plan.md"}

    index.add_excludes(tree / "design", ["archive"])
    index.sync(tree)  # and a subfolder's pattern when the parent is searched
    assert "design/archive/plan.md" not in _indexed(index, tree)

    stats = index.sync(tree / "Old")  # a folder a parent's pattern leaves out is not read at all
    assert stats.excluded_by == (str(tree), "old") and stats.files == 0


def test_rebuild_keeps_the_patterns_and_forget_drops_them(tree, index, capsys, monkeypatch):
    db = index.path
    index.close()
    monkeypatch.setenv("DOCWHERE_DB", str(db))
    assert main(["exclude", str(tree), "old"]) == 0
    assert main(["index", str(tree), "--rebuild"]) == 0
    assert main(["exclude", str(tree)]) == 0
    assert f"{tree}: excluding old (2 left out)" in capsys.readouterr().out

    assert main(["forget", str(tree)]) == 0
    assert "no longer excluding old" in capsys.readouterr().out
    assert main(["exclude", str(tree)]) == 0
    assert "nothing excluded here" in capsys.readouterr().out


def test_status_and_search_json_report_exclusions(tree, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "db" / "index.db"))
    main(["exclude", str(tree), "old"])
    capsys.readouterr()
    main(["search", "body", "--in", str(tree / "Old"), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["hits"] == []
    assert out["excluded_roots"] == [{"root": str(tree / "Old"), "set_on": str(tree), "pattern": "old"}]
    assert out["excludes"] == [{"pattern": "old", "set_on": str(tree), "left_out": 1}]

    main(["status", "--json"])
    [row] = [r for r in json.loads(capsys.readouterr().out)["roots"] if r["path"] == str(tree)]
    assert row["excludes"] == [{"pattern": "old", "matched": None}]  # its own folder was never synced
    assert main(["exclude", str(tree), "--remove"]) == 2
    assert main(["exclude", str(tree), "--clear"]) == 0


def test_every_search_says_what_the_patterns_left_out(tree, capsys, monkeypatch, tmp_path):
    # set on the parent, searched in a subfolder: the user may not remember the pattern
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "db" / "index.db"))
    main(["exclude", str(tree), "old", "archive", "nothing*"])
    capsys.readouterr()
    main(["search", "body", "--in", str(tree / "design"), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert {e["pattern"]: e["left_out"] for e in out["excludes"]} == {"old": 1, "archive": 1, "nothing*": 0}

    main(["search", "body", "--in", str(tree / "design")])
    assert "left out by exclusions, not searched: old (1), archive (1); see: docwhere status" in capsys.readouterr().err

    main(["search", "body", "--in", str(tree / "design"), "--no-update", "--json"])
    assert all(e["left_out"] is None for e in json.loads(capsys.readouterr().out)["excludes"])


def test_an_index_from_before_exclusions_is_kept_and_upgraded(tree, tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as con:
        con.executescript(
            "CREATE TABLE roots(key TEXT PRIMARY KEY, path TEXT NOT NULL, synced_at REAL);"
            "PRAGMA user_version=1;"
        )
        con.execute("INSERT INTO roots VALUES (?, ?, 1.0)", (path_key(tree), str(tree)))
    index = Index(db)
    try:
        assert index.roots()[0]["excludes"] == []
        index.add_excludes(tree, ["old"])
        assert index.excludes(tree) == ["old"]
    finally:
        index.close()
