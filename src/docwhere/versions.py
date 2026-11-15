"""Recognise files that are versions of one another from their names.

"仕様書_v1.4.pdf", "仕様書_v2.3.docx", "仕様書_最終版.docx" and "仕様書 - コピー.docx" share
the family key "仕様書". Dates are deliberately not treated as versions: "議事録_20260915" and
"議事録_20261015" are different meetings, not revisions of one document.
"""

from __future__ import annotations

import re
from pathlib import Path

from docwhere.model import norm

_VERSION = re.compile(r"(?:^|(?<=[\s_\-(\[]))(?:v|ver\.?|version|rev\.?|r)\s?(\d+(?:\.\d+)*)(?![\d.])", re.IGNORECASE)
_EDITION = re.compile(r"第\s?(\d+)\s?版|改訂\s?(\d+)|改版\s?(\d+)")
_MARKERS = re.compile(
    r"最終版?|最新版?|確定版?|修正版?|改訂版?|旧版|ドラフト|draft|final|latest|fixed|old|のコピー|コピー|copy|\(\d+\)",
    re.IGNORECASE,
)
_SEPARATORS = re.compile(r"[\s_\-.()\[\]]+")
MIN_KEY_CHARS = 2


def family(path: str) -> tuple[str, tuple[int, ...]]:
    """(family key, version numbers or () when the name carries none)."""
    stem = norm(Path(path).stem).lower()
    version: tuple[int, ...] = ()
    if m := _VERSION.search(stem):
        version = tuple(int(x) for x in m.group(1).split("."))
        stem = stem[: m.start()] + " " + stem[m.end() :]
    elif m := _EDITION.search(stem):
        version = (int(next(g for g in m.groups() if g)),)
        stem = stem[: m.start()] + " " + stem[m.end() :]
    key = _SEPARATORS.sub("", _MARKERS.sub(" ", stem))
    return (key if len(key) >= MIN_KEY_CHARS else norm(Path(path).stem).lower()), version


def marked(path: str) -> bool:
    """Whether the name says it is one version of a document: a number (v1.4, 第2版) or a
    word (最終版, 旧版, コピー)."""
    stem = norm(Path(path).stem).lower()
    return bool(_VERSION.search(stem) or _EDITION.search(stem) or _MARKERS.search(stem))


def newer_versions(files: list[tuple[str, int]]) -> dict[str, str]:
    """Map each superseded path to the newest path of its family.

    ``files`` holds (path, mtime_ns). Files of one family are versions of one another when they
    lie in the same folder, or when both names are marked as versions (``marked``): two
    "設計書.docx" of two projects are different documents, while "旧資料/仕様書_v1.4.pdf" and
    "仕様書/仕様書_v2.3.docx" are one. Among versions the newest is the highest version when
    every one is numbered, else the most recently modified file.
    """
    families: dict[str, list[tuple[str, int, tuple[int, ...]]]] = {}
    for path, mtime in files:
        key, version = family(path)
        families.setdefault(key, []).append((path, mtime, version))

    newer: dict[str, str] = {}
    for members in families.values():
        if len(members) < 2:
            continue
        for group in _versions_of_one(members):
            if len(group) < 2:
                continue
            if all(version for _, _, version in group):
                newest = max(group, key=lambda m: (m[2], m[1]))
            else:
                newest = max(group, key=lambda m: m[1])
            for path, _, _ in group:
                if path != newest[0]:
                    newer[path] = newest[0]
    return newer


def _versions_of_one(members: list[tuple]) -> list[list[tuple]]:
    """Split a family into the files that are versions of one another: joined by a shared
    folder, or by both being marked as versions."""
    parent = list(range(len(members)))

    def top(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_in: dict[str, int] = {}
    first_marked = None
    for i, (path, _, _) in enumerate(members):
        folder = str(Path(path).parent).lower()
        if folder in first_in:
            parent[top(i)] = top(first_in[folder])
        else:
            first_in[folder] = i
        if marked(path):
            if first_marked is None:
                first_marked = i
            else:
                parent[top(i)] = top(first_marked)
    groups: dict[int, list[tuple]] = {}
    for i, member in enumerate(members):
        groups.setdefault(top(i), []).append(member)
    return list(groups.values())
