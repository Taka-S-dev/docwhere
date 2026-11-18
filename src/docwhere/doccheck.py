"""docwhere doc-check: .doc and .ppt files read both ways, without Office (extractors.msdoc,
extractors.msppt) and by Word or PowerPoint, and compared, so that reading them without Office
is turned on only where it reads them as Office.

Each file is read in a worker that is stopped, Office with it, when it takes too long, as an
index does. Only counts, file paths and section or slide numbers are reported unless ``show``
asks for the lines that differ.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SAMPLE = 50
TIMEOUT = 120  # seconds Office gets for one file before it is stopped
KINDS = (".doc", ".ppt")


def check_one(path: str) -> dict:
    """Runs in a worker: the file read both ways. {fast, reason, fast_s, office, office_error, office_s}"""
    from docwhere.extractors import msdoc, msppt
    from docwhere.extractors.legacy import doc_through_word, ppt_through_powerpoint

    is_ppt = path.lower().endswith(".ppt")
    reader, by_office = (msppt, ppt_through_powerpoint) if is_ppt else (msdoc, doc_through_word)
    out: dict = {"fast": None, "reason": None, "office": None, "office_error": None}
    started = time.perf_counter()
    try:
        out["fast"] = _flat(reader.extract(Path(path)))
    except msdoc.Unsupported as e:
        out["reason"] = str(e)
    except Exception as e:  # noqa: BLE001 - a fault in the reader is a finding, not the end of the check
        out["reason"] = f"reader fault: {type(e).__name__}: {e}"
    out["fast_s"] = time.perf_counter() - started
    started = time.perf_counter()
    try:
        out["office"] = _flat(by_office(Path(path)))
    except Exception as e:  # noqa: BLE001
        out["office_error"] = f"{type(e).__name__}: {e}"
    out["office_s"] = time.perf_counter() - started
    return out


def _flat(chunks) -> list:
    return [[c.title, c.crumb, list(c.lines)] for c in chunks]


@dataclass
class Report:
    total: int = 0  # .doc and .ppt files found
    checked: int = 0
    same: int = 0
    to_office: Counter = field(default_factory=Counter)  # reason -> files the reader leaves to Office
    differ: list = field(default_factory=list)  # {path, section, title, fast, office}
    office_failed: list = field(default_factory=list)  # {path, error}
    fast_seconds: float = 0.0
    office_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {"total": self.total, "checked": self.checked, "same": self.same, "to_office": dict(self.to_office),
                "differ": [{k: d[k] for k in ("path", "section", "title")} for d in self.differ],
                "office_failed": self.office_failed,
                "seconds": {"without_office": round(self.fast_seconds, 2), "by_office": round(self.office_seconds, 2)}}


def doc_files(folders: list[Path]) -> list[Path]:
    """The .doc and .ppt files under the folders, Office's own lock files (~$name.doc) left out."""
    from docwhere.index import MAX_FILE_BYTES

    found = []
    for folder in folders:
        for top, _, names in os.walk(folder):
            found += [Path(top, n) for n in names if n.lower().endswith(KINDS) and not n.startswith("~$")]
    return sorted(p for p in found if _size(p) <= MAX_FILE_BYTES)  # as an index: larger files are not read


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def sample(paths: list[Path], n: int) -> list[Path]:
    """At most ``n`` of the paths, spread over the whole list (0: all), each kind in proportion
    to how many of it there are, and every kind found at least once."""
    if not n or len(paths) <= n:
        return list(paths)
    groups = [[p for p in paths if p.suffix.lower() == kind] for kind in KINDS]
    groups = [g for g in groups if g]
    shares = [max(1, n * len(g) // len(paths)) for g in groups]
    shares[shares.index(max(shares))] -= sum(shares) - n
    picked = [g[i * len(g) // k] for g, k in zip(groups, shares) for i in range(k)]
    return sorted(picked)


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
            report.office_failed.append({"path": path, "error": out.failure})
            continue
        result = out.result
        report.fast_seconds += result["fast_s"]
        report.office_seconds += result["office_s"]
        if result["office_error"]:
            report.office_failed.append({"path": path, "error": result["office_error"]})
        elif result["fast"] is None:
            report.to_office[result["reason"]] += 1
        elif result["fast"] == result["office"] or result["fast"] == _without_today(result["office"]):
            report.same += 1
        else:
            report.differ.append(_first_difference(path, result["fast"], result["office"]))
    return report


def _without_today(chunks: list) -> list:
    """The chunks without the lines that are today's date: a slide's date that PowerPoint fills
    in with the day it opens the file, which the reader without Office leaves out."""
    return [[title, crumb, [line for line in lines if not _is_today(line)]] for title, crumb, lines in chunks]


def _is_today(line: str, today=None) -> bool:
    import datetime
    import re

    today = today or datetime.date.today()
    words = {str(today.year), f"{today.year % 100:02d}", str(today.month), f"{today.month:02d}", str(today.day),
             f"{today.day:02d}", today.strftime("%B"), today.strftime("%b"), today.strftime("%A"), today.strftime("%a"),
             "年", "月", "日"}
    weekday = "月火水木金土日"[today.weekday()]
    words |= {weekday, "曜"}
    tokens = re.findall(r"\d+|[A-Za-z]+|[^\W\d_]", line)
    numbers = {t.lstrip("0") for t in tokens if t.isdigit()}
    return (str(today.year) in tokens or f"{today.year % 100:02d}" in tokens) and str(today.day) in numbers and all(
        t in words or t.lstrip("0") in words for t in tokens)


def _first_difference(path: str, fast: list, office: list) -> dict:
    for i, (a, b) in enumerate(zip(fast, office)):
        if a != b:
            line = next((j for j, (x, y) in enumerate(zip(a[2], b[2])) if x != y), min(len(a[2]), len(b[2])))
            return {"path": path, "section": i, "title": b[0],
                    "fast": a[2][line] if line < len(a[2]) else None,
                    "office": b[2][line] if line < len(b[2]) else None}
    i = min(len(fast), len(office))
    return {"path": path, "section": i, "title": (office[i] if i < len(office) else fast[i])[0],
            "fast": None, "office": None}
