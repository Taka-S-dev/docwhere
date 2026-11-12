"""Reading files in worker processes, so that one file that never finishes can be stopped.

Each worker is handed one file at a time, so the main process always knows which file every
worker is reading and for how long. A worker still on its file after the timeout is killed,
together with any application it started (Office, for the legacy formats), and its file is
reported as timed out; a new worker takes the next file. Files whose reader drives a desktop
application go to one worker of their own, which keeps the application open between files.
A pooled worker whose reader finds that a file needs the application hands the file on to
that worker.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from multiprocessing.connection import Connection, wait
from typing import Any

TICK = 1.0  # seconds between reports while files are being read
STOP_GRACE = 30  # seconds a worker gets to close its application after the last file

Job = Callable[[str], Any]


@dataclass
class Task:
    path: str
    serial: bool
    data: Any = None  # handed back with the result


@dataclass
class Outcome:
    task: Task
    result: Any = None  # what the job returned
    failure: str | None = None  # set instead when the worker was stopped or died
    timed_out: bool = False


def _serve(conn: Connection, job: Job, serial: bool) -> None:
    from docwhere import extractors

    extractors.set_worker("serial" if serial else "pooled")
    extractors.on_process_started(lambda pid: conn.send(("pid", pid)))
    try:
        while (path := conn.recv()) is not None:
            try:
                conn.send(("done", job(path)))
            except extractors.NeedsApplication:
                conn.send(("serial", None))
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        extractors.release()


@dataclass
class _Worker:
    serial: bool
    job: Job
    proc: Any = None
    conn: Connection | None = None
    task: Task | None = None
    started: float = 0.0
    apps: set[int] = field(default_factory=set)

    def start(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        self.conn, child = ctx.Pipe()
        self.proc = ctx.Process(target=_serve, args=(child, self.job, self.serial), daemon=True)
        self.proc.start()
        child.close()

    def give(self, task: Task) -> None:
        if self.proc is None:
            self.start()
        self.task, self.started = task, time.monotonic()
        self.conn.send(task.path)

    def kill(self) -> None:
        for pid in self.apps:
            try:
                os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
            except OSError:
                pass  # already gone
        self.apps.clear()
        if self.proc is not None:
            self.proc.kill()
            self.proc.join()
            self.conn.close()
        self.proc = self.conn = self.task = None

    def stop(self) -> None:
        """Let the worker close its application, then make sure it is gone."""
        if self.proc is None:
            return
        try:
            self.conn.send(None)
        except OSError:
            pass
        self.proc.join(STOP_GRACE)
        if self.proc.is_alive():
            self.kill()
        else:
            self.conn.close()
            self.proc = self.conn = None


def run(
    tasks: list[Task],
    job: Job,
    workers: int,
    timeout: float,
    tick: Callable[[list[tuple[str, float]]], None] | None = None,
) -> Iterator[Outcome]:
    """Run ``job(path)`` for every task, yielding outcomes as they finish.

    ``tick`` is called about every second with the files being read and how long each has
    taken so far, so a caller can show progress even while no file finishes.
    """
    queues = {False: deque(t for t in tasks if not t.serial), True: deque(t for t in tasks if t.serial)}
    pool = [_Worker(False, job) for _ in range(min(workers, len(queues[False])))]
    if queues[True]:
        pool.append(_Worker(True, job))
    next_tick = 0.0
    try:
        while True:
            for w in pool:
                if w.task is None and queues[w.serial]:
                    w.give(queues[w.serial].popleft())
            busy = [w for w in pool if w.task is not None]
            if not busy:
                return
            ready = wait([w.conn for w in busy], timeout=max(0.05, next_tick - time.monotonic()))
            for w in busy:
                if w.conn not in ready:
                    continue
                try:
                    kind, value = w.conn.recv()
                except (EOFError, OSError):
                    w.proc.join(5)
                    code = w.proc.exitcode
                    task = w.task
                    w.kill()
                    yield Outcome(task, failure=f"the reader stopped on this file (exit code {code})")
                    continue
                if kind == "pid":
                    w.apps.add(value)
                elif kind == "serial":
                    task, w.task = w.task, None
                    queues[True].append(Task(task.path, True, task.data))
                    if not any(p.serial for p in pool):
                        pool.append(_Worker(True, job))
                else:
                    task, w.task = w.task, None
                    yield Outcome(task, result=value)
            now = time.monotonic()
            for w in pool:
                if w.task is not None and now - w.started > timeout:
                    task = w.task
                    w.kill()
                    yield Outcome(task, failure=f"timed out after {timeout:.0f}s and was stopped", timed_out=True)
            if now >= next_tick:
                next_tick = now + TICK
                if tick:
                    tick([(w.task.path, now - w.started) for w in pool if w.task is not None])
    finally:
        for w in pool:
            if w.task is not None:
                w.kill()  # interrupted while reading
            else:
                w.stop()
