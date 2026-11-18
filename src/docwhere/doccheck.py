"""docwhere doc-check: .doc files read both ways, without Word (extractors.msdoc) and by Word,
and compared, so that reading them without Word is turned on only where it reads them as Word.

Each file is read in a worker that is stopped, Word with it, when it takes too long, as an
index does. Only counts, file paths and section numbers are reported unless ``show`` asks for
the lines that differ.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SAMPLE = 50
TIMEOUT = 120  # seconds Word gets for one file before it is stopped


def check_one(path: str) -> dict:
    """Runs in a worker: the file read both ways. {fast, reason, fast_s, word, word_error, word_s}"""
    from docwhere.extractors import msdoc
    from docwhere.extractors.legacy import doc_through_word

    out: dict = {"fast": None, "reason": None, "word": None, "word_error": None}
    started = time.perf_counter()
    try:
        out["fast"] = _flat(msdoc.extract(Path(path)))
    except msdoc.Unsupported as e:
        out["reason"] = str(e)
    except Exception as e:  # noqa: BLE001 - a fault in the reader is a finding, not the end of the check
        out["reason"] = f"reader fault: {type(e).__name__}: {e}"
    out["fast_s"] = time.perf_counter() - started
    started = time.perf_counter()
    try:
        out["word"] = _flat(doc_through_word(Path(path)))
    except Exception as e:  # noqa: BLE001
        out["word_error"] = f"{type(e).__name__}: {e}"
    out["word_s"] = time.perf_counter() - started
    return out


def _flat(chunks) -> list:
    return [[c.title, c.crumb, list(c.lines)] for c in chunks]


@dataclass
class Report:
    total: int = 0  # .doc files found
    checked: int = 0
    same: int = 0
    to_word: Counter = field(default_factory=Counter)  # reason -> files the reader leaves to Word
    differ: list = field(default_factory=list)  # {path, section, title, fast, word}
    word_failed: list = field(default_factory=list)  # {path, error}
    fast_seconds: float = 0.0
    word_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {"total": self.total, "checked": self.checked, "same": self.same, "to_word": dict(self.to_word),
                "differ": [{k: d[k] for k in ("path", "section", "title")} for d in self.differ],
                "word_failed": self.word_failed,
                "seconds": {"without_word": round(self.fast_seconds, 2), "by_word": round(self.word_seconds, 2)}}


def doc_files(folders: list[Path]) -> list[Path]:
    """The .doc files under the folders, Word's own lock files (~$name.doc) left out."""
    from docwhere.index import MAX_FILE_BYTES

    found = []
    for folder in folders:
        for top, _, names in os.walk(folder):
            found += [Path(top, n) for n in names if n.lower().endswith(".doc") and not n.startswith("~$")]
    return sorted(p for p in found if _size(p) <= MAX_FILE_BYTES)  # as an index: larger files are not read


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def sample(paths: list[Path], n: int) -> list[Path]:
    """At most ``n`` of the paths, spread over the whole list (0: all)."""
    if not n or len(paths) <= n:
        return list(paths)
    return [paths[i * len(paths) // n] for i in range(n)]


def check(paths: list[Path], total: int, progress=None) -> Report:
    from docwhere import workers

    report = Report(total=total)
    tasks = [workers.Task(str(p), serial=True) for p in paths]
    for out in workers.run(tasks, check_one, workers=1, timeout=TIMEOUT):
        report.checked += 1
        if progress:
            progress(report.checked, len(paths))
        path = out.task.path
        if out.failure:
            report.word_failed.append({"path": path, "error": out.failure})
            continue
        result = out.result
        report.fast_seconds += result["fast_s"]
        report.word_seconds += result["word_s"]
        if result["word_error"]:
            report.word_failed.append({"path": path, "error": result["word_error"]})
        elif result["fast"] is None:
            report.to_word[result["reason"]] += 1
        elif result["fast"] == result["word"]:
            report.same += 1
        else:
            report.differ.append(_first_difference(path, result["fast"], result["word"]))
    return report


def _first_difference(path: str, fast: list, word: list) -> dict:
    for i, (a, b) in enumerate(zip(fast, word)):
        if a != b:
            line = next((j for j, (x, y) in enumerate(zip(a[2], b[2])) if x != y), min(len(a[2]), len(b[2])))
            return {"path": path, "section": i, "title": b[0],
                    "fast": a[2][line] if line < len(a[2]) else None, "word": b[2][line] if line < len(b[2]) else None}
    i = min(len(fast), len(word))
    return {"path": path, "section": i, "title": (word[i] if i < len(word) else fast[i])[0],
            "fast": None, "word": None}
