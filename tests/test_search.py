import json
from pathlib import Path

from docwhere.cli import main
from docwhere.search import search, term_groups


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


def test_a_term_past_the_candidate_limit_is_reported(docs, index, monkeypatch):
    import docwhere.search as search_module

    index.sync(docs)
    capped: list[str] = []
    search(index, [docs], ["config", "退避"], capped=capped)
    assert capped == []  # a few sections each: all of them ranked
    monkeypatch.setattr(search_module, "CANDIDATES_PER_TERM", 1)
    found: dict = {}
    search(index, [docs], ["config", "退避"], capped=capped, found=found)  # indexed and short (LIKE) terms alike
    assert capped == ["config", "退避"]
    assert found["at_least"] is True  # the count is then only how many were looked at


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
    # one a file was listed; every match is counted, so the agent can tell there is more to read
    assert out["matched"]["files"] == len(out["hits"]) and out["matched"]["sections"] > len(out["hits"])

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


def test_a_partly_scanned_pdf_is_listed_and_old_reads_are_redone(docs, index):
    from test_extractors import _partly_scanned

    _partly_scanned(docs / "mixed.pdf")
    index.sync(docs)
    [(path, note)] = index.partly_read([docs])
    assert path.endswith("mixed.pdf") and "(p.2)" in note
    assert next(r for r in index.roots() if r["path"] == str(docs))["partly"] == 1
    assert index.sync(docs).updated == 0
    # PDFs read before the last change to how PDFs are read are read again; other formats are not
    from docwhere.extractors import OUTPUT_VERSION_BY_EXT

    index.db.execute("UPDATE files SET version = ?", (OUTPUT_VERSION_BY_EXT[".pdf"] - 1,))
    assert [Path(t[1]).suffix for t in index.scan(docs).todo] == [".pdf", ".pdf"]


def test_every_match_is_counted_before_a_few_of_each_file_are_kept(docs, index):
    index.sync(docs)
    found: dict = {}
    hits = search(index, [docs], ["config"], per_file=1, limit=2, found=found)
    assert len(hits) == 2 and found["files"] == 4 and found["sections"] > 4


def test_a_later_page_of_a_section_does_not_take_its_heading_again():
    from docwhere.search import W_TITLE, score_row

    first = {"title": "操作ログの使い方", "crumb": "", "body": "操作ログ", "path": "a.pdf", "continued": 0}
    later = {**first, "continued": 1}  # the next page, under the same bookmark
    assert score_row(first, ["操作ログ"])[0] - score_row(later, ["操作ログ"])[0] == W_TITLE
    assert score_row({k: v for k, v in first.items() if k != "continued"}, ["操作ログ"]) == score_row(first, ["操作ログ"])  # a wiki row


def test_a_file_over_the_size_limit_is_listed_as_unreadable_not_skipped_unseen(docs, index, monkeypatch):
    import docwhere.index as index_module

    monkeypatch.setattr(index_module, "MAX_FILE_BYTES", 1024)
    (docs / "huge.txt").write_text("config " * 400, encoding="utf-8")  # 2.8 KB, over the limit set here
    index.sync(docs)
    errors = dict(index.errors([docs]))
    assert "too large to read" in errors[str(docs / "huge.txt")]
    assert all(not h.path.endswith("huge.txt") for h in search(index, [docs], ["config"], limit=50))


def test_a_scan_of_every_section_stops_at_its_time_and_says_so(docs, index, monkeypatch):
    import docwhere.search as search_module

    index.sync(docs)
    capped: list[str] = []
    assert search(index, [docs], ["c.nfig"], regex=True, capped=capped)  # no three plain letters: every section
    assert capped == []
    monkeypatch.setattr(search_module, "SCAN_SECONDS", 0.0)
    found: dict = {}
    search(index, [docs], ["c.nfig"], regex=True, capped=capped, found=found)
    assert capped == ["c.nfig"] and found["at_least"] is True  # cut short, and the rest is said to be unseen


