"""SQLite index of files and their chunks, with a trigram FTS5 table for substring search.

The index is a cache: it lives under %LOCALAPPDATA%\\docwhere, is kept current by comparing
mtime and size on every sync, and is simply rebuilt when its schema version changes.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from docwhere import workers as worker_pool
from docwhere.excludes import Matcher, normalize
from docwhere.extractors import (
    EXTRACTOR_VERSION,
    OUTPUT_VERSION,
    NeedsApplication,
    extract,
    is_serial,
    supported_extensions,
)
from docwhere.model import Chunk

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 200 * 1024 * 1024
SKIP_DIRS = {"node_modules", "__pycache__", "$RECYCLE.BIN", "System Volume Information"}
# Each worker loads the extraction libraries once (a second or more), so a few files share one.
FILES_PER_WORKER = 20
MAX_WORKERS = 8
# A file still being read after this long is given up on and recorded as unreadable: an Office
# conversion waiting on a dialog, or a reader stuck in a damaged file, would otherwise stop the
# whole sync. Generous, since a large workbook can legitimately take minutes.
FILE_TIMEOUT = 180
# Extracted files are saved in batches; a sync stopped part way loses at most the last batch.
SAVE_EVERY_FILES = 50
SAVE_EVERY_SECONDS = 5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS roots(
    key TEXT PRIMARY KEY, path TEXT NOT NULL, synced_at REAL,
    excludes TEXT NOT NULL DEFAULT '[]', exclude_hits TEXT);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, path TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, version INTEGER NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    title TEXT NOT NULL, crumb TEXT NOT NULL, body TEXT NOT NULL, anchors TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
CREATE TABLE IF NOT EXISTS queue(
    key TEXT PRIMARY KEY, path TEXT NOT NULL, requested_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reader(
    id INTEGER PRIMARY KEY CHECK (id = 1), pid INTEGER, heartbeat REAL, started REAL,
    current TEXT, done INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
    stop INTEGER NOT NULL DEFAULT 0);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    title, crumb, body, content='chunks', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, title, crumb, body)
    VALUES (new.id, new.title, new.crumb, new.body);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, title, crumb, body)
    VALUES ('delete', old.id, old.title, old.crumb, old.body);
END;
"""

@dataclass
class SyncProgress:
    done: int
    total: int
    reading: list[tuple[Path, float]]  # files being read now, with the seconds spent so far


Progress = Callable[[SyncProgress], None]


def default_db_path() -> Path:
    if env := os.environ.get("DOCWHERE_DB"):
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or Path.home() / ".cache"
    return Path(base) / "docwhere" / "index.db"


def path_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(path))


def prefix_of(root_key: str) -> str:
    return root_key if root_key.endswith(os.sep) else root_key + os.sep


@dataclass
class SyncStats:
    files: int = 0
    updated: int = 0
    removed: int = 0
    failed: int = 0
    timed_out: list[str] = field(default_factory=list)
    excluded_by: tuple[str, str] | None = None  # (folder, pattern) when the folder itself is left out
    # every pattern that applied, keyed (folder it is set on, pattern): the folders and files it
    # left out in this sync (a left-out folder counts once, its contents are not walked)
    left_out: dict[tuple[str, str], int] = field(default_factory=dict)


@dataclass
class Scan:
    root: Path
    todo: list[tuple]  # (key, path, mtime_ns, size) of every file still to read
    stats: SyncStats
    # the entries of ``todo`` read before and modified since (not new, not a retry after an
    # extractor update): a search can leave these as last read
    changed: list[tuple] = field(default_factory=list)


class Stopped(Exception):
    """A sync was asked to stop part way (``Index.extract``'s ``should_stop``)."""


