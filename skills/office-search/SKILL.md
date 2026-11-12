---
name: office-search
description: Find which document in a folder tree covers a topic, and where (section, page, slide, cell), across PDF, Word, Excel, PowerPoint (including legacy .doc/.xls/.ppt) and text files, using the indexed `docwhere` CLI; then show that section or open the file at the spot. Use whenever the user asks which document describes something, where a spec / procedure / command / setting / error code is written, what a documented value or default is, asks to remember where something is written, or wants folders kept out of searches — e.g. "このコマンドの仕様が書いてあるドキュメントどれ？", "〜の手順はどこ？", "このタイムアウトの既定値は？", "その箇所開いて", "〜は〜に書いてあるって覚えておいて", "資料の地図作って", "旧版フォルダは検索に出さないで". Also searches an added team wiki (Confluence) ("wikiに書いてある？"). Prefer this over office-read's office_find.py for more than a handful of files.
---

# office-search

`docwhere` is installed on PATH (source: the docwhere repo; `uv tool install -e <repo>`).
It indexes a folder once (later runs only re-read changed files), searches section by section,
and ranks sections whose **heading** names the term above pages that merely mention it.

## Quick or thorough

Searching takes well under a second; the time goes into how many rounds you run and how many
sections you read. Pick one of two modes before starting:

| | quick (default) | thorough |
|---|---|---|
| when | "どこ？", "何秒？", "ざっと", "すぐ" — most questions | "ちゃんと", "漏れなく", "全部", "比較して", or the user is about to act on the answer |
| folders | the most likely kind from the map | every kind that could hold it |
| terms | 3–4, one search | 5–6, then search again with other wordings |
| reading | answer from snippets; read at most 1 section | read the top 3–5 sections |
| checks | a matching note that still holds is enough | newer versions, the runner-up document, disagreements |

Quick mode must not guess. Escalate to thorough on your own, or answer with an explicit
"確度は低いです（理由）。じっくり探しますか？", when:
- the top hits match only in body text, not in a heading or a table row naming the subject
- the top hits disagree with each other
- the value or procedure is not in any snippet and one section read did not settle it

## Workflow

