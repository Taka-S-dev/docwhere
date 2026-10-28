"""Keyword grader for office-search evals: writes grading.json next to each run's outputs.

Each assertion maps to a predicate over answer.md. Judgement calls (e.g. "does not present
the ops manual as the spec") are approximated and should be spot-checked by reading.
"""

import json
import re
import sys
from pathlib import Path


def has(*words):
    return lambda t: all(w in t for w in words)


def any_of(*words):
    return lambda t: any(w in t for w in words)


def both(*preds):
    return lambda t: all(p(t) for p in preds)


CHECKS = {
    "purge-spec": [
        has("v2.3"),
        any_of("3.3"),
        has("90"),
        has("--dry-run", "--target", "default"),
        both(has("30"), any_of("旧", "古", "outdated")),
        lambda t: not re.search(r"月次運用手順書[^\n]{0,40}(に仕様|が仕様)", t),
        any_of("scan_設計メモ", "スキャン"),
    ],
    "error-E2041": [
        has("エラーコード一覧.xlsx"),
        any_of("A3", "D3", "3 行目", "3行目", "E2041 の行", "E2041 行"),
        any_of("スライド 2", "スライド2", "slide 2"),
        has("login --refresh"),
        any_of("SSO", "トークンキャッシュ"),
    ],
    "proxy-timeout": [
        has("2.2", "ネットワーク設定"),
        has("HTTPS_PROXY"),
        both(has("net.timeout"), any_of("30 秒", "30秒", "既定 30", "既定30")),
        both(has("接続ガイド"), any_of("p.3", "3ページ", "3 ページ", "2.2 コマンドライン", "コマンドラインツール")),
        has("E3002"),
    ],
    "log-location": [
        has("ログ運用手順.doc"),
        any_of("3 ログの保存先", "3. ログの保存先", "「3 ログ", "ログの保存先と保存期間"),
        has("ProgramData\\OrbitSync\\logs"),
        any_of("14 日", "14日"),
        has("log.dir"),
    ],
    "bandwidth": [
        has("性能チューニングガイド"),
        any_of("帯域制限", "p.2", "2 ページ", "2ページ"),
        both(has("bandwidth_limit", "Mbps"), any_of("無制限", "既定は 0", "既定 0", "既定値 0")),
        has("bandwidth_schedule"),
    ],
}


def main(iteration: Path) -> None:
    for eval_dir in sorted(iteration.glob("eval-*")):
        meta = json.loads((eval_dir / "eval_metadata.json").read_text(encoding="utf-8"))
        checks = CHECKS[meta["eval_name"]]
        for run in sorted(eval_dir.glob("*/run-*")):
            text = (run / "outputs" / "answer.md").read_text(encoding="utf-8")
            exps = [{"text": a, "passed": bool(c(text)), "evidence": "keyword check on answer.md"}
                    for a, c in zip(meta["assertions"], checks)]
            n = sum(e["passed"] for e in exps)
            timing = json.loads((run / "timing.json").read_text()) if (run / "timing.json").exists() else {}
            (run / "grading.json").write_text(json.dumps({
                "expectations": exps,
                "summary": {"passed": n, "failed": len(exps) - n, "total": len(exps), "pass_rate": round(n / len(exps), 2)},
                "timing": timing,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"{eval_dir.name:24} {run.parent.name:14} {n}/{len(exps)}", [i for i, e in enumerate(exps) if not e["passed"]])


if __name__ == "__main__":
    main(Path(sys.argv[1]))