class Index:
    def __init__(self, db_path: Path | None = None) -> None:
        self.path = Path(db_path or default_db_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = self._connect()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)  # wait out a concurrent sync instead of failing
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            db.close()
            for suffix in ("", "-wal", "-shm"):
                Path(f"{self.path}{suffix}").unlink(missing_ok=True)
            db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(_SCHEMA)
        # Columns added after the first release: added in place so an existing index is kept.
        columns = {r["name"] for r in db.execute("PRAGMA table_info(roots)")}
        if "excludes" not in columns:
            db.execute("ALTER TABLE roots ADD COLUMN excludes TEXT NOT NULL DEFAULT '[]'")
        if "exclude_hits" not in columns:
            db.execute("ALTER TABLE roots ADD COLUMN exclude_hits TEXT")
        if "read_ms" not in {r["name"] for r in db.execute("PRAGMA table_info(files)")}:
            db.execute("ALTER TABLE files ADD COLUMN read_ms INTEGER")  # how long reading the file took
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return db

    def close(self) -> None:
        self.db.close()

    # ---- writing -----------------------------------------------------------------------

    def sync(
        self, root: Path, progress: Progress | None = None, workers: int | None = None, timeout: float = FILE_TIMEOUT
    ) -> SyncStats:
        """Bring the index for ``root`` up to date: extract new or changed files, drop deleted ones.

        Files that could not be read are tried again when they change or when the extractors
        do (``EXTRACTOR_VERSION``); files that were read, only when their output would change.
        """
        return self.extract(self.scan(root), progress, workers, timeout)

    def scan(self, root: Path) -> Scan:
        """The quick half of a sync: list the folder, drop files that are gone and find the files
        to read. Nothing is extracted, so this takes seconds even for a large folder."""
        root = Path(root).resolve()
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        root_key = path_key(root)

        stats = SyncStats()
        matcher = self._matcher(root_key)
        hits: dict[tuple[str, str], int] = {}
        if rule := matcher.match(root_key):  # a parent folder's pattern leaves this whole folder out
            stats.excluded_by = (self._root_path(rule[0]), rule[1])
            hits[rule] = 1
            on_disk = {}
        else:
            on_disk = {path_key(p): (p, st.st_mtime_ns, st.st_size) for p, st in _walk(root, matcher, hits)}
        stats.left_out = {(self._root_path(k), p): hits.get((k, p), 0) for k, p in matcher.sources()}
        known = {r["key"]: r for r in self._files_under(root_key, "id, key, mtime_ns, size, version, error")}
        stats.files = len(on_disk)

        gone = [r["id"] for k, r in known.items() if k not in on_disk]
        todo = [(k, p, mtime, size) for k, (p, mtime, size) in on_disk.items() if not _current(known.get(k), mtime, size)]
        changed = [t for t in todo if t[0] in known and (known[t[0]]["mtime_ns"], known[t[0]]["size"]) != (t[2], t[3])]
        stats.removed = len(gone)
        hits_json = json.dumps({p: hits.get((root_key, p), 0) for p in self._excludes(root_key)}, ensure_ascii=False)
        with self.db:
            self._delete_files(gone)
            self.db.execute(
                "INSERT INTO roots(key, path, synced_at, exclude_hits) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET path = excluded.path, synced_at = excluded.synced_at,"
                " exclude_hits = excluded.exclude_hits",
                (root_key, str(root), time.time(), hits_json),
            )
        return Scan(root, todo, stats, changed)

    def extract(
        self,
        scan: Scan,
        progress: Progress | None = None,
        workers: int | None = None,
        timeout: float = FILE_TIMEOUT,
        should_stop: Callable[[], bool] | None = None,
    ) -> SyncStats:
        """The slow half of a sync: read the files ``scan`` found. When ``should_stop`` returns
        true (asked about every second) the reading stops with ``Stopped``; what was read is kept."""
        stats, todo = scan.stats, scan.todo
        done, reading, saved = 0, [], time.monotonic()

        def tick(now_reading: list[tuple[str, float]] | None = None) -> None:
            nonlocal reading
            if should_stop and should_stop():
                raise Stopped
            if now_reading is not None:
                reading = [(Path(p), s) for p, s in now_reading]
            if progress:
                progress(SyncProgress(done, len(todo), reading))

        outcomes = _extract_all(todo, workers, timeout, tick)
        try:
            for key, path, mtime, size, chunks, error, timed_out, seconds in outcomes:
                self._store(key, path, mtime, size, chunks, error, seconds)
                done += 1
                stats.updated += 1
                stats.failed += error is not None
                if timed_out:
                    stats.timed_out.append(str(path))
                if done % SAVE_EVERY_FILES == 0 or time.monotonic() - saved >= SAVE_EVERY_SECONDS:
                    self.db.commit()  # a sync stopped part way keeps what it read
                    saved = time.monotonic()
                tick()
        finally:
            outcomes.close()  # stops the readers, and the Office instances they started, at once
            self.db.commit()
        return stats

    def pending(self, todo: list[tuple]) -> list[tuple]:
        """The entries of a scan's ``todo`` that are still not read (another process may be
        reading them)."""
        left = []
        for i in range(0, len(todo), 500):
            batch = todo[i : i + 500]
            marks = ",".join("?" * len(batch))
            rows = {
                r["key"]: r
                for r in self.db.execute(
                    f"SELECT key, mtime_ns, size, version, error FROM files WHERE key IN ({marks})", [t[0] for t in batch]
                )
            }
            left += [t for t in batch if not _current(rows.get(t[0]), t[2], t[3])]
        return left

    def forget(self, root: Path, keep_excludes: bool = False) -> None:
        """Drop everything indexed under ``root`` (the next sync re-extracts it) and, unless
        ``keep_excludes``, the folder's exclusion patterns."""
        root_key = path_key(Path(root).resolve())
        with self.db:
            self._delete_files([r["id"] for r in self._files_under(root_key, "id")])
            if not keep_excludes:
                self.db.execute("DELETE FROM roots WHERE key = ?", (root_key,))

    # ---- exclusions --------------------------------------------------------------------

    def excludes(self, root: Path) -> list[str]:
        """The patterns set on ``root`` itself (see ``docwhere.excludes``)."""
        return self._excludes(path_key(Path(root).resolve()))

    def add_excludes(self, root: Path, patterns: list[str]) -> tuple[list[str], int]:
        """Leave ``patterns`` out of ``root`` from now on. Indexed files they match are dropped
        at once, without a sync. Returns the patterns that were new and the number of files dropped."""
        root = Path(root).resolve()
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        key = path_key(root)
        current = self._excludes(key)
        added: list[str] = []
        for p in map(normalize, patterns):
            if p.lower() not in {c.lower() for c in current + added}:
                added.append(p)
        with self.db:
            self.db.execute(
                "INSERT INTO roots(key, path, excludes) VALUES (?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET excludes = excluded.excludes",
                (key, str(root), json.dumps(current + added, ensure_ascii=False)),
            )
            matcher = self._matcher(key)
            dropped = [r["id"] for r in self._files_under(key, "id, key") if matcher.match(r["key"])]
            self._delete_files(dropped)
        return added, len(dropped)

    def remove_excludes(self, root: Path, patterns: list[str] | None = None) -> tuple[list[str], list[str]]:
        """Stop leaving ``patterns`` (all of them when None) out of ``root``; the next sync reads
        their files. Returns the patterns removed and the given ones that were not set."""
        key = path_key(Path(root).resolve())
        current = self._excludes(key)
        wanted = None if patterns is None else {normalize(p).lower() for p in patterns}
        removed = [c for c in current if wanted is None or c.lower() in wanted]
        set_now = {c.lower() for c in current}
        unknown = [p for p in patterns or [] if normalize(p).lower() not in set_now]
        keep = [c for c in current if c not in removed]
        with self.db:
            self.db.execute("UPDATE roots SET excludes = ? WHERE key = ?", (json.dumps(keep, ensure_ascii=False), key))
            # a folder registered only to hold patterns goes away with its last one
            self.db.execute("DELETE FROM roots WHERE key = ? AND synced_at IS NULL AND excludes = '[]'", (key,))
        return removed, unknown

    def excludes_applying(self, root: Path) -> list[dict]:
        """Every pattern that leaves something out under ``root``, with the folder it is set on."""
        rules = self._matcher(path_key(Path(root).resolve())).sources()
        return [{"pattern": p, "set_on": self._root_path(k)} for k, p in rules]

    def _excludes(self, root_key: str) -> list[str]:
        row = self.db.execute("SELECT excludes FROM roots WHERE key = ?", (root_key,)).fetchone()
        return json.loads(row["excludes"]) if row else []

    def _matcher(self, root_key: str) -> Matcher:
        """The patterns that apply while indexing ``root_key``: those set on it, on a folder
        above it and on a folder below it."""
        rules = []
        for r in self.db.execute("SELECT key, excludes FROM roots WHERE excludes != '[]'"):
            k = r["key"]
            if k == root_key or root_key.startswith(prefix_of(k)) or k.startswith(prefix_of(root_key)):
                rules += [(k, p) for p in json.loads(r["excludes"])]
        return Matcher(rules)

    def _root_path(self, root_key: str) -> str:
        row = self.db.execute("SELECT path FROM roots WHERE key = ?", (root_key,)).fetchone()
        return row["path"] if row else root_key

    def _store(
        self, key: str, path: Path, mtime: int, size: int, chunks: list[Chunk], error: str | None, seconds: float = 0.0
    ) -> None:
        row = self.db.execute("SELECT id FROM files WHERE key = ?", (key,)).fetchone()
        read_ms = round(seconds * 1000)
        if row:
            file_id = row["id"]
            self.db.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
            self.db.execute(
                "UPDATE files SET path = ?, mtime_ns = ?, size = ?, version = ?, error = ?, read_ms = ? WHERE id = ?",
                (str(path), mtime, size, EXTRACTOR_VERSION, error, read_ms, file_id),
            )
        else:
            file_id = self.db.execute(
                "INSERT INTO files(key, path, mtime_ns, size, version, error, read_ms) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, str(path), mtime, size, EXTRACTOR_VERSION, error, read_ms),
            ).lastrowid
        self.db.executemany(
            "INSERT INTO chunks(file_id, title, crumb, body, anchors) VALUES (?, ?, ?, ?, ?)",
            [(file_id, c.title, c.crumb, "\n".join(c.lines), _pack_anchors(c.anchors)) for c in chunks],
        )

    def _delete_files(self, ids: list[int]) -> None:
        for i in range(0, len(ids), 500):
            batch = ids[i : i + 500]
            marks = ",".join("?" * len(batch))
            self.db.execute(f"DELETE FROM chunks WHERE file_id IN ({marks})", batch)
            self.db.execute(f"DELETE FROM files WHERE id IN ({marks})", batch)

    # ---- reading -----------------------------------------------------------------------

    def _files_under(self, root_key: str, columns: str) -> list[sqlite3.Row]:
        prefix = prefix_of(root_key)
        return self.db.execute(
            f"SELECT {columns} FROM files WHERE substr(key, 1, ?) = ?", (len(prefix), prefix)
        ).fetchall()

    def roots(self) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT key, path, synced_at, excludes, exclude_hits FROM roots ORDER BY path"):
            prefix = prefix_of(r["key"])
            files, errors, chunks = self.db.execute(
                "SELECT count(*), count(f.error),"
                " (SELECT count(*) FROM chunks c JOIN files g ON g.id = c.file_id WHERE substr(g.key, 1, ?) = ?)"
                " FROM files f WHERE substr(f.key, 1, ?) = ?",
                (len(prefix), prefix, len(prefix), prefix),
            ).fetchone()
            hits = json.loads(r["exclude_hits"] or "{}")
            excludes = [{"pattern": p, "matched": hits.get(p)} for p in json.loads(r["excludes"])]
            out.append({
                "path": r["path"], "synced_at": r["synced_at"], "files": files, "chunks": chunks, "errors": errors,
                "excludes": excludes,
            })
        return out

    def read_times(self) -> dict[str, dict]:
        """Per extension, the files whose reading time is known and the seconds they took, the
        slowest kind first: where indexing spends its time on this PC."""
        totals: dict[str, list] = {}
        for r in self.db.execute("SELECT path, read_ms FROM files WHERE read_ms IS NOT NULL"):
            ext = os.path.splitext(r["path"])[1].lower()
            entry = totals.setdefault(ext, [0, 0])
            entry[0] += 1
            entry[1] += r["read_ms"]
        return {
            ext: {"files": n, "seconds": round(ms / 1000, 1), "average": round(ms / n / 1000, 2)}
            for ext, (n, ms) in sorted(totals.items(), key=lambda kv: -kv[1][1])
        }

    def errors(self, roots: list[Path] | None = None) -> list[tuple[str, str]]:
        """Files that could not be read (all of them, not only those that failed in the last sync)."""
        if roots is None:
            rows = self.db.execute("SELECT path, error FROM files WHERE error IS NOT NULL ORDER BY path").fetchall()
        else:
            rows = []
            for root in roots:
                rows += [r for r in self._files_under(path_key(Path(root).resolve()), "path, error") if r["error"]]
            rows.sort(key=lambda r: r["path"])
        return [(r["path"], r["error"]) for r in rows]

    def files_in(self, roots: list[Path]) -> list[sqlite3.Row]:
        """path and mtime of every indexed file under ``roots``."""
        rows: list[sqlite3.Row] = []
        for root in roots:
            rows += self._files_under(path_key(Path(root).resolve()), "path, mtime_ns")
        return rows

    def chunk_with_neighbors(self, chunk_id: int, context: int = 0) -> list[sqlite3.Row]:
        """The chunk plus up to ``context`` chunks before and after it from the same file."""
        row = self.db.execute("SELECT file_id FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if row is None:
            return []
        return self.db.execute(
            "SELECT c.id, c.title, c.crumb, c.body, c.anchors, f.path FROM chunks c JOIN files f ON f.id = c.file_id"
            " WHERE c.file_id = ? AND c.id BETWEEN ? AND ? ORDER BY c.id",
            (row["file_id"], chunk_id - context, chunk_id + context),
        ).fetchall()


def _pack_anchors(anchors: list[dict]) -> str:
    """Store one anchor when every line shares it (Word sections, PDF pages, slides)."""
    if anchors and all(a == anchors[0] for a in anchors):
        anchors = anchors[:1]
    return json.dumps(anchors, ensure_ascii=False, separators=(",", ":"))


def _current(row: sqlite3.Row | None, mtime: int, size: int) -> bool:
    if row is None or (row["mtime_ns"], row["size"]) != (mtime, size):
        return False
    if row["error"] is not None:
        return row["version"] == EXTRACTOR_VERSION
    return OUTPUT_VERSION <= row["version"] <= EXTRACTOR_VERSION


def _walk(root: Path, matcher: Matcher, hits: dict) -> Iterator[tuple[Path, os.stat_result]]:
    """Readable files under ``root``. ``hits`` counts, per (folder, pattern) rule, the folders
    and files it left out; a left-out folder is not entered, so its contents are not counted."""
    exts = supported_extensions()

    def left_out(path: str) -> bool:
        rule = matcher.match(path_key(path)) if matcher else None
        if rule:
            hits[rule] = hits.get(rule, 0) + 1
        return rule is not None

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS and not left_out(os.path.join(dirpath, d))
        ]
        for name in filenames:
            if name.startswith("~$") or os.path.splitext(name)[1].lower() not in exts:
                continue  # ~$ = Office lock file
            path = Path(dirpath, name)
            if left_out(str(path)):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_size <= MAX_FILE_BYTES:
                yield path, st


def _extract_job(path: str) -> tuple[list[Chunk], str | None]:
    try:
        return extract(Path(path)), None
    except NeedsApplication:
        raise  # the worker hands the file on to the application's worker
    except Exception as e:  # one unreadable file must not stop the sync
        return [], f"{type(e).__name__}: {e}"


def _extract_all(todo: list[tuple], workers: int | None, timeout: float, tick) -> Iterator[tuple]:
    """(key, path, mtime, size, chunks, error, timed_out, seconds) for every file in ``todo``."""
    job = _extract_job  # looked up here so a test can put a slow reader in its place
    tasks = [worker_pool.Task(str(item[1]), is_serial(item[1]), item) for item in todo]
    pooled = sum(not t.serial for t in tasks)
    workers = workers or min(MAX_WORKERS, os.cpu_count() or 1, -(-pooled // FILES_PER_WORKER))
    for out in worker_pool.run(tasks, job, max(1, workers), timeout, tick):
        key, path, mtime, size = out.task.data
        if out.failure:
            yield key, path, mtime, size, [], out.failure, out.timed_out, out.seconds
        else:
            yield key, path, mtime, size, *out.result, False, out.seconds
