from __future__ import annotations

import json
import sys
import time

import pytest

from docwhere import background, doctor
from docwhere.cli import main
from docwhere.index import Index

HEADER = "---\nname: office-search\ndescription: {}\n---\n\n# body\n"


@pytest.mark.parametrize(("description", "problem"), [
    ("Find documents. Use when asked where something is written.", None),
    (">-\n  Find documents: use when asked.\n  More text.", None),
    ("Find documents: use when asked.", "': ' or ' #'"),
    ("x" * 1025, "1025 characters"),
])
def test_skill_header_problems(description, problem):
    problems = doctor.skill_problems(HEADER.format(description), "office-search")
    assert (problems == []) if problem is None else any(problem in p for p in problems)


def test_skill_name_must_match_its_folder_and_a_copy_must_match_the_original(tmp_path):
    assert doctor.skill_problems(HEADER.format("ok"), "other") == ["name 'office-search' is not the folder's name 'other'"]
    original = tmp_path / "a" / "office-search" / "SKILL.md"
    copy = tmp_path / "b" / "office-search" / "SKILL.md"
    for path, text in ((original, "new"), (copy, "old")):
        path.parent.mkdir(parents=True)
        path.write_text(HEADER.format(text), encoding="utf-8")
    check = doctor._skill_check(copy, original)
    assert check.ok is False and "copy it again" in check.detail


@pytest.fixture
def probe(tmp_path, monkeypatch):
    started: list[list[str]] = []
    monkeypatch.setattr(background, "spawn_detached", started.append)
    index = Index(tmp_path / "index.db")
    yield index, index.path.with_name(doctor.PROBE_FILE), started
    index.close()


def test_survival_is_unknown_until_a_probe_reports(probe, monkeypatch):
    index, record, started = probe
    assert doctor._survival(index).ok is None and len(started) == 1
    data = json.loads(record.read_text(encoding="utf-8"))

    record.write_text(json.dumps({**data, "pid": 1}), encoding="utf-8")
    monkeypatch.setattr(doctor, "_alive", lambda pid: True)
    assert doctor._survival(index).ok is None  # still waiting for the command to end

    data = json.loads(record.read_text(encoding="utf-8"))
    record.write_text(json.dumps({**data, "pid": 1}), encoding="utf-8")
    monkeypatch.setattr(doctor, "_alive", lambda pid: False)
    assert doctor._survival(index).ok is False  # stopped with the command

    data = json.loads(record.read_text(encoding="utf-8"))
    record.write_text(json.dumps({**data, "pid": 1, "survived": True}), encoding="utf-8")
    assert doctor._survival(index).ok is True

    data = json.loads(record.read_text(encoding="utf-8"))
    record.write_text(json.dumps({**data, "spawned": time.time() - 60}), encoding="utf-8")
    assert doctor._survival(index).ok is False  # never started


def test_a_probe_writes_only_its_own_record(tmp_path, monkeypatch):
    record = tmp_path / "probe.json"
    record.write_text(json.dumps({"id": "later", "spawned": 1}), encoding="utf-8")
    monkeypatch.setattr(doctor, "PROBE_GRACE", 0)
    doctor.cmd_probe(type("A", (), {"parent": 999999, "record": record, "id": "earlier"}))
    assert json.loads(record.read_text(encoding="utf-8")) == {"id": "later", "spawned": 1}


def test_doctor_lists_every_check_and_fails_on_a_bad_skill(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(background, "spawn_detached", lambda arguments: None)
    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "db" / "index.db"))
    bad = tmp_path / "office-search" / "SKILL.md"
    bad.parent.mkdir()
    bad.write_text(HEADER.format("Find: things"), encoding="utf-8")
    assert main(["doctor", "--no-office", "--skill", str(bad), str(tmp_path)]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert [line[:5] for line in lines[:2]] == ["ok   ", "ok   "]
    assert any(line.startswith("FAIL skill") and "': '" in line for line in lines)
    assert any(line.startswith("ok   change check") for line in lines)

    main(["doctor", "--no-office", "--json"])
    names = [c["name"] for c in json.loads(capsys.readouterr().out)]
    assert names[:3] == ["index", "search engine", "background reading"]


@pytest.mark.skipif(sys.platform != "win32", reason="needs Office COM")
def test_office_probes_convert_with_each_application():
    pytest.importorskip("win32com.client")
    checks = doctor._office_checks()
    assert [c.name for c in checks] == ["Word (.doc)", "Excel (.xls)", "PowerPoint (.ppt)"]
    assert all(c.ok is not False for c in checks), checks


def test_change_check_times_the_listing(tmp_path, probe):
    index, _, _ = probe
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    check = doctor._scan_check(index, tmp_path)
    assert check.ok and check.detail.startswith("1 files listed")
    assert doctor._scan_check(index, tmp_path / "missing").ok is False

