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
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path

from docwhere.extractors import EXTRACTOR_VERSION, extract, is_serial, release, supported_extensions
from docwhere.model import Chunk

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 200 * 1024 * 1024
SKIP_DIRS = {"node_modules", "__pycache__", "$RECYCLE.BIN", "System Volume Information"}
# Starting worker processes costs ~3 s on Windows (each re-imports the extractors), which
# outweighs the gain until there are a few dozen files to extract.
PARALLEL_THRESHOLD = 40
FILES_PER_WORKER = 20

_SCHEMA = """
CREATE TABLE IF NOT EXISTS roots(
    key TEXT PRIMARY KEY, path TEXT NOT NULL, synced_at REAL);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, path TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL, size INTEGER NOT NULL, version INTEGER NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    title TEXT NOT NULL, crumb TEXT NOT NULL, body TEXT NOT NULL, anchors TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
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

Progress = Callable[[int, int], None]


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
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return db

    def close(self) -> None:
        self.db.close()

    # ---- writing -----------------------------------------------------------------------

    def sync(self, root: Path, progress: Progress | None = None, workers: int | None = None) -> SyncStats:
        """Bring the index for ``root`` up to date: extract new or changed files, drop deleted ones."""
        root = Path(root).resolve()
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        root_key = path_key(root)

        on_disk = {path_key(p): (p, st.st_mtime_ns, st.st_size) for p, st in _walk(root)}
        known = {r["key"]: r for r in self._files_under(root_key, "id, key, mtime_ns, size, version")}
        stats = SyncStats(files=len(on_disk))

        gone = [r["id"] for k, r in known.items() if k not in on_disk]
        todo = [
            (k, p, mtime, size)
            for k, (p, mtime, size) in on_disk.items()
            if (r := known.get(k)) is None or (r["mtime_ns"], r["size"], r["version"]) != (mtime, size, EXTRACTOR_VERSION)
        ]
        with self.db:
            self._delete_files(gone)
        stats.removed = len(gone)

        for done, (key, path, mtime, size, chunks, error) in enumerate(_extract_all(todo, workers), 1):
            if error is _RETRY:
                stats.failed += 1  # worker crashed; leave the file unrecorded so the next sync retries it
            else:
                self._store(key, path, mtime, size, chunks, error)
                stats.updated += 1
                stats.failed += error is not None
            if done % 50 == 0:
                self.db.commit()
            if progress:
                progress(done, len(todo))

        self.db.execute(
            "INSERT INTO roots(key, path, synced_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET path = excluded.path, synced_at = excluded.synced_at",
            (root_key, str(root), time.time()),
        )
        self.db.commit()
        return stats

    def forget(self, root: Path) -> None:
        """Drop everything indexed under ``root`` (the next sync re-extracts it)."""
        root_key = path_key(Path(root).resolve())
        with self.db:
            self._delete_files([r["id"] for r in self._files_under(root_key, "id")])
            self.db.execute("DELETE FROM roots WHERE key = ?", (root_key,))

    def _store(self, key: str, path: Path, mtime: int, size: int, chunks: list[Chunk], error: str | None) -> None:
        row = self.db.execute("SELECT id FROM files WHERE key = ?", (key,)).fetchone()
        if row:
            file_id = row["id"]
            self.db.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
            self.db.execute(
                "UPDATE files SET path = ?, mtime_ns = ?, size = ?, version = ?, error = ? WHERE id = ?",
                (str(path), mtime, size, EXTRACTOR_VERSION, error, file_id),
            )
        else:
            file_id = self.db.execute(
                "INSERT INTO files(key, path, mtime_ns, size, version, error) VALUES (?, ?, ?, ?, ?, ?)",
                (key, str(path), mtime, size, EXTRACTOR_VERSION, error),
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
        for r in self.db.execute("SELECT key, path, synced_at FROM roots ORDER BY path"):
            prefix = prefix_of(r["key"])
            files, errors, chunks = self.db.execute(
                "SELECT count(*), count(f.error),"
                " (SELECT count(*) FROM chunks c JOIN files g ON g.id = c.file_id WHERE substr(g.key, 1, ?) = ?)"
                " FROM files f WHERE substr(f.key, 1, ?) = ?",
                (len(prefix), prefix, len(prefix), prefix),
            ).fetchone()
            out.append({"path": r["path"], "synced_at": r["synced_at"], "files": files, "chunks": chunks, "errors": errors})
        return out

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


def _walk(root: Path) -> Iterator[tuple[Path, os.stat_result]]:
    exts = supported_extensions()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
        for name in filenames:
            if name.startswith("~$") or os.path.splitext(name)[1].lower() not in exts:
                continue  # ~$ = Office lock file
            path = Path(dirpath, name)
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_size <= MAX_FILE_BYTES:
                yield path, st


_RETRY = "retry"


def _extract_job(path: str) -> tuple[list[Chunk], str | None]:
    try:
        return extract(Path(path)), None
    except Exception as e:  # one unreadable file must not stop the sync
        return [], f"{type(e).__name__}: {e}"


def _extract_all(todo: list[tuple], workers: int | None) -> Iterator[tuple]:
    pooled = [item for item in todo if not is_serial(item[1])]
    serial = [item for item in todo if is_serial(item[1])]
    if len(pooled) < PARALLEL_THRESHOLD or workers == 1:
        serial, pooled = pooled + serial, []
    try:
        if pooled:
            workers = workers or min(8, os.cpu_count() or 1, -(-len(pooled) // FILES_PER_WORKER))
            with ProcessPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_extract_job, str(item[1])): item for item in pooled}
                for fut in as_completed(futures):
                    key, path, mtime, size = futures[fut]
                    try:
                        chunks, error = fut.result()
                    except BrokenProcessPool:
                        chunks, error = [], _RETRY
                    yield key, path, mtime, size, chunks, error
        for key, path, mtime, size in serial:
            yield key, path, mtime, size, *_extract_job(str(path))
    finally:
        release()