1. **Decide the folder.** Read the notes file (see [The notes file](#the-notes-file)) and pick
   folders from its folder rows by subject and by kind of document:
   - how to do something, an operation, a procedure → マニュアル
   - the rules: command syntax, option and setting values, screen items, protocol, error codes → 仕様書
   - how it is built, why, module structure, internal flow → 設計書

   Kind is a priority, not a filter: documents overlap (a design document may carry the
   command spec). Search the most likely kind first; when its hits are weak, search the
   others before concluding. Pass several folders by repeating `--in`.

   A `wiki:NAME` (or `wiki:NAME/SPACE`) row is a folder row for the team wiki: when it fits,
   search it with `docwhere wiki search … --wiki NAME [--space SPACE]` alongside or instead of
   the local folders (read `references/wiki.md` first).

   If the user named a folder, use it. If there is no notes file or no row fits, ask once and
   offer to draft the map (below); do not search the whole drive.

   Also look at the file rows: one whose `note` or `section` fits the question is the first
   place to check: search with `--in` set to its folder and confirm the hit from that file
   still says so. A note is a hint, never the answer by itself.

2. **Expand the question into search terms, then search once with all of them.**
   Terms are ORed and a section matching several of them ranks higher, so give the variants
   a document might actually use:
   - the exact identifier (`config`, `set-timeout`, `E1234`)
   - Japanese and English wordings (`設定コマンド`, `configuration`)
   - spelling variants (`ログイン` / `ログオン`, `サーバ` / `サーバー`)
   - when the user describes a symptom rather than naming the thing, the likely causes too
     ("タイムアウトする" → `タイムアウト`, `timeout`, `プロキシ`, `proxy`)
   - for a value question, how documents label a default: `既定値`, `デフォルト`, `default`, `初期値`

   Search is literal, so word the terms the way a document **states the answer**, not the way
   the question asks it. Documents rarely write "最大登録数は N"; they write "N 件まで登録できる":
   - a limit ("最大いくつ？", "上限は？") → the counter with まで (`件まで`, `人まで`, `台まで`,
     `個まで`, `文字まで`) and the verb phrase (`登録できる`, `指定できる`, `設定できる`), plus `上限`
   - a minimum ("何文字以上？") → `字以上`, `文字以上`, `以上必要`
   - whether something is allowed ("〜していい？", "大丈夫？") → `できる`, `できない`, `禁止`, `不可`,
     and what the document would call the situation (シャットダウン → `電源`, `再起動`;
     壊れる → `破損`)
   - a renamed or colloquial thing → its formal names (パスワード → `パスフレーズ`, `暗号化キー`)

   Do not pass a bare short word that appears everywhere (`まで`, `最大`, `設定`): it matches
   nearly every section and buries the answer. Attach it to the subject or a counter instead.
   When the first search returns only sections whose heading names the subject but no snippet
   with the answer, search again with `--all` on the subject plus one answer phrase
   (`docwhere search リモート 件まで --all`).

   Full-width/half-width differences are already normalised; do not add those variants.

   ```
   docwhere search config 設定コマンド configuration --in "D:\docs\仕様書" --json --keep-changed
   ```

   Pass `--keep-changed` until the user decides otherwise (see "Files changed since they
   were read" below).

   Add `--all` when every term must appear (narrowing), `--ext pdf,docx` to limit formats,
   `-n 20` for more results. Later searches take well under a second.

   **A folder not read yet.** The first search on a new folder (or after many files changed)
   waits at most 20 seconds, then answers from the files read so far and leaves the rest to a
   background reader, which keeps going after the command returns (legacy .doc/.ppt are
   converted through Office, about a second each). The JSON's `reading` says how much the
   search saw: `complete` is true once every file was read; while it is false, `read` of
   `files` were searched, `remaining` are not yet, `remaining_types` tells which kinds and
   `about_seconds_left` how long reading should take. `reading` is always there; never take
   a missing field for a finished read. With `complete: false`:
   - Start or end the answer with 「<files> 件中 <read> 件を読んだ時点の答えです（残りは主に
     .doc、あと約◯分）」.
   - Nothing found: never say it is not written. Say the answer may be in the files not read
     yet, and ask 「全件で探すなら、読み終わるのを待って探し直します（あと約◯分）。待ちますか？」.
   - Thorough mode, or the user asked to search everything ("全部", "漏れなく"): ask the same
     before answering.
   - The user agrees: search again with `--wait-all`, which waits until every file is read
     (stderr gets a progress line every 10 seconds). If your shell's time limit is shorter
     than the wait, do not wait: tell the user how long it will take and to ask again then;
     the reader keeps going meanwhile and nothing read is lost.
   `docwhere status` shows the reader's progress, `docwhere index --stop` stops it (what it
   read is kept; the next search continues). A file that takes over 3 minutes is given up
   and listed in `unreadable`; the rest is still read.

   **Files changed since they were read.** A document edited since docwhere read it often
   still says the same thing on the question asked, so do not make the user wait for it to be
   read again without asking. With `--keep-changed`, such files are searched as last read
   (new files are still read). The JSON's `changed` has `count`, `kept` and the first `files`;
   a hit from such a file has `changed_since_read: true`.
   - `changed.kept` is true and you have not asked yet in this conversation: answer from what
     was read, and end with one question: 「前回読んだ後に更新されたファイルが <count> 件あります
     （<names, up to 3>）。読み直して探し直しますか？」. An answer that rests on a hit with
     `changed_since_read` says so: 「（<file> は更新前の内容です）」.
   - The user says yes: search again without `--keep-changed`, and leave it off for the rest
     of this conversation. No: keep passing it and do not ask again in this conversation.
   - Ask once per conversation; a new conversation asks again.

3. **Judge the hits, do not just relay them.** Each hit has:
   - `crumb` (heading path) and `location` (`p.12`, `slide 4`, `Sheet!D5`, `L40`; empty for
     Word, where the heading is the location)
   - `snippet` — a table row comes labelled by its column headers
     (`項目: 接続タイムアウト / 既定値: 30秒`, `コード: E2041 / 対処: ...`) in Excel, Word and
     PowerPoint alike, so a value question is often answered by the snippet alone
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
   search ran last, which may be another agent's. If nothing fits, search again with different
   wording before concluding the information is absent.

   When a PDF hit's answer sits in a table, figure or boxed note whose extracted text reads
   jumbled, look at the page itself: `docwhere snap --id 412 --term タイムアウト` saves the page
   as a PNG with the term highlighted and prints its path; read that image.

4. **Answer.**
   - A value question: the value first, then where it is written — "30秒です（通信仕様_v2.docx
     3.2 通信パラメータ）". When several values fit (接続タイムアウト and 応答タイムアウト), give
     each with its name rather than picking one. When an older version says otherwise, give
     the current value and say the old one differs.
   - A "where" question: file name, section heading (and page / slide / cell when present),
     a short quote supporting it, then the answer. Mention the runner-up document when two
     documents both cover it, and say which one is current when they disagree.
   - In quick mode keep it to a few lines.

5. **Report what could not be searched.** The JSON's `unreadable` lists every file in the
   folder that docwhere could not read (scanned PDFs without text, password-protected or broken
   files, files given up after taking too long) — not only new failures. When `unreadable_count` is above zero, end the answer with
   one line naming them (or the count, if many), because the answer could be in one of them.
   Never say that every file was searched when this list is not empty.
   Likewise `excludes`: the patterns that applied to the searched folders, each with
   `left_out`, the folders and files it kept out of this search (see `references/exclude.md`).
   When any `left_out` is above zero, end the answer with one line saying so —
   「除外中：旧版（3 か所）、*_bak*（1 件）は検索していません」 — because the answer could be
   in one of them, and the user may have forgotten the pattern. When nothing fits, offer to
   search them by removing the pattern. When `excluded_roots` is not empty, the folder the
   user asked about is itself excluded: say so first, before any answer.

6. **Offer to remember a hard find.** When the answer took re-searching, or sits somewhere
   its wording would not lead (a different term, an appendix, a document of another kind),
   ask in one line whether to add it to the notes file. Do not offer after an easy hit.

7. **Show the user only when asked.**
   - To look over the candidates ("見せて", "並べて", "パパっと確認したい", "抜粋見たい"),
     to mark the answer on the page, or to show reasoning across pages or a figure: first read
     `references/view.md` (next to this file), then run `docwhere view` or `search --view` as
     it says.
   - To jump into one document ("開いて", "そこ開いて"): `docwhere open N` opens hit N of the
     *last* search at its spot — Acrobat at the page, Word with the paragraph selected, Excel
     with the cell selected, PowerPoint at the slide, VS Code at the line.
   If you ran another search since, search again first so the numbers match.

## When something does not work on this PC

Office files stay unreadable, a view does not open in the window, background reading never
progresses, the user asks to check the setup ("動作確認して"), or asks why reading or searching
is slow ("何が遅い？", "インデックスに時間かかる"): read `references/troubleshooting.md` first.

## Leaving files out

To keep old versions, backups or drafts out ("旧版は検索に出さないで"), to say what is
excluded, or to undo it: read `references/exclude.md` first.

## The notes file

`notes.csv` next to the index: `%LOCALAPPDATA%\docwhere\notes.csv`, or the folder of
`DOCWHERE_DB` when that is set. The user edits it by hand (Excel works), and so do you.
UTF-8 with BOM, header `path,section,kind,note`:

```csv
path,section,kind,note
D:\proj\docs\仕様書\通信,,仕様書,通信 プロトコル コマンド
D:\proj\docs\仕様書\GUI,,仕様書,GUI 画面
D:\proj\docs\設計書,,設計書,
D:\proj\docs\マニュアル,,マニュアル,
D:\proj\docs\仕様書\通信\通信仕様_v2.docx,3.2 通信パラメータ,仕様書,タイムアウト 再送回数
```

- `path` — absolute. A folder makes a map row; a file makes a note on that file.
  `wiki:NAME` (or `wiki:NAME/SPACE`) makes a map row for a wiki added with `docwhere wiki add`.
- `section` — optional heading, `p.12`, `slide 4` or `Sheet!D5`.
- `kind` — マニュアル, 仕様書, 設計書, or whatever the user's own categories are; keep theirs.
- `note` — a few keywords or a short phrase. Brief is fine; it only has to point.

A line starting with `#` is a comment; the user may write there how their kinds differ
("設計書には画面遷移も含む"), and that overrides the defaults in step 1.

Before adding a row ("〜は〜に書いてあるって覚えておいて"), drafting the map ("地図作って", or
when there is none) or opening the file ("メモ開いて"): read `references/notes.md` first.

**Stale rows**: when a row's path no longer exists, its section is gone, or the file has a
`newer_version`, say so in the answer and offer to fix or drop the row. Never edit or delete
the user's rows silently.

## The team wiki

Before running any `docwhere wiki` command, read `references/wiki.md` (adding a wiki,
searching, showing a page, errors).

- When to search it: the notes map has a `wiki:` row whose kind or note fits the question, the
  user mentions the wiki, or the local search comes up short and a wiki is added. In thorough
  mode search both and merge.

## Limits

- Scanned PDFs are not OCR'd; they appear in `unreadable`.
- Word has no page numbers in the file; the section heading is the location. Auto-numbered
  headings get a best-effort `3.2.1` prefix that may not match custom numbering like 第3章.
- PDFs without bookmarks get headings guessed from font size and boldness; in unusually
  formatted PDFs the crumb may be missing or approximate, so read the section when it matters.
  PDF tables are read as laid-out text, so a row may not come out labelled by its headers.
- Wiki hits depend on the wiki's own search; docwhere ranks sections only within the pages it
  returns. Attachments on wiki pages and content filled in by macros are not read.
- `--no-update` skips the change scan when searching the same folder repeatedly in one task;
  `reading.complete` is then null (not checked), so do not use it while a folder is still
  being read.