def test_a_hit_says_which_pages_its_heading_runs_over(tmp_path, capsys, monkeypatch):
    import json

    import pymupdf

    from docwhere.cli import main

    folder = tmp_path / "books"
    folder.mkdir()
    doc = pymupdf.open()
    for body in ["献立を決める", "キャベツを洗う", "ニンジンを切る", "ドレッシングを混ぜる", "タマネギを炒める", "皿に盛る"]:
        doc.new_page().insert_htmlbox(pymupdf.Rect(50, 300, 545, 340), f"<p>{body}</p>")
    doc.set_toc([[1, "1 準備", 1], [1, "2 サラダ", 2], [1, "3 仕上げ", 6]])
    doc.save(folder / "料理.pdf")
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "index" / "index.db"))

    assert main(["search", "ドレッシング", "皿", "--in", str(folder), "--json"]) == 0
    hits = {h["location"]: h for h in json.loads(capsys.readouterr().out)["hits"]}
    assert hits["p.4"]["heading_pages"] == "p.2-5"  # the salad's heading runs over pages 2 to 5
    assert hits["p.6"]["heading_pages"] is None  # a heading on one page says nothing more

    assert main(["search", "ドレッシング", "--in", str(folder)]) == 0
    assert "p.4 (the heading runs over p.2-5)" in capsys.readouterr().out


def _notes(tmp_path, files: dict) -> Path:
    folder = tmp_path / "notes"
    folder.mkdir()
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def test_wordings_of_one_thing_count_as_one_and_the_subject_decides(tmp_path, index):
    folder = _notes(tmp_path, {
        # says both wordings of "default" again and again, nothing on the subject asked for
        "reset.md": "# 設定の初期化\n" + "既定値をやめて初期値にする。\n" * 8,
        "oven.md": "# 画面\nオーブンの温度を決める。\n初期値は 180。\n",
    })
    index.sync(folder)
    hits = search(index, [folder], ["初期値", "既定値", "デフォルト", "オーブン"])
    assert Path(hits[0].path).name == "oven.md"
    assert term_groups(["初期値", "既定値", "サーバ", "サーバー", "オーブン"]) == [0, 0, 1, 1, 2]


def test_a_must_term_is_required_and_any_of_its_wordings_will_do(tmp_path, index):
    folder = _notes(tmp_path, {
        "a.md": "# 火力\n火加減のデフォルトは 180。\n",
        "b.md": "# 音量\n音量の既定値は 5。\n",
        "c.md": "# サーバー\nサーバーの既定値は 8080。\n",
    })
    index.sync(folder)
    assert [Path(h.path).name for h in search(index, [folder], ["既定値", "デフォルト"], must=["火加減"])] == ["a.md"]
    # サーバ is required; the document writes サーバー, which is the same thing
    assert [Path(h.path).name for h in search(index, [folder], ["既定値", "サーバー"], must=["サーバ"])] == ["c.md"]
    assert search(index, [folder], ["既定値"], must=["どこにもない語"]) == []


def test_the_snippet_is_the_line_that_says_something_about_the_subject(tmp_path, index):
    folder = _notes(tmp_path, {
        "a.md": "# 火加減\n火加減\n6042ページ 火加減のデフォルト\n火加減モードを選ぶ。\nデフォルト: 180\n",
    })
    index.sync(folder)
    [hit] = search(index, [folder], ["火加減", "デフォルト"])
    assert hit.snippet == "デフォルト: 180"  # not the heading repeated, nor the line pointing to another page


def test_two_things_close_together_outrank_the_same_two_far_apart(tmp_path, index):
    folder = _notes(tmp_path, {
        "near.md": "# 野菜\nトマトは16個まで並べられます。\n",
        "far.md": "# 盛り付け\nトマトの切り方を選びます。" + "ほかの説明が続きます。" * 6 + "1皿に30個まで盛り付けできます。\n",
    })
    index.sync(folder)
    assert [Path(h.path).name for h in search(index, [folder], ["トマト", "個まで"], require_all=True)] == ["near.md", "far.md"]


