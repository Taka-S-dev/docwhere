"""Saving settings from two windows at once."""

import json
import multiprocessing
import threading

import pytest

from docwhere.settings import load_settings, save_settings, settings_path


def _save_many(db, name, times):
    for i in range(times):
        save_settings(db, **{name: i})


def test_saves_from_several_processes_and_threads_keep_every_setting(tmp_path):
    db = tmp_path / "index.db"
    save_settings(db, title="手順書", pdf_app={"command": ["x.exe", "{file}"]})
    # the reading window saving its place as it is moved, while the index window saves its own
    ctx = multiprocessing.get_context("spawn")
    others = [ctx.Process(target=_save_many, args=(db, f"process{n}", 40)) for n in range(2)]
    for p in others:
        p.start()
    threads = [threading.Thread(target=_save_many, args=(db, f"thread{n}", 40)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for p in others:
        p.join(60)
        assert p.exitcode == 0
    saved = load_settings(db)
    assert saved["title"] == "手順書" and saved["pdf_app"] == {"command": ["x.exe", "{file}"]}
    assert all(saved[f"process{n}"] == 39 for n in range(2)) and all(saved[f"thread{n}"] == 39 for n in range(3))


def test_a_file_that_cannot_be_read_is_not_written_over(tmp_path):
    db = tmp_path / "index.db"
    settings_path(db).write_text('{"title": "手順書", "pdf_app": {', encoding="utf-8")  # cut off
    with pytest.raises(ValueError, match="保存しませんでした"):
        save_settings(db, zoom={"mode": "fit"})
    assert settings_path(db).read_text(encoding="utf-8") == '{"title": "手順書", "pdf_app": {'
    settings_path(db).write_text(json.dumps({"title": "手順書"}), encoding="utf-8")
    save_settings(db, zoom={"mode": "fit"})
    assert load_settings(db) == {"title": "手順書", "zoom": {"mode": "fit"}}


def test_nothing_is_saved_in_a_folder_others_can_write_to(tmp_path, monkeypatch):
    import docwhere.settings as settings_module

    db = tmp_path / "index.db"
    planted = json.dumps({"pdf_app": {"command": ["planted.exe", "{file}"]}})
    settings_path(db).write_text(planted, encoding="utf-8")
    monkeypatch.setattr(settings_module, "private", lambda folder: False)
    with pytest.raises(PermissionError, match="保存しません"):
        save_settings(db, zoom={"mode": "fit"})
    assert settings_path(db).read_text(encoding="utf-8") == planted  # nothing written, nothing written through
    assert not list(tmp_path.glob("settings*.tmp")) and not (tmp_path / "settings.lock").exists()


def test_a_save_leaves_no_temporary_file_behind(tmp_path):
    db = tmp_path / "index.db"
    save_settings(db, title="手順書")
    save_settings(db, zoom={"mode": "fit"})
    assert load_settings(db) == {"title": "手順書", "zoom": {"mode": "fit"}}
    assert not list(tmp_path.glob("settings*.tmp"))
