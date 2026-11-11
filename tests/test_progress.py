"""Progress lines for a reader that is not a terminal (an agent running the command)."""

import io
from pathlib import Path

from docwhere import progress
from docwhere.index import SyncProgress


def test_lines_come_every_few_seconds_and_name_a_slow_file(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(progress.time, "monotonic", lambda: clock[0])
    out = io.StringIO()
    report = progress.Reporter("indexing docs", out, live=False)

    report(SyncProgress(0, 400, [(Path("a.docx"), 0.0)]))
    clock[0] += 3
    report(SyncProgress(30, 400, []))  # too soon for another line
    clock[0] += 17
    report(SyncProgress(100, 400, [(Path("仕様.doc"), 2.0), (Path("big.xlsx"), 45.0)]))
    clock[0] += 5
    report(SyncProgress(400, 400, []))
    report(SyncProgress(400, 400, []))  # a last tick after the end adds nothing

    assert out.getvalue().splitlines() == [
        "indexing docs: 0/400 files (0%)",
        "indexing docs: 100/400 files (25%), 5.0 files/s, about 1m 00s left; reading big.xlsx for 45s",
        "indexing docs: 400/400 files (100%) in 25s",
    ]


def test_durations():
    assert [progress.duration(s) for s in (5, 151, 3725)] == ["5s", "2m 31s", "1h 02m"]
