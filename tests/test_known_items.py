"""The ranking measure made from an index itself (skills/office-search/evals/known_items.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "skills" / "office-search" / "evals"))

import known_items


def _rows():
    filler = "これは説明のための文章で、長さを足すために書いてある。" * 3
    rows = [{"id": 1, "title": "サラダ", "crumb": "", "body": f"キャベツ ニンジン {filler}"},
            {"id": 2, "title": "スープ", "crumb": "", "body": f"キャベツ タマネギ {filler}"},
            {"id": 3, "title": "炒め物", "crumb": "", "body": f"ニンジン タマネギ ドレッシング {filler}"},
            {"id": 4, "title": "短い", "crumb": "", "body": "キャベツ"}]
    return rows


def test_questions_use_words_of_the_text_found_in_a_few_sections_not_the_heading():
    rows = _rows()
    questions = known_items.make_questions(rows, 10, seed=1)
    assert questions == known_items.make_questions(rows, 10, seed=1)  # the same questions each time
    assert {q["id"] for q in questions} <= {1, 2, 3}  # a section too short to ask about is left out
    for q in questions:
        row = next(r for r in rows if r["id"] == q["id"])
        assert len(q["terms"]) >= 2
        assert all(t in row["body"].lower() and t not in row["title"].lower() for t in q["terms"])
        assert "ドレッシング" not in q["terms"]  # in one section only: it would find it whatever the ranking


def test_each_question_is_ranked_as_the_agent_searches(docs, index):
    index.sync(docs)
    chunk = index.db.execute("SELECT id FROM chunks WHERE crumb = ?", ("コマンド > config コマンド",)).fetchone()[0]
    out = known_items.evaluate(index, [{"id": chunk, "terms": ["config"], "title": "config"},
                                       {"id": 999999, "terms": ["config"], "title": "gone"}])
    assert out["questions"] == 2 and out["hit@1"] == 0.5 and out["hit@10"] == 0.5
    assert [m["title"] for m in out["misses"]] == ["gone"]
