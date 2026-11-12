"""Files are read in worker processes that can be stopped one at a time."""

import os
import subprocess
import sys
import time
from pathlib import Path

from docwhere import index as index_module
from docwhere import workers
from docwhere.search import search


def _read(path):
    """Stands in for a reader: 'hang' never finishes, 'crash' kills its worker."""
    name = Path(path).name
    if "hang" in name:
        time.sleep(600)
    if "crash" in name:
        os._exit(3)
    return name


def _hang_in_app(path):
    """Starts an 'application', reports it as the extractors do, then waits on it forever."""
    from docwhere.extractors import process_started

    app = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    Path(path).write_text(str(app.pid))
    process_started(app.pid)
    time.sleep(600)


def _hang_on_named(path, *args):
    if "hang" in Path(path).name:
        time.sleep(600)
    return index_module._extract_job(path, *args)


def _alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=True).stdout
    return str(pid) in out


def test_a_file_that_never_finishes_is_stopped_and_the_rest_are_read():
    tasks = [workers.Task(n, serial=False) for n in ("a", "hang", "b", "c")]
    started = time.monotonic()
    seen = []
    outcomes = list(workers.run(tasks, _read, 2, timeout=2, tick=seen.append))
    assert time.monotonic() - started < 30
    by_path = {o.task.path: o for o in outcomes}
    assert by_path["hang"].timed_out and "timed out" in by_path["hang"].failure
    assert [by_path[n].result for n in ("a", "b", "c")] == ["a", "b", "c"]
    assert any(path == "hang" and spent >= 1 for reading in seen for path, spent in reading)


def test_a_worker_that_dies_loses_only_its_file():
    tasks = [workers.Task(n, serial=n.startswith("s")) for n in ("crash", "a", "s1", "s2")]
    outcomes = {o.task.path: o for o in workers.run(tasks, _read, 1, timeout=60)}
    assert "exit code 3" in outcomes["crash"].failure and not outcomes["crash"].timed_out
    assert [outcomes[n].result for n in ("a", "s1", "s2")] == ["a", "s1", "s2"]


def test_the_application_a_stopped_file_was_waiting_on_is_closed_too(tmp_path):
    marker = tmp_path / "pid.txt"
    [outcome] = workers.run([workers.Task(str(marker), serial=True)], _hang_in_app, 1, timeout=3)
    assert outcome.timed_out
    pid = int(marker.read_text())
    for _ in range(20):
        if not _alive(pid):
            break
        time.sleep(0.2)
    assert not _alive(pid)


def test_sync_records_a_timed_out_file_and_does_not_wait_on_it_again(docs, index, monkeypatch):
    (docs / "hang.md").write_text("# x\nnever read\n", encoding="utf-8")
    monkeypatch.setattr(index_module, "_extract_job", _hang_on_named)
    stats = index.sync(docs, timeout=3)
    assert [Path(p).name for p in stats.timed_out] == ["hang.md"]
    assert {Path(p).name: e for p, e in index.errors([docs])}["hang.md"].startswith("timed out")
    assert search(index, [docs], ["config"])  # the other files were read
    assert index.sync(docs, timeout=3).updated == 0


def test_files_that_failed_are_tried_again_when_the_extractors_change(docs, index, monkeypatch):
    (docs / "broken.docx").write_bytes(b"not a zip")
    index.sync(docs)
    monkeypatch.setattr(index_module, "EXTRACTOR_VERSION", index_module.EXTRACTOR_VERSION + 1)
    assert index.sync(docs).updated == 1  # only the unreadable one; the others' output still holds
    monkeypatch.setattr(index_module, "OUTPUT_VERSION", index_module.EXTRACTOR_VERSION)
    assert index.sync(docs).updated == len(index.files_in([docs])) - 1  # the broken one was just tried


def _needs_app_when_named(path):
    """Reads a file in a pooled worker unless its name says it needs the application."""
    from docwhere.extractors import NeedsApplication, may_start_application

    if "app" in Path(path).name and not may_start_application():
        raise NeedsApplication
    return f"{Path(path).name} in {os.getpid()}"


def test_a_file_that_needs_the_application_is_handed_to_its_worker():
    tasks = [workers.Task(name, False) for name in ("a.xls", "app.xls", "b.xls", "app2.xls")]
    outcomes = {o.task.path: o for o in workers.run(tasks, _needs_app_when_named, 2, timeout=60)}
    assert set(outcomes) == {"a.xls", "app.xls", "b.xls", "app2.xls"}
    assert all(o.failure is None for o in outcomes.values())
    pids = {name: o.result.split(" in ")[1] for name, o in outcomes.items()}
    assert outcomes["app.xls"].task.serial and outcomes["app2.xls"].task.serial
    assert pids["app.xls"] == pids["app2.xls"]  # one application worker, kept for both
    assert pids["app.xls"] not in (pids["a.xls"], pids["b.xls"])
