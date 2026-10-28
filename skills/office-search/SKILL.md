---
name: office-search
description: Find which document in a folder tree covers a topic, and where (section, page, slide, cell), across PDF, Word, Excel, PowerPoint (including legacy .doc/.xls/.ppt) and text files, using the indexed `docwhere` CLI; then show that section or open the file at the spot. Use whenever the user asks which document describes something, where a spec / procedure / command / setting / error code is written, or wants to locate information inside a pile of documents — e.g. "このコマンドの仕様が書いてあるドキュメントどれ？", "〜について書いてある資料探して", "〜の手順はどこ？", "何章に書いてある？", "このエラーの対処法どこかに書いてない？", "その箇所開いて". Prefer this over office-read's office_find.py for anything beyond a handful of files, since docwhere keeps an index, ranks by section and knows which file is the newer version.
---

# office-search

`docwhere` is installed on PATH (source: the docwhere repo; `uv tool install -e <repo>`).
It indexes a folder once (later runs only re-read changed files), searches section by section,
and ranks sections whose **heading** names the term above pages that merely mention it.

## Workflow

1. **Decide the folder.** Use the folder the user named. If none, ask once; do not search
   the whole drive. Pass it with `--in` every time.

2. **Expand the question into 3–6 search terms, then search once with all of them.**
   Terms are ORed and a section matching several of them ranks higher, so give the variants
   a document might actually use:
   - the exact identifier (`config`, `set-timeout`, `E1234`)
   - Japanese and English wordings (`設定コマンド`, `configuration`)
   - spelling variants (`ログイン` / `ログオン`, `サーバ` / `サーバー`)
   - when the user describes a symptom rather than naming the thing, the likely causes too
     ("タイムアウトする" → `タイムアウト`, `timeout`, `プロキシ`, `proxy`)
   Full-width/half-width differences are already normalised; do not add those variants.

   ```
   docwhere search config 設定コマンド configuration --in "D:\docs" --json
   ```

   Add `--all` when every term must appear (narrowing), `--ext pdf,docx` to limit formats,
   `-n 20` for more results. The first search on a new folder builds the index and can take
   a while for thousands of files (legacy .doc/.xls/.ppt are converted through Office, about
   a second each); later searches take well under a second.

3. **Judge the hits, do not just relay them.** Each hit has:
   - `crumb` (heading path) and `location` (`p.12`, `slide 4`, `Sheet!D5`, `L40`; empty for
     Word, where the heading is the location)
   - `snippet` — for spreadsheets the whole row labelled by its column headers
     (`コード: E2041 / 対処: ...`), usually enough without reading further
   - `modified` (file date) and `newer_version` — set when another file in the folder is a
     newer version of the same document (`仕様書_v1.4.pdf` → `仕様書_v2.3.docx`). Such hits are
     ranked lower; treat their content as possibly outdated and prefer the newer file.

   A hit whose `title`/`crumb` names the subject is usually the specification; a hit that only
   mentions it in body text usually is not. When the top hits are ambiguous, read the section:

   ```
   docwhere show --id 412        # whole section, by the hit's "id" from the JSON
   docwhere show --id 412 -C 1   # plus the sections before and after
   ```

   Prefer `--id` over the hit number (`docwhere show 2`): the number refers to whichever
   search ran last, which may be another agent's. Read at most 3–5 sections. If nothing fits,
   search again with different wording before concluding the information is absent.

4. **Answer with the place, then the content.** Give file name, section heading (and
   page / slide / cell when present), a short quote supporting it, then the answer. Mention
   the runner-up document when two documents both cover it, and say which one is current
   when they disagree.

5. **Report what could not be searched.** The JSON's `unreadable` lists every file in the
   folder that docwhere could not read (scanned PDFs without text, password-protected or broken
   files) — not only new failures. When `unreadable_count` is above zero, end the answer with
   one line naming them (or the count, if many), because the answer could be in one of them.
   Never say that every file was searched when this list is not empty.

6. **Open only when asked** ("開いて", "そこ見せて"): `docwhere open N` opens hit N of the
   *last* search at its spot — Acrobat at the page, Word with the paragraph selected, Excel
   with the cell selected, PowerPoint at the slide, VS Code at the line. If you ran another
   search since, search again first so the numbers match.

## Limits

- Scanned PDFs are not OCR'd; they appear in `unreadable`.
- Word has no page numbers in the file; the section heading is the location. Auto-numbered
  headings get a best-effort `3.2.1` prefix that may not match custom numbering like 第3章.
- PDFs without bookmarks get headings guessed from font size and boldness; in unusually
  formatted PDFs the crumb may be missing or approximate, so read the section when it matters.
- `--no-update` skips the change scan when searching the same folder repeatedly in one task.
