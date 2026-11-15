"""Measure the ranking on any index with questions made from it, no hand-written answers needed.

    uv run python skills/office-search/evals/known_items.py <index.db> [--n 300] [--seed 1] [--misses 5]

Each question is a section picked at random: a few words of its text that are not in its
headings and are found in only a few other sections (a word in that section alone would put it
first whatever the ranking), searched as the agent searches (the first ten hits, three a file).
The answer is that section. The index is copied first; the original is not opened.

These questions use the document's own words, so they are easier than a person's, who may word
a question otherwise: the figures compare one ranking with another, they are not how often a
real question is answered.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

# words without a dictionary: a run of katakana, of kanji, or of letters and digits
WORD = re.compile(r"[ァ-ヴー]{3,}|[一-龠々]{2,6}|[A-Za-z][A-Za-z0-9_.\-]{2,}")
MIN_BODY = 80  # characters: a shorter section has too few words of its own to ask about
TERMS = 3
RARE = (2, 20)  # sections a question word may be found in: in more, it points nowhere in particular
LIMIT, PER_FILE = 10, 3  # as the agent searches (docwhere search's defaults)


def words(text: str) -> set[str]:
    return {w.lower() for w in WORD.findall(text)}


def make_questions(rows: list, n: int, seed: int) -> list[dict]:
    """[{"id", "terms", "title"}, ...]: up to ``n`` questions from ``rows`` (id, title, crumb, body)."""
    seen = [words(r["body"]) for r in rows]
    df = Counter(w for ws in seen for w in ws)
    rng = random.Random(seed)
    order = list(range(len(rows)))
    rng.shuffle(order)
    questions = []
    for i in order:
        row = rows[i]
        if len(row["body"]) < MIN_BODY:
            continue
        heading = words(f"{row['title']} {row['crumb']}")
        pool = sorted((w for w in seen[i] if w not in heading and RARE[0] <= df[w] <= RARE[1]), key=lambda w: (df[w], w))
        if len(pool) < 2:
            continue
        terms = rng.sample(pool[: TERMS * 3], min(TERMS, len(pool[: TERMS * 3])))
        questions.append({"id": row["id"], "terms": terms, "title": row["crumb"] or row["title"]})
        if len(questions) >= n:
            break
    return questions


def evaluate(index, questions: list[dict]) -> dict:
    """Where each question's section ranked, and the figures over all of them."""
    from docwhere.search import search

    roots = [Path(r["path"]) for r in index.roots()]
    ranks, misses = [], []
    for q in questions:
        hits = search(index, roots, q["terms"], limit=LIMIT, per_file=PER_FILE)
        rank = next((i + 1 for i, h in enumerate(hits) if h.chunk_id == q["id"]), None)
        ranks.append(rank)
        if rank != 1:
            misses.append({**q, "rank": rank, "first": (hits[0].crumb or hits[0].title) if hits else None})
    n = len(ranks) or 1
    return {
        "questions": len(ranks),
        "hit@1": round(sum(r == 1 for r in ranks) / n, 3),
        "hit@5": round(sum(bool(r) and r <= 5 for r in ranks) / n, 3),
        "hit@10": round(sum(bool(r) for r in ranks) / n, 3),
        "mrr": round(sum(1 / r for r in ranks if r) / n, 3),
        "misses": misses,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("db", type=Path, help="the index to ask; a copy of it is used")
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--misses", type=int, default=5, help="questions not answered first, shown as examples")
    ap.add_argument("--json", type=Path, help="also write the figures and every miss here")
    args = ap.parse_args()

    from docwhere.index import Index

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "index.db"
        for suffix in ("", "-wal", "-shm"):
            if Path(f"{args.db}{suffix}").exists():
                shutil.copy(f"{args.db}{suffix}", f"{copy}{suffix}")
        index = Index(copy)
        try:
            rows = index.db.execute("SELECT id, title, crumb, body FROM chunks ORDER BY id").fetchall()
            out = evaluate(index, make_questions(rows, args.n, args.seed))
        finally:
            index.close()
    figures = {k: v for k, v in out.items() if k != "misses"}
    print(json.dumps(figures, ensure_ascii=False))
    for m in out["misses"][: args.misses]:
        print(f"  rank {m['rank'] or '-':>2}  {' '.join(m['terms'])}  -> wanted: {m['title'][:40]} / first: {(m['first'] or '')[:40]}")
    if args.json:
        args.json.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
