"""Measure how well docwhere's search finds known answers in the evaluation folder.

    uv run python skills/office-search/evals/bench.py <work-dir> [--noise N] [--json OUT]

Builds the folder of make_corpus.py under <work-dir> (with N unrelated documents, 1000 by
default) the first time, adds two unrelated files that share a name, indexes it into an
index of its own, then runs each query below as the agent would (``docwhere search --json``)
and reports:

- hit@1 / hit@5: the share of queries whose answer is the first hit / among the first five
- the time each search took (the index is up to date, so this is the search alone)
- old version first: queries where the outdated spec ranks above the current answer
- false "newer version": hits marked as superseded by a file that is not a version of them
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent

# terms as an agent would choose them; answers as (file name, words of the section's heading
# or location), any of which counts
QUERIES = [
    ("proxy", ["プロキシ", "HTTPS_PROXY"], [("OrbitSync_CLI仕様書_v2.3.docx", "2.2"), ("社内ネットワーク接続ガイド.pdf", "2.2")]),
    ("timeout default", ["タイムアウト", "既定", "net.timeout"], [("OrbitSync_CLI仕様書_v2.3.docx", "2.2")]),
    ("remote limit", ["リモート", "件まで", "上限"], [("OrbitSync_CLI仕様書_v2.3.docx", "2.3")]),
    ("passphrase", ["パスフレーズ"], [("OrbitSync_CLI仕様書_v2.3.docx", "2.4")]),
    ("purge default", ["purge", "既定値", "保存期間"], [("OrbitSync_CLI仕様書_v2.3.docx", "3.3")]),
    ("clean renamed", ["clean", "改名"], [("OrbitSync_CLI仕様書_v2.3.docx", "3.3")]),
    ("E2041", ["E2041"], [("障害対応手順_2026.pptx", "E2041"), ("エラーコード一覧.xlsx", "")]),
    ("power loss", ["電源断", "再起動", "破損"], [("月次運用手順書.docx", "4")]),
    ("generations (2 letters)", ["世代"], [("月次運用手順書.docx", "2"), ("OrbitSync_CLI仕様書_v2.3.docx", "3.3")]),
    ("claim deadline (2 letters)", ["申請", "期限"], [("経費精算マニュアル.docx", "1")]),
    ("same-named file", ["設計レビュー", "案件A"], [("設計書.docx", "")]),
]
OLD = "OrbitSync_CLI仕様書_v1.4.pdf"


def build(root: Path, noise: int) -> None:
    sys.path.insert(0, str(HERE))
    import make_corpus

    make_corpus.main(root)
    if noise:
        make_corpus.noise(root, noise)
    # two unrelated files of one name in two folders: neither is a version of the other
    from docx import Document

    for project, text in (("案件A", "案件A の設計レビューは 10 月に実施する。"), ("案件B", "案件B の見積は 11 月に提出する。")):
        doc = Document()
        doc.add_heading(f"{project} 設計書", 0)
        doc.add_paragraph(text)
        (root / project).mkdir(parents=True, exist_ok=True)
        doc.save(root / project / "設計書.docx")
    os.utime(root / "案件B" / "設計書.docx", (time.time(), time.time()))
    os.utime(root / "案件A" / "設計書.docx", (time.time() - 86400, time.time() - 86400))
    (root / ".built").write_text(str(noise), encoding="utf-8")


def run(argv: list[str]) -> tuple[int, str]:
    from docwhere.cli import main

    # main sets the streams to UTF-8 itself, which a StringIO cannot take
    out, err = (io.TextIOWrapper(io.BytesIO(), encoding="utf-8") for _ in range(2))
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(argv)
    out.flush()
    return code, out.buffer.getvalue().decode("utf-8")


def matches(hit: dict, answers: list[tuple[str, str]]) -> bool:
    where = " ".join(str(hit.get(k) or "") for k in ("crumb", "title", "location"))
    return any(Path(hit["path"]).name == name and words in where for name, words in answers)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("work", type=Path)
    ap.add_argument("--noise", type=int, default=1000)
    ap.add_argument("--json", type=Path, help="also write the results here")
    args = ap.parse_args()

    root = (args.work / "corpus").resolve()
    os.environ["DOCWHERE_DB"] = str((args.work / "index" / "index.db").resolve())
    if not (root / ".built").exists():
        print(f"building {root} ...", file=sys.stderr)
        build(root, args.noise)
    code, _ = run(["index", str(root)])
    if code:
        print("indexing failed", file=sys.stderr)
        return 1

    rows = []
    for name, terms, answers in QUERIES:
        start = time.perf_counter()
        code, out = run(["search", *terms, "--in", str(root), "--json", "-n", "10", "--no-update"])
        ms = (time.perf_counter() - start) * 1000
        hits = json.loads(out)["hits"] if code == 0 else []
        rank = next((i + 1 for i, h in enumerate(hits) if matches(h, answers)), None)
        old = next((i + 1 for i, h in enumerate(hits) if Path(h["path"]).name == OLD), None)
        false_newer = [
            h["path"] for h in hits
            if h["newer_version"] and Path(h["newer_version"]).name == Path(h["path"]).name
            and Path(h["newer_version"]).parent != Path(h["path"]).parent
        ]
        rows.append({"query": name, "rank": rank, "ms": round(ms, 1), "old_first": bool(old and (rank is None or old < rank)),
                     "false_newer": len(false_newer)})

    n = len(rows)
    summary = {
        "queries": n,
        "hit@1": round(sum(r["rank"] == 1 for r in rows) / n, 2),
        "hit@5": round(sum(bool(r["rank"]) and r["rank"] <= 5 for r in rows) / n, 2),
        "median_ms": round(statistics.median(r["ms"] for r in rows), 1),
        "max_ms": max(r["ms"] for r in rows),
        "old_version_first": sum(r["old_first"] for r in rows),
        "false_newer_version": sum(r["false_newer"] for r in rows),
    }
    width = max(len(r["query"]) for r in rows)
    for r in rows:
        flags = (" OLD-FIRST" if r["old_first"] else "") + (f" FALSE-NEWER x{r['false_newer']}" if r["false_newer"] else "")
        print(f"{r['query']:<{width}}  rank {r['rank'] or '-':>2}  {r['ms']:>7.1f} ms{flags}")
    print(json.dumps(summary, ensure_ascii=False))
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "queries": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
