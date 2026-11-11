"""Progress of a sync, written to stderr so that --json output on stdout stays intact.

On a terminal one line is rewritten in place. Elsewhere (an agent or a script reading the
output) a full line is written every ``EVERY`` seconds instead, so the reader can tell a long
sync from a stuck one: each line says how far it got, the rate, the time left, and which file
has been taking long.
"""

from __future__ import annotations

import shutil
import sys
import time
from typing import TextIO

from docwhere.index import SyncProgress

EVERY = 10  # seconds between lines when stderr is not a terminal
SLOW = 10  # a file read for longer than this is named


def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


class Reporter:
    def __init__(self, label: str, stream: TextIO | None = None, live: bool | None = None) -> None:
        self.label = label
        self.stream = stream or sys.stderr
        self.live = self.stream.isatty() if live is None else live
        self.started: float | None = None
        self.last = 0.0
        self.finished = False

    def __call__(self, p: SyncProgress) -> None:
        now = time.monotonic()
        if self.started is None:
            self.started = now
        if self.finished:
            return
        finished = self.finished = p.done == p.total
        if self.live:
            width = shutil.get_terminal_size().columns - 1
            print("\r" + self.line(p, now)[:width].ljust(width), end="\n" if finished else "", file=self.stream, flush=True)
        elif finished or self.last == 0.0 or now - self.last >= EVERY:
            self.last = now
            print(self.line(p, now), file=self.stream, flush=True)

    def line(self, p: SyncProgress, now: float) -> str:
        text = f"{self.label}: {p.done}/{p.total} files ({p.done * 100 // max(p.total, 1)}%)"
        elapsed = now - self.started
        if p.done == p.total:
            return text + f" in {duration(elapsed)}"
        if p.done and elapsed >= 1:
            rate = p.done / elapsed
            # Slow files tend to be left for last, so this is a lower bound more than a forecast.
            text += f", {rate:.1f} files/s, about {duration((p.total - p.done) / rate)} left"
        slow = max(p.reading, key=lambda r: r[1], default=None)
        if slow and (self.live or slow[1] >= SLOW):
            text += f"; reading {slow[0].name}" + (f" for {duration(slow[1])}" if slow[1] >= SLOW else "")
        return text
