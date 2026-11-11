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
   the local folders (see [The team wiki](#the-team-wiki)).

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
   docwhere search config 設定コマンド configuration --in "D:\docs\仕様書" --json
   ```

   Add `--all` when every term must appear (narrowing), `--ext pdf,docx` to limit formats,
   `-n 20` for more results. Later searches take well under a second.

   **A folder not read yet.** The first search on a new folder (or after many files changed)
   waits at most 20 seconds, then answers from the files read so far and leaves the rest to a
   background reader, which keeps going after the command returns (legacy .doc/.xls/.ppt are
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
   `left_out`, the folders and files it kept out of this search (see Leaving files out).
   When any `left_out` is above zero, end the answer with one line saying so —
   「除外中：旧版（3 か所）、*_bak*（1 件）は検索していません」 — because the answer could be
   in one of them, and the user may have forgotten the pattern. When nothing fits, offer to
   search them by removing the pattern. When `excluded_roots` is not empty, the folder the
   user asked about is itself excluded: say so first, before any answer.

6. **Offer to remember a hard find.** When the answer took re-searching, or sits somewhere
   its wording would not lead (a different term, an appendix, a document of another kind),
   ask in one line whether to add it to the notes file. Do not offer after an easy hit.

7. **Show the user only when asked.**
   - To look over the candidates ("見せて", "並べて", "パパっと確認したい", "抜粋見たい"): run the
     same search again with `--view`. It opens one local HTML page in the browser with every
     hit's section (terms marked, the hit line flagged, neighbouring sections collapsed), PDF
     pages as images, and a link to the original. The page stays on this machine; it has no
     scripts and loads nothing from the network. `docwhere view 1 3` shows only some hits of
     the last search.
   - Once you know the answer, mark it: pass the sentence that states it, copied from the
     snippet or section, as `--quote "..."` (repeatable), and your one-line answer as
     `--answer "..."`. The quote gets an orange frame on the PDF page and bold marking in the
     text, the terms drop to an underline, and the answer heads the page. Without a quote
     every occurrence of a search term is highlighted equally, which buries the answer.
     Show only the hits that matter (`docwhere view 2 --quote ...`) rather than all of them.
     When the answer rests on several places (two pages, two documents), pass one `--quote`
     per place: each becomes its own piece of evidence with its source, page and close-up,
     found anywhere in the shown files, and its page is added to that file's pages. A quote
     that cannot be found is shown as such — fix its wording rather than leave it.
     Word and PowerPoint hits are shown as pages too (Word exports them to PDF on first view,
     a few seconds per document, then cached); their page numbers are those of the export.
     Excel hits are shown as the rows around the hit with the header row. For a table row,
     quote the snippet as given (`コード: E2041 / 対処: ...`); it is matched to the row.
   - The page opens in docwhere's own window (not the browser), which lets the user turn to
     any page (← →), follow the PDF's cross-references (Backspace goes back), open the file
     in Acrobat/Word/Excel at the spot, copy a citation and keep the window on top. One window
     serves every question: a later view replaces the page and raises the window. If the
     user prefers the browser, `docwhere viewer browser` (or `--browser` once).
   - Always pass the user's question as `--title "..."`. Each view is a new file that opens
     in a new browser tab, so earlier answers stay open while the user asks the next
     question; the title is what tells those tabs apart. Every page links to a history page
     listing the recent questions and answers. Old pages and snapshots are pruned
     automatically; run `docwhere clean` only when the user asks to clear them.
   - Add `-P 1` (or `-P 2`) for the PDF pages either side of each hit page when the user asks
     for them ("前後のページも"), or when the hit runs on: a table or procedure that continues
     over the page break, a hit at the very top or bottom of its page, or a note that says
     "次ページ参照". Keep the default otherwise; each extra page makes the file heavier.
     Pages are shown in page order; the page holding the `--quote` is always included, and
     the close-up and page number in the answer jump to it.
     `snap -P 1` saves the neighbouring pages for you to read in the same situations.
   - The `p.` number is the PDF's page index, which can differ from the number printed on
     the page (front matter shifts it). The viewer shows both ("p.120（印刷 118）") when the
     pages carry printed numbers; when you cite a page to the user, give both the same way.
     The user can also type a printed page number into the viewer to go there, open the PDF's
     contents, and click the section path above each page.
   - To jump into one document ("開いて", "そこ開いて"): `docwhere open N` opens hit N of the
     *last* search at its spot — Acrobat at the page, Word with the paragraph selected, Excel
     with the cell selected, PowerPoint at the slide, VS Code at the line.
   If you ran another search since, search again first so the numbers match.

## When something does not work on this PC

Run `docwhere doctor` when Office files stay unreadable, a view does not open in the window,
the background reading never seems to progress, or the user asks to check the setup
("動作確認して"). It prints one line per check (`ok` / `FAIL` / `--`); each Office check takes a few
seconds and is stopped after 60. Report each FAIL in plain words with what it means for searching. One
check, whether background reading survives the command, is only known on the second run:
when it says `--`, run it again after 10 seconds. Pass `--skill <the SKILL.md you were
loaded from>` to check that copy is current.

## Leaving files out

Old versions, backups and drafts slow the first index and push older answers into the hits.
The user can keep them out per folder; a pattern set on a folder applies below it, whichever
folder is searched, and is remembered in the index (nothing is written to the folder).

- A name matches a folder or file of that name at any depth: `旧版`, `*_bak*`, `*.log`,
  `*コピー*`. A pattern with `/` is a path from the folder: `設計/archive`. Case is ignored;
  write `*旧*` to match part of a name.
- To add, when the user asks ("旧版は検索に出さないで", "バックアップは除外して"): turn the
  request into patterns, tell the user the patterns and the folder in one line, then run
  `docwhere exclude "<folder>" <pattern>...`. Do not add patterns the user did not ask for;
  when old versions crowd the hits, suggest a pattern and wait for a yes. Already indexed
  files the pattern matches drop out at once.
- To see what is excluded ("何を除外してる？"): `docwhere status --json`; each folder's
  `excludes` lists `pattern` and `matched` (folders and files it left out the last time that
  folder itself was indexed or searched; `null` until then — the pattern still applies). Answer in plain words, e.g. 「設計フォルダでは『旧版』フォルダ
  （3 か所）と、名前に _bak が入るファイルを除外しています」. A pattern with `matched: 0`
  matches nothing — say so, it is probably mistyped.
- To undo ("さっきの除外戻して"): `docwhere exclude "<folder>" --remove <pattern>` (or `--clear`
  for all of that folder's patterns). The next search reads those files again, which can
  take a while for many Office files.
- `docwhere forget` drops a folder's patterns along with its index; `index --rebuild` keeps
  them.

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

**Registering** ("〜は〜に書いてあるって覚えておいて", "通信の仕様書はこのフォルダ"):
check that the path exists (and, for a section, that a search finds it there), then append
one row. Quote a field that contains a comma. Read the file back to check it still parses;
create it with the header if it does not exist. Tell the user the row you added.

**Drafting the map** ("地図作って", or when there is none): list the folder tree two or three
levels deep and sample file names (and, where names are vague, the headings from a search
or `docwhere show`) to infer each folder's subject and kind. Show the draft rows and write
them only after the user confirms or corrects them — a wrong kind sends later searches to the
wrong place. Do not add file rows when drafting; those come from real finds. For an added
wiki, `docwhere wiki spaces --json` lists its spaces with their names; draft one
`wiki:NAME/SPACE` row per space whose name says what it holds.

**Opening it** ("メモ開いて", "地図のファイル開いて"): create it first if missing (the header
line only, UTF-8 with BOM so Excel shows Japanese correctly), then open it with the default
app (`start "" "<path>"` / `os.startfile`), usually Excel. Mention that while Excel has it
open the file is locked, so rows cannot be added until it is closed.

**Stale rows**: when a row's path no longer exists, its section is gone, or the file has a
`newer_version`, say so in the answer and offer to fix or drop the row. Never edit or delete
the user's rows silently.

## The team wiki

`docwhere wiki` searches a wiki through the wiki's own search and reads only the pages it
returns; nothing is copied to disk except the last result list. It is the only part of
docwhere that uses the network, and only towards wikis the user added. Say "社内 wiki" (or the
name the user gave it) in answers, not the product name.

- Which wikis exist: `docwhere wiki list`. None → ask the user for the wiki's address (the
  top page they open in the browser) and a short name, then run
  `docwhere wiki add NAME URL [--space KEY]` yourself, followed by
  `docwhere wiki check NAME --term <a Japanese word their pages surely contain>` and
  `docwhere wiki spaces NAME`. Never guess or reuse an address the user did not give.
  Report each check line in plain words; on a FAIL, say what it means and what to try
  (wrong address, the network, a login the wiki asks for). Then offer `wiki:` rows for the
  notes map from the space list (see Drafting the map).
- When to search it: the notes map has a `wiki:` row whose kind or note fits the question, the
  user mentions the wiki, or the local search comes up short and a wiki is added. In thorough
  mode search both and merge.
- `docwhere wiki search <terms…> --json [--wiki NAME] [--space KEY] [-n 5]` — phrase terms as
  for local search. Hits carry `title`, `crumb`, `snippet`, `url` (with `#heading` when the
  section has one) and `updated`. The wiki's own word matching decides which pages come back;
  if Japanese terms find nothing, try shorter or other wordings before giving up (old servers
  index Japanese poorly unless their indexing language is set to Japanese).
- `docwhere wiki show N` — the hit's section with its neighbours, for reading before answering.
  `--whole` for the full page.
- Cite a wiki answer as `社内 wiki「<title>」 > <section>` with the URL, and the update date
  when it matters.
- To show it ("開いて", "見せて"): `docwhere wiki view [N…] --title "<question>" --answer "<one
  line>" --quote "<sentence copied from the section>"` — the same reader as `view`, with the
  quotes framed and a button that opens the page in the browser. It re-reads only the pages
  shown. `wiki search … --view` does it in one step without re-reading. `docwhere wiki open N`
  opens the page in the browser directly.
- Errors: "asks to log in" means the page or the wiki is not viewable anonymously (docwhere
  reads only those); "cannot reach" means this PC is not on the wiki's network. Say so and
  answer from the local documents.

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
