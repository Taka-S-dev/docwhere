import json
from pathlib import Path

from docwhere.cli import main
from docwhere.search import search


def test_sync_skips_lock_files_and_is_incremental(docs, index):
    first = index.sync(docs)
    assert (first.files, first.updated, first.failed) == (5, 5, 0)
    assert index.sync(docs).updated == 0

    (docs / "notes.md").write_text("# changed\n", encoding="utf-8")
    (docs / "説明会.pptx").unlink()
    again = index.sync(docs)
    assert (again.updated, again.removed) == (1, 1)


def test_section_whose_heading_names_the_term_ranks_first(docs, index):
    index.sync(docs)
    hits = search(index, [docs], ["config"], per_file=1)
    top = hits[0]
    assert Path(top.path).name == "運用マニュアル.docx"
    assert top.crumb == "コマンド > config コマンド"
    # the intro also mentions config, but only in passing
    assert all(h.crumb != "はじめに" for h in hits[:4])


def test_every_kind_reports_where_the_hit_is(docs, index):
    index.sync(docs)
    by_file = {Path(h.path).name: h for h in search(index, [docs], ["config"], per_file=1, limit=20)}
    assert by_file["設計書.pdf"].location in ("p.2", "p.3")
    assert by_file["コマンド一覧.xlsx"].location == "CLI!A2"
    assert by_file["コマンド一覧.xlsx"].snippet == "コマンド: config / 概要: 設定を検証する"
    assert by_file["説明会.pptx"].location == "slide 2"
    assert by_file["運用マニュアル.docx"].anchor["term"] == "config"


def test_short_terms_width_variants_and_require_all(docs, index):
    index.sync(docs)
    assert search(index, [docs], ["退避"])  # 2 characters: LIKE fallback
    assert search(index, [docs], ["ｃｏｎｆｉｇ"])  # full-width query
    both = search(index, [docs], ["config", "--force"], require_all=True, limit=20)
    assert both and all(set(h.terms) == {"config", "--force"} for h in both)


def test_scope_by_folder_and_extension(docs, index):
    index.sync(docs)
    assert {Path(h.path).name for h in search(index, [docs / "sub"], ["config"])} == {"設計書.pdf"}
    assert {Path(h.path).suffix for h in search(index, [docs], ["config"], exts=["xlsx"])} == {".xlsx"}


def test_unreadable_file_is_recorded_not_fatal(docs, index):
    (docs / "broken.docx").write_bytes(b"not a zip")
    stats = index.sync(docs)
    assert stats.failed == 1
    assert [Path(p).name for p, _ in index.errors([docs])] == ["broken.docx"]


def test_older_version_is_flagged_and_ranked_below_newer(docs, index):
    import shutil

    spec = docs / "運用マニュアル.docx"
    shutil.copy(spec, docs / "sub" / "運用マニュアル_v1.docx")
    shutil.move(spec, docs / "運用マニュアル_v2.docx")
    index.sync(docs)
    hits = [h for h in search(index, [docs], ["config"], per_file=1, limit=20) if "運用マニュアル" in h.path]
    assert [Path(h.path).name for h in hits] == ["運用マニュアル_v2.docx", "運用マニュアル_v1.docx"]
    assert hits[0].newer_version is None
    assert Path(hits[1].newer_version).name == "運用マニュアル_v2.docx"


def test_cli_json_lists_unreadable_files_even_when_nothing_changed(docs, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "cli" / "index.db"))
    (docs / "broken.docx").write_bytes(b"not a zip")
    main(["index", str(docs)])
    capsys.readouterr()
    assert main(["search", "config", "--in", str(docs), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["index"]["updated"] == 0
    assert out["unreadable_count"] == 1 and out["unreadable"][0]["path"].endswith("broken.docx")


def test_cli_search_json_then_show(docs, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "cli" / "index.db"))
    assert main(["search", "config", "--in", str(docs), "--json", "--per-file", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["index"]["updated"] == 5
    assert out["hits"][0]["crumb"] == "コマンド > config コマンド"

    assert main(["show", "1"]) == 0
    shown = capsys.readouterr().out
    assert "config は設定ファイルを読み込み、値を検証する。" in shown
    assert main(["show", "99"]) == 2

    assert main(["show", "--id", str(out["hits"][0]["id"])]) == 0
    assert "config は設定ファイルを読み込み" in capsys.readouterr().out
