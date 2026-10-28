# office-search evals

Test prompts for the office-search skill, run against a generated document folder for a
fictional CLI tool (OrbitSync).

| File | Purpose |
|---|---|
| `evals.json` | prompts, expected answers and assertions; `{corpus}` stands for the generated folder |
| `make_corpus.py` | builds the document folder |
| `grade.py` | keyword-checks each run's `answer.md` against the assertions and writes `grading.json` |

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
