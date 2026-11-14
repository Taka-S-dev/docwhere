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


def test_search_names_the_wikis_added_on_this_pc(docs, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "cli" / "index.db"))
    assert main(["search", "config", "--in", str(docs), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["wikis"] == []
    assert main(["wiki", "add", "社内", "http://wiki.example.invalid/"]) == 0
    assert "saved in" in capsys.readouterr().out
    assert main(["search", "config", "--in", str(docs), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["wikis"] == ["社内"]  # a new session learns of it here


def test_a_word_after_not_leaves_out_sections_and_folders_containing_it(docs, index):
    index.sync(docs)
    every = {Path(h.path).name for h in search(index, [docs], ["コマンド"], limit=20)}
    assert {"運用マニュアル.docx", "notes.md"} <= every
    kept = {Path(h.path).name for h in search(index, [docs], ["コマンド"], limit=20, exclude=["deploy"])}
    assert "notes.md" not in kept and "運用マニュアル.docx" in kept  # the section naming deploy is gone
    in_sub = {Path(h.path).name for h in search(index, [docs], ["設計"], limit=20, exclude=["sub"])}
    assert "設計書.pdf" not in in_sub  # a folder name in the path leaves its files out


def test_a_regular_expression_finds_codes_and_reports_what_it_matched(docs, index):
    (docs / "errors.md").write_text("# エラー\nE1001 は接続失敗。E2041 は時間切れ。e9 は対象外。\n", encoding="utf-8")
    index.sync(docs)
    [hit] = search(index, [docs], [r"E\d{4}"], regex=True)
    assert Path(hit.path).name == "errors.md" and hit.terms == ["E1001", "E2041"]
    assert search(index, [docs], ["E1001 は接続"], regex=True)[0].terms == ["E1001 は接続"]  # a literal: the index narrows


def test_a_bad_regular_expression_is_refused_before_anything_is_read(docs, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "cli" / "index.db"))
    assert main(["search", "E(", "--regex", "--in", str(docs)]) == 2
    assert "not a valid regular expression" in capsys.readouterr().err


def test_file_names_are_searched_including_files_not_read_yet(docs, tmp_path, capsys, monkeypatch):
    from docwhere import background

    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "cli" / "index.db"))
    monkeypatch.setattr(background, "request", lambda index, root: True)
    main(["index", str(docs)])
    (docs / "sub" / "運用マニュアル_旧版.docx").write_bytes(b"not read yet")
    capsys.readouterr()
    assert main(["search", "運用", "マニュアル", "--name", "--in", str(docs), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "name"
    assert [Path(h["path"]).name for h in out["hits"]] == ["運用マニュアル.docx", "運用マニュアル_旧版.docx"]
    assert out["hits"][0]["id"] and "not read yet" in out["hits"][1]["snippet"]
    assert main(["search", "マニュアル", "--name", "--not", "旧版", "--in", str(docs), "--json"]) == 0
    assert [Path(h["path"]).name for h in json.loads(capsys.readouterr().out)["hits"]] == ["運用マニュアル.docx"]
    assert main(["search", r"^説明.*\.pptx$", "--name", "--regex", "--in", str(docs)]) == 0
    assert "説明会.pptx" in capsys.readouterr().out


def test_other_wordings_are_found_for_the_same_thing(tmp_path):
    from docwhere.search import expand_variants, load_variant_groups

    assert expand_variants(["サーバ", "ログイン"]) == ["サーバー", "ログオン", "サインイン"]
    assert expand_variants(["サーバー"]) == ["サーバ"] and expand_variants(["マニュアル"]) == []
    own = tmp_path / "synonyms.csv"
    own.write_text("# 自分の言い換え\n帳票,レポート\n", encoding="utf-8-sig")
    assert expand_variants(["帳票"], load_variant_groups(own)) == ["レポート"]


def test_a_query_typed_in_a_box_keeps_phrases_and_leaves_out_minus_words():
    from docwhere.search import parse_query

    assert parse_query('接続 "応答 タイムアウト" -旧版 -"検討 資料" -') == (["接続", "応答 タイムアウト", "-"], ["旧版", "検討 資料"])
