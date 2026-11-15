# office-search evals

Test prompts for the office-search skill, run against a generated document folder for a
fictional CLI tool (OrbitSync).

| File | Purpose |
|---|---|
| `evals.json` | prompts, expected answers and assertions; `{corpus}` stands for the generated folder |
| `make_corpus.py` | builds the document folder |
| `grade.py` | keyword-checks each run's `answer.md` against the assertions and writes `grading.json` |
| `bench.py` | measures the search alone on the same folder: hit@1 / hit@5, search time, outdated versions ranked first, files wrongly marked as superseded |
| `known_items.py` | measures the ranking on any index with questions made from it: a section's own words in, that section expected out |

## Measuring the search

```
uv run python skills/office-search/evals/bench.py <work-dir> [--noise 1000] [--json out.json]
```

The first run builds the folder (without the legacy `.doc`, so Word is not needed) and an
index of its own under `<work-dir>`; later runs reuse both. Compare the summary line before
and after a change to the search.

```
uv run python skills/office-search/evals/known_items.py <index.db> [--n 300] [--seed 1]
```

Works on any index, the user's own included, without hand-written answers: each question is a
few words of one section that are not in its headings and are found in a few other sections,
searched as the agent searches, the answer being that section. It copies the index first and
prints the share of questions answered first, within five and within ten. The questions use the
documents' own words, so they are easier than real ones: compare a ranking before and after a
change with the same seed, rather than read the figures as how often a real question is
answered. Questions made from a user's documents quote them; keep their output out of git.

## Reproduce

```
uv run python skills/office-search/evals/make_corpus.py <corpus-dir> 300
```

- With the trailing number, the folder also gets a legacy `.doc` (needs Word), a PDF without
  bookmarks and that many unrelated documents. Without it, only the ten core files are built.
- The folder deliberately contains an outdated spec, an ops manual that only references the
  spec, and a scanned PDF with no text layer.

Replace `{corpus}` in the prompts with the folder path, run each prompt with and without the
skill (skill-creator's workflow), save each answer as
`<workspace>/iteration-N/eval-<id>-<name>/<with_skill|without_skill>/run-1/outputs/answer.md`,
then grade:

```
python -X utf8 skills/office-search/evals/grade.py <workspace>/iteration-N
```

The keyword checks approximate judgement calls (for example, "does not present the ops manual
as the spec"); read the answers when a result looks off.

Keep the workspace (`skills/office-search-workspace/`) out of git; it holds generated
documents and run transcripts.
