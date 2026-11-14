"""The time left of a sync: files weighed by kind, divided by the pace of the last half minute."""

from pathlib import Path

from docwhere.eta import Estimate


def _serial(path: Path) -> bool:
    return Path(path).suffix in (".doc", ".ppt")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_conversions_left_for_last_are_not_hidden_by_the_quick_files_read_first():
    clock = Clock()
    paths = [Path(f"{i}.md") for i in range(90)] + [Path(f"{i}.doc") for i in range(10)]
    e = Estimate(paths, _serial, workers=4, history={".md": 0.1, ".doc": 6.0}, clock=clock)
    for p in paths[:90]:
        clock.now += 0.03
        e.done(p)
    # files per second so far (90 in under 3 s) would say well under a second for the last 10
    assert e.seconds_left() == 60.0


def test_the_pace_of_the_last_half_minute_decides_not_the_history():
    clock = Clock()
    paths = [Path(f"{i}.doc") for i in range(300)]
    e = Estimate(paths, _serial, workers=1, history={".doc": 1.0}, clock=clock)
    for p in paths[:200]:
        clock.now += 1.0
        e.done(p)
    assert round(e.seconds_left()) == 100
    for p in paths[200:250]:
        clock.now += 10.0  # Office slowed down: ten times as long per file
        e.done(p)
    assert round(e.seconds_left()) == 500  # 50 left at the new pace, not at the old one


def test_parallel_quick_files_are_estimated_from_how_fast_they_really_go():
    clock = Clock()
    paths = [Path(f"{i}.txt") for i in range(6000)]
    e = Estimate(paths, _serial, workers=8, history={}, clock=clock)
    for p in paths[:3850]:
        clock.now += 1 / 660  # 660 files a second, far quicker than the guess for an unknown kind
        e.done(p)
    assert 2.5 < e.seconds_left() < 4.5


def test_a_file_running_past_its_weight_keeps_the_estimate_from_reaching_zero():
    e = Estimate([Path("big.doc")], _serial, workers=1, history={".doc": 2.0}, clock=Clock())
    assert e.seconds_left([(Path("big.doc"), 1.0)]) == 2.0  # the weight, until a pace is known
    assert e.seconds_left([(Path("big.doc"), 30.0)]) == 30.0  # assumed half way


def test_without_a_pace_yet_parallel_readers_share_the_quick_files_but_not_one_long_file():
    e = Estimate([Path(f"{i}.md") for i in range(8)] + [Path("huge.pdf")], _serial, workers=4,
                 history={".md": 1.0, ".pdf": 20.0}, clock=Clock())
    assert e.seconds_left() == 20.0
