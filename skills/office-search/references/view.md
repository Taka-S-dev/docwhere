# Showing the hits (office-search)

Read this before running `docwhere view` or `search --view`, or `wiki view`.

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
- When the answer takes more than one place to reach — a value on one page and its
  condition on another, a table that refers to a figure, two documents read together —
  show how they connect: follow each `--quote` (or `--figure`) with `--note "<what this
  place shows, in one plain sentence>"`. With notes the evidence reads as numbered steps
  ending in 「つまり <--answer>」, so the user can follow the reasoning, not only the
  sources. Give the steps in the order of the reasoning, and keep each note to what that
  place itself says:
  `--quote "失敗したときの再試行は図2の流れに従う。" --note "本文は再試行の回数を図2に任せている"
  --figure "図2 再試行の流れ" --note "図2 には最大3回とある" --answer "再試行は最大3回です"`.
- When the evidence is a figure (a diagram, chart or screenshot) rather than a sentence,
  pass `--figure` instead of `--quote`: the figure is ringed on its page and shown as a
  close-up. Name it by its caption ("図2 再試行の流れ") or by a word printed inside it when
  that word is real text; a figure that is only a picture has no text of its own, so when
  there is no caption, look at the page with `docwhere snap` and give its place instead:
  `p.12#2` is the second figure from the top of page 12 (of the first hit; `3:p.12#2` of
  hit 3). Figures are found in PDF, Word and PowerPoint pages, not in Excel or the wiki.
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
