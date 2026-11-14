"""The time left of a sync, estimated again as files finish.

Files per second since the start misleads: the quick files go first (several readers take
them in parallel) while the Office conversions, one at a time, are left for last, and the
pace changes as the sync goes (a busy PC, a share that slows down). So each file left is
weighed by what files of its kind took on this PC before (a .doc conversion weighs about ten
times a .xlsx), and that weight left is divided by the weight read per second over the last
half minute of the clock. The conversions and the parallel readers are two lanes working at
once; the sync ends when the slower one does. Until a lane has a recent pace, the weights
alone give its time.
"""

from __future__ import annotations

import os
import time
from collections import Counter, deque
from collections.abc import Callable
from pathlib import Path

DEFAULT_SECONDS = 0.3  # a file of a kind never read on this PC
DEFAULT_CONVERSION = 1.5  # the same for a legacy .doc/.ppt converted through Office
WINDOW = 30.0  # seconds of the clock the pace is taken over
MIN_FILES = 3  # files a lane must have finished within the window to have a pace
MIN_WEIGHT = 0.001  # even a file read in no measurable time counts


class Estimate:
    """Kept as running sums, so a sync of a hundred thousand files can ask after every file."""

    def __init__(
        self,
        paths: list[Path],
        serial: Callable[[Path], bool],
        workers: int,
        history: dict[str, float],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """``history``: average seconds per file by extension on this PC; ``workers``: readers in parallel."""
        self.workers = max(1, workers)
        self.clock = clock
        self.left: dict[str, tuple[bool, float]] = {}  # path -> (serial, weight in seconds)
        self.total = {False: 0.0, True: 0.0}  # weight of the files left, per lane
        self.sizes = {False: Counter(), True: Counter()}  # weight -> files left, per lane
        for p in paths:
            ext = os.path.splitext(str(p))[1].lower()
            lane = serial(p)
            weight = max(MIN_WEIGHT, history.get(ext, DEFAULT_CONVERSION if lane else DEFAULT_SECONDS))
            self.left[str(p)] = (lane, weight)
            self.total[lane] += weight
            self.sizes[lane][weight] += 1
        self.recent: dict[bool, deque] = {False: deque(), True: deque()}  # (when, weight) of files read

    def done(self, path: Path | str, seconds: float = 0.0) -> None:
        entry = self.left.pop(str(path), None)
        if entry is None:
            return
        lane, weight = entry
        self.total[lane] -= weight
        self.sizes[lane][weight] -= 1
        if not self.sizes[lane][weight]:
            del self.sizes[lane][weight]
        self.recent[lane].append((self.clock(), weight))

    def pace(self, lane: bool, now: float) -> float | None:
        """Weight read per second of the clock over the last WINDOW seconds, or None."""
        recent = self.recent[lane]
        while recent and now - recent[0][0] >= WINDOW:  # the files of the last WINDOW seconds, the start excluded
            recent.popleft()
        if len(recent) < MIN_FILES:
            return None
        # from the first of them finishing: the time before it (starting Word, say) is no pace;
        # up to now, not to the last one: a file holding the lane up slows the pace as it should
        span = now - recent[0][0]
        return sum(w for _, w in list(recent)[1:]) / span if span > 0 else None

    def seconds_left(self, reading: list[tuple[Path | str, float]] = ()) -> float:
        """Seconds until every file is read, ``reading`` being the files under way and how long
        each has taken so far."""
        now = self.clock()
        need = {}
        for lane in (False, True):
            left = max(0.0, self.total[lane])
            if not left:
                need[lane] = 0.0
                continue
            pace = self.pace(lane, now)
            if pace:
                need[lane] = left / pace
            else:
                parallel = 1 if lane else self.workers
                need[lane] = max(left / parallel, max(self.sizes[lane], default=0.0))
        # a file under way past its weight holds its lane up: assume it is half way through
        for path, spent in reading:
            entry = self.left.get(str(path))
            if entry is not None:
                lane, weight = entry
                need[lane] = max(need[lane], weight - spent if weight > spent * 1.5 else spent)
        return max(0.0, need[False], need[True])
