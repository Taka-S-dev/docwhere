"""Folders and files the user keeps out of a folder's index.

A pattern is set on one folder and applies below it, whichever folder is being indexed: a
search in a subfolder or a parent folder leaves the same files out. Matching ignores case.

- A pattern without ``/`` is a name: it matches any folder or file of that name at any depth
  (``old``, ``*_bak*``, ``*.log``).
- A pattern with ``/`` is a path from the folder it is set on (``specs/archive``, ``*/draft``);
  everything inside a matching folder is left out.
"""

from __future__ import annotations

import os
from fnmatch import fnmatchcase


def normalize(pattern: str) -> str:
    """The stored form of a pattern: ``/`` separators, no leading ``./`` or surrounding ``/``."""
    p = pattern.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/")
    if not p or any(part in ("", ".", "..") for part in p.split("/")):
        raise ValueError(f"not a usable pattern: {pattern!r}")
    return p


def _matches(pattern: str, rel: str) -> bool:
    parts = rel.split("/")
    if "/" not in pattern:
        return any(fnmatchcase(part, pattern) for part in parts)
    return any(fnmatchcase("/".join(parts[:n]), pattern) for n in range(1, len(parts) + 1))


class Matcher:
    """Tells which pattern, if any, leaves a path out. Paths are index keys (``path_key``)."""

    def __init__(self, rules: list[tuple[str, str]]) -> None:
        # rules: (key of the folder the pattern is set on, pattern)
        self.rules = [(_prefix(key), pattern.lower(), (key, pattern)) for key, pattern in rules]

    def __bool__(self) -> bool:
        return bool(self.rules)

    def sources(self) -> list[tuple[str, str]]:
        """Every (folder key, pattern) rule, as given."""
        return [rule for _, _, rule in self.rules]

    def match(self, key: str) -> tuple[str, str] | None:
        """The (folder key, pattern) rule that leaves ``key`` out, else None. A folder's own
        patterns never leave the folder itself out; a parent folder's can."""
        lowered = key.lower()
        for prefix, pattern, rule in self.rules:
            if lowered.startswith(prefix) and _matches(pattern, lowered[len(prefix):].replace(os.sep, "/")):
                return rule
        return None


def _prefix(key: str) -> str:
    key = key.lower()
    return key if key.endswith(os.sep) else key + os.sep
