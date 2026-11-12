"""Reading folders in a separate process, so a search can answer before a large folder is read.

A search that finds more to read than fits its wait puts the folder on a queue in the index
and starts one reader process (``docwhere reader``), unless one is already running. The reader
works through the queue a folder at a time, saving as it goes, and exits when the queue is
empty. It shows no window and needs no terminal: the search that started it can finish.

The state lives in the index (tables ``queue`` and ``reader``), so every docwhere process sees
the same reader and two searches never start two. The reader writes a heartbeat every few
seconds; one that stops beating (killed, the PC put to sleep) is replaced by the next search,
which then continues from what was saved.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from docwhere.index import FILE_TIMEOUT, Index, Stopped, SyncProgress, path_key

HEARTBEAT = 5  # seconds between heartbeats
STALE = 60  # a reader silent for this long is taken for dead
LOG_NAME = "reader.log"


@dataclass
class ReaderState:
    pid: int | None
    current: str | None  # the folder being read
    done: int
    total: int
    started: float
    queued: list[str]  # folders waiting, the current one excluded
    reading: list[tuple[str, float]] = field(default_factory=list)  # files being read and for how long

    def rate(self) -> float | None:
        """Files per second over this reader's current folder, once it has read a few."""
        elapsed = time.time() - self.started
        return self.done / elapsed if self.done >= 5 and elapsed > 0 else None


def state(index: Index) -> ReaderState | None:
    """The running reader, or None."""
    row = index.db.execute(
        "SELECT pid, heartbeat, started, current, done, total, reading FROM reader WHERE id = 1"
    ).fetchone()
    if not _alive(row):
        return None
    queued = [r["path"] for r in index.db.execute("SELECT path FROM queue ORDER BY requested_at")]
    try:
        reading = [(str(p), float(s)) for p, s in json.loads(row["reading"] or "[]")]
    except (ValueError, TypeError):
        reading = []
    return ReaderState(row["pid"], row["current"], row["done"], row["total"], row["started"] or time.time(),
                       [q for q in queued if q != row["current"]], reading)


def request(index: Index, root: Path) -> bool:
    """Queue ``root`` for reading and make sure a reader runs. True when one was started."""
    root = Path(root).resolve()
    db = index.db
    db.commit()
    db.execute("BEGIN IMMEDIATE")  # one search at a time decides whether to start a reader
    try:
        db.execute(
            "INSERT INTO queue(key, path, requested_at) VALUES (?, ?, ?) ON CONFLICT(key) DO NOTHING",
            (path_key(root), str(root), time.time()),
        )
        if _alive(db.execute("SELECT heartbeat FROM reader WHERE id = 1").fetchone()):
            db.commit()
            return False
        now = time.time()
        db.execute(
            "INSERT INTO reader(id, pid, heartbeat, started, current, done, total, stop) VALUES (1, NULL, ?, ?, NULL, 0, 0, 0)"
            " ON CONFLICT(id) DO UPDATE SET pid = NULL, heartbeat = excluded.heartbeat, started = excluded.started,"
            " current = NULL, done = 0, total = 0, stop = 0",
            (now, now),
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    _spawn(index.path)
    return True


def stop(index: Index) -> bool:
    """Ask the reader to stop and empty the queue. True when a reader was running."""
    running = state(index) is not None
    with index.db:
        index.db.execute("DELETE FROM queue")
        index.db.execute("UPDATE reader SET stop = 1 WHERE id = 1")
    return running


def serve(index: Index, timeout: float = FILE_TIMEOUT) -> int:
    """The reader process: read every queued folder, then exit."""
    from docwhere.progress import Reporter

    me = os.getpid()
    if not _claim(index, me):
        return 0  # another reader took over
    beat = threading.Event()
    threading.Thread(target=_heartbeat, args=(index.path, me, beat), daemon=True).start()
    log = open(index.path.with_name(LOG_NAME), "w", encoding="utf-8", buffering=1)  # noqa: SIM115 - held for the run
    try:
        while True:
            job = index.db.execute("SELECT key, path FROM queue ORDER BY requested_at LIMIT 1").fetchone()
            if job is None or _stop_asked(index):
                break
            with index.db:
                index.db.execute(
                "UPDATE reader SET current = ?, done = 0, total = 0, started = ?, reading = NULL WHERE id = 1",
                (job["path"], time.time()),
            )
            report = Reporter(f"reading {job['path']}", stream=log, live=False)
            try:
                scan = index.scan(Path(job["path"]))
                index.extract(scan, _Progress(index, report), timeout=timeout, should_stop=lambda: _stop_asked(index))
            except Stopped:
                print("stopped", file=log)
                break
            except (OSError, sqlite3.Error) as e:  # the folder is gone or unreachable: move on
                print(f"skipped {job['path']}: {e}", file=log)
            with index.db:
                index.db.execute("DELETE FROM queue WHERE key = ?", (job["key"],))
    finally:
        beat.set()
        with index.db:
            index.db.execute("DELETE FROM reader WHERE id = 1 AND pid = ?", (me,))
        log.close()
    return 0


class _Progress:
    """Writes how far the reader got into the index (for searches and status) and its log."""

    def __init__(self, index: Index, report) -> None:
        self.index, self.report, self.last = index, report, 0.0

    def __call__(self, p: SyncProgress) -> None:
        self.report(p)
        now = time.monotonic()
        if now - self.last >= 2 or p.done == p.total:
            self.last = now
            with self.index.db:
                reading = json.dumps([[str(path), round(seconds, 1)] for path, seconds in p.reading], ensure_ascii=False)
                self.index.db.execute(
                    "UPDATE reader SET done = ?, total = ?, reading = ? WHERE id = 1", (p.done, p.total, reading)
                )


def _alive(row) -> bool:
    return row is not None and row["heartbeat"] is not None and time.time() - row["heartbeat"] < STALE


def _claim(index: Index, me: int) -> bool:
    db = index.db
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    row = db.execute("SELECT pid, heartbeat FROM reader WHERE id = 1").fetchone()
    if row is not None and row["pid"] not in (None, me) and _alive(row):
        db.commit()
        return False
    db.execute(
        "INSERT INTO reader(id, pid, heartbeat, started) VALUES (1, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET pid = excluded.pid, heartbeat = excluded.heartbeat, stop = 0",
        (me, time.time(), time.time()),
    )
    db.commit()
    return True


def _heartbeat(db_path: Path, me: int, done: threading.Event) -> None:
    # its own connection: the main thread is busy inside a long read
    db = sqlite3.connect(db_path, timeout=30)
    try:
        while not done.wait(HEARTBEAT):
            with db:
                db.execute("UPDATE reader SET heartbeat = ? WHERE id = 1 AND pid = ?", (time.time(), me))
    finally:
        db.close()


def _stop_asked(index: Index) -> bool:
    row = index.db.execute("SELECT stop FROM reader WHERE id = 1").fetchone()
    return row is None or bool(row["stop"])


def _spawn(db_path: Path) -> None:
    spawn_detached(["--db", str(db_path), "reader"])


def spawn_detached(arguments: list[str]) -> None:
    """Start ``docwhere <arguments>`` as a process of its own that outlives the caller."""
    command = [sys.executable, "-m", "docwhere", *arguments]
    options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        # No window, its own process group (a Ctrl+C in the search's console does not reach it),
        # and out of the caller's job when the caller allows it, so it outlives the search.
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(command, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **options)
            return
        except OSError:
            pass  # the job forbids breaking away; the reader then ends with it and the next search restarts it
        subprocess.Popen(command, creationflags=flags, **options)
    else:
        subprocess.Popen(command, start_new_session=True, **options)