def test_search_looks_for_other_wordings_and_says_which_parts_of_a_term_exist(tmp_path, capsys, monkeypatch):
    folder = _notes(tmp_path, {"a.md": "# 接続\nサーバのアドレスを登録できる。\n"})
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "db" / "index.db"))
    assert main(["search", "サーバー", "--in", str(folder), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["also_searched"] == ["サーバ"] and len(out["hits"]) == 1
    assert out["hits"][0]["line"] == "サーバのアドレスを登録できる。"  # the hit line whole, to quote from
    assert main(["search", "サーバー", "--exact", "--in", str(folder), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["hits"] == []
    assert main(["search", "何個まで登録できる", "--in", str(folder), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hits"] == [] and [s["part"] for s in out["suggest"]] == ["登録できる"]
    assert main(["search", "アドレス", "--must", "サーバー", "--in", str(folder), "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["hits"]) == 1


def test_search_without_a_folder_searches_the_index_and_never_the_working_folder(docs, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "cli" / "index.db"))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "x.md").write_text("# x\nconfig\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    assert main(["search", "config"]) == 2  # nothing indexed: asks for --in rather than index this folder
    assert "--in DIR" in capsys.readouterr().err
    assert main(["search", "config", "--in", str(docs), "--json"]) == 0
    capsys.readouterr()
    assert main(["search", "config", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["roots"] == [str(docs)] and out["hits"]
    assert all(not h["path"].startswith(str(elsewhere)) for h in out["hits"])


def test_show_reads_a_page_by_its_printed_number_and_only_the_lines_asked_for(tmp_path, capsys, monkeypatch):
    from test_folio import _manual

    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "cli" / "index.db"))
    folder = tmp_path / "docs"
    folder.mkdir()
    _manual(folder / "m.pdf").close()  # printed numbers are two lower than the page index
    assert main(["search", "Install", "--in", str(folder), "--json"]) == 0
    hit = json.loads(capsys.readouterr().out)["hits"][0]
    assert main(["show", "--id", str(hit["id"]), "--page", "23"]) == 0
    assert "[p.25（印刷 23）]" in capsys.readouterr().out
    assert main(["show", "--id", str(hit["id"]), "--page", "p25"]) == 0
    assert "page reference 25" in capsys.readouterr().out
    assert main(["show", "--id", str(hit["id"]), "--page", "900"]) == 2
    assert main(["show", "--id", str(hit["id"]), "-C", "2", "--grep", "reference 13"]) == 0
    out = capsys.readouterr().out
    assert "page reference 13" in out and "page reference 12" not in out and "(no line here has it)" in out
    assert main(["show", "--id", "999999"]) == 2
    assert "no section with id 999999" in capsys.readouterr().err


def test_open_takes_a_section_id(docs, tmp_path, capsys, monkeypatch):
    from docwhere import opener

    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "cli" / "index.db"))
    assert main(["search", "config", "--in", str(docs), "--ext", "pdf", "--json"]) == 0
    hit = json.loads(capsys.readouterr().out)["hits"][0]
    opened = []
    monkeypatch.setattr(opener, "open_at", lambda path, anchor, app: opened.append((path, anchor.get("page"))) or "opened")
    assert main(["search", "deploy", "--in", str(docs), "--json"]) == 0  # another search ran since
    assert main(["open", "--id", str(hit["id"]), "--term", "config"]) == 0
    assert opened == [(hit["path"], int(hit["location"][2:]))]
    assert main(["open"]) == 2


def test_a_hit_says_that_its_page_holds_pictures_and_a_miss_that_the_files_do(tmp_path, capsys, monkeypatch):
    from test_extractors import _manual_with_pictures

    folder = tmp_path / "books"
    folder.mkdir()
    _manual_with_pictures(folder / "manual.pdf")
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "index" / "index.db"))

    def hit(term):
        assert main(["search", term, "--in", str(folder), "--json"]) == 0
        return json.loads(capsys.readouterr().out)["hits"][0]

    assert (hit("Section 3 is")["location"], hit("Section 3 is")["pictures"]) == ("p.3", {"count": 1, "page_share": 0.18})
    assert (hit("Section 6 is")["location"], hit("Section 6 is")["pictures"]) == ("p.6", None)

    # a third of a manual's pages hold a screen: the list a person reads does not say so on each
    assert main(["search", "Section 3 is", "--in", str(folder)]) == 0
    assert "picture" not in capsys.readouterr().out

    # nothing found: the words may be in a picture, and the answer says where pictures are
    assert main(["search", "watchdog", "--in", str(folder), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hits"] == []
    assert out["pictures_unread"] == {
        "pictures": 1, "pages": 1, "files": [{"path": str(folder / "manual.pdf"), "pictures": 1, "pages": 1}]}
    assert main(["search", "watchdog", "--in", str(folder)]) == 0
    assert "the files searched hold 1 picture(s) on 1 page(s)" in capsys.readouterr().err


def test_counting_pictures_can_be_turned_off_and_on_again(tmp_path, capsys, monkeypatch):
    from test_extractors import _manual_with_pictures

    from docwhere.index import Index

    folder = tmp_path / "books"
    folder.mkdir()
    _manual_with_pictures(folder / "manual.pdf")
    db = tmp_path / "index" / "index.db"
    monkeypatch.setenv("DOCWHERE_DB", str(db))
    monkeypatch.delenv("DOCWHERE_COUNT_PICTURES", raising=False)

    def searched(term):
        assert main(["search", term, "--in", str(folder), "--json"]) == 0
        return json.loads(capsys.readouterr().out)

    assert main(["pictures", "off"]) == 0
    assert "pictures: off" in capsys.readouterr().out
    out = searched("Section 3 is")
    assert out["index"]["updated"] == 1 and out["hits"][0]["pictures"] is None
    assert searched("watchdog")["pictures_unread"] is None
    assert Index(db).db.execute("SELECT pictures FROM files").fetchone()[0] is None  # not counted, not "none"

    # turned on: the PDF already in the index is read again, though the file did not change
    assert main(["pictures", "on"]) == 0
    capsys.readouterr()
    out = searched("Section 3 is")
    assert out["index"]["updated"] == 1 and out["hits"][0]["pictures"] == {"count": 1, "page_share": 0.18}
    assert searched("Section 3 is")["index"]["updated"] == 0  # and only once

    # turned off: nothing is read again, and the count that was made is no longer told
    assert main(["pictures", "off"]) == 0
    capsys.readouterr()
    out = searched("Section 3 is")
    assert out["index"]["updated"] == 0 and out["hits"][0]["pictures"] is None
    assert main(["pictures"]) == 0
    assert "pictures: off" in capsys.readouterr().out


def test_a_pdf_without_pictures_is_not_read_again_for_the_count(tmp_path, capsys, monkeypatch):
    import pymupdf

    folder = tmp_path / "books"
    folder.mkdir()
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Only words on this page")
    doc.save(folder / "plain.pdf")
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "index" / "index.db"))
    monkeypatch.delenv("DOCWHERE_COUNT_PICTURES", raising=False)
    for updated in (1, 0):  # counted and found to have none: that is known, and it is current
        assert main(["search", "Only words", "--in", str(folder), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["index"]["updated"] == updated


def _pdf(path: Path, pages: list[str], heading: str | None = None, *more_headings) -> None:
    import pymupdf

    doc = pymupdf.open()
    for text in pages:
        doc.new_page().insert_text((72, 72), text, fontname="japan", fontsize=11)
    if heading:
        doc.set_toc([[1, heading, 1], *more_headings])
    doc.save(path)


def test_all_the_words_are_found_across_the_pages_of_one_section(tmp_path, index):
    root = tmp_path / "docs"
    root.mkdir()
    filler = "野菜を切って下ごしらえをする。\n" * 10
    # the dish named on one page and the setting asked about on a later one, under one heading
    _pdf(root / "far.pdf", ["煮込みの手順\n" + filler + "鍋を用意する。", filler, "強火で沸かす。\n" + filler], "煮込みの手順")
    _pdf(root / "next.pdf", ["焼き物の手順\n" + filler + "鍋を用意する。", "強火で沸かす。\n" + filler], "焼き物の手順")
    _pdf(root / "same.pdf", ["蒸し物の手順\n鍋を用意する。強火で沸かす。"], "蒸し物の手順")
    # under two headings: words of two sections are not found together
    _pdf(root / "apart.pdf", ["揚げ物\n鍋を用意する。", "炒め物\n強火で沸かす。"], "揚げ物", [1, "炒め物", 2])
    index.sync(root)
    hits = search(index, [root], ["鍋", "強火"], require_all=True, limit=10)
    names = [Path(h.path).name for h in hits]
    assert names == ["same.pdf", "next.pdf", "far.pdf"]  # the closer, the higher
    assert hits[0].elsewhere is None
    assert hits[1].elsewhere == [{"term": "強火", "location": "p.2", "snippet": "強火で沸かす。"}]
    assert hits[2].location == "p.1" and hits[2].elsewhere[0]["location"] == "p.3"


def test_a_long_section_read_in_pieces_holds_its_words_together(tmp_path, index):
    import docx

    root = tmp_path / "docs"
    root.mkdir()
    d = docx.Document()
    d.add_heading("長い節", 1)
    d.add_paragraph("ここで鍋を出す。")
    for i in range(140):  # past MAX_CHUNK_CHARS: the section is indexed in two pieces
        d.add_paragraph(f"{i} 番目の段落。材料をそろえて、順に下ごしらえをしておく。")
    d.add_paragraph("強火にかける。")
    d.save(root / "long.docx")
    index.sync(root)
    hits = search(index, [root], ["鍋", "強火"], require_all=True)
    assert len(hits) == 1 and hits[0].elsewhere[0]["term"] == "強火"


def test_a_file_named_for_one_word_holds_it_for_require_all(tmp_path, index):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "煮込み.md").write_text("# 手順\n火加減は中火。\n", encoding="utf-8")
    (root / "焼き物.md").write_text("# 手順\n火加減は強火。\n", encoding="utf-8")
    index.sync(root)
    hits = search(index, [root], ["煮込み", "火加減"], require_all=True)
    assert [Path(h.path).name for h in hits] == ["煮込み.md"]


def test_the_pages_of_one_section_are_one_hit_and_name_the_others(tmp_path, index):
    root = tmp_path / "docs"
    root.mkdir()
    # three pages of one heading say it, then a page of another heading
    _pdf(root / "book.pdf", ["鍋の手入れ\n鍋は乾かす。", "鍋は重ねない。", "鍋の底を磨く。", "包丁\n鍋の横に置く。"],
         "鍋の手入れ", [1, "包丁", 4])
    index.sync(root)
    hits = search(index, [root], ["鍋"], per_file=5)
    assert [h.title for h in hits].count("鍋の手入れ") == 1 and len(hits) == 2
    first = next(h for h in hits if h.title == "鍋の手入れ")
    # the page with the heading ranks first and stands for the section; the others are named
    assert first.location == "p.1"
    assert [(p["location"], p["anchor"]["page"]) for p in first.more_in_section] == [("p.2", 2), ("p.3", 3)]
    other = next(h for h in hits if h.title == "包丁")
    assert other.more_in_section is None  # another heading's page is a hit of its own


def test_a_spreadsheets_rows_are_hits_of_their_own(tmp_path, index):
    import openpyxl

    root = tmp_path / "docs"
    root.mkdir()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "在庫"
    ws.append(["品名", "メモ"])
    for i in range(400):  # rows enough to be read in several parts
        ws.append([f"品 {i}", "鍋" if i in (3, 390) else "皿"])
    wb.save(root / "stock.xlsx")
    index.sync(root)
    hits = search(index, [root], ["鍋"], per_file=5)
    assert len(hits) >= 2 and all(h.more_in_section is None for h in hits)


def test_pages_are_named_as_runs():
    from docwhere.search import pages_label

    places = [{"location": f"p.{n}"} for n in (17, 12, 13, 14, 21, 23, 22)]
    assert pages_label(places) == "p.12-14, p.17, p.21-23"
    assert pages_label([{"location": ""}, {"location": "p.3"}]) == ""  # not all pages: counted instead

def test_a_section_opening_with_several_bookmarks_is_still_one(tmp_path, index):
    root = tmp_path / "docs"
    root.mkdir()
    # two bookmarks start on page 1: its title names both, page 2's only the inner one;
    # the heading path is the same, and so is the section
    _pdf(root / "two.pdf", ["下ごしらえ\n鍋を用意する。", "強火で沸かす。"], "台所", [2, "下ごしらえ", 1])
    index.sync(root)
    hits = search(index, [root], ["鍋", "強火"], require_all=True)
    assert [Path(h.path).name for h in hits] == ["two.pdf"]
    assert hits[0].elsewhere[0]["location"] == "p.2"
