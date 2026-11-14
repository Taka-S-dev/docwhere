# The team wiki (office-search)

Read this before running any `docwhere wiki` command.

`docwhere wiki` searches a wiki through the wiki's own search and reads only the pages it
returns; nothing is copied to disk except the last result list. It is the only part of
docwhere that uses the network, and only towards wikis the user added. Say "社内 wiki" (or the
name the user gave it) in answers, not the product name.

- Which wikis exist: `docwhere wiki list`. None → ask the user for the wiki's address (the
  top page they open in the browser) and a short name, then run
  `docwhere wiki add NAME URL [--space KEY]` yourself, followed by
  `docwhere wiki check NAME --term <a Japanese word their pages surely contain>` and
  `docwhere wiki spaces NAME`. Never guess or reuse an address the user did not give.
  Tell the user it is saved on this PC (the `saved in` line), not only in this conversation.
  Report each check line in plain words; on a FAIL, say what it means and what to try
  (wrong address, the network, a login the wiki asks for). Then offer `wiki:` rows for the
  notes map from the space list (see Drafting the map in `references/notes.md`).
- `docwhere wiki search <terms…> --json [--wiki NAME] [--space KEY] [-n 5]` — phrase terms as
  for local search. Hits carry `title`, `crumb`, `snippet`, `url` (with `#heading` when the
  section has one) and `updated`. The wiki's own word matching decides which pages come back;
  if Japanese terms find nothing, try shorter or other wordings before giving up (old servers
  index Japanese poorly unless their indexing language is set to Japanese).
- `docwhere wiki show N` — the hit's section with its neighbours, for reading before answering.
  `--whole` for the full page.
- Cite a wiki answer as `社内 wiki「<title>」 > <section>` with the URL, and the update date
  when it matters.
- To show it ("開いて", "見せて"), with `--note` after a `--quote` as for local files:
  `docwhere wiki view [N…] --title "<question>" --answer "<one
  line>" --quote "<sentence copied from the section>"` — the same reader as `view`, with the
  quotes framed and a button that opens the page in the browser. It re-reads only the pages
  shown. `wiki search … --view` does it in one step without re-reading. `docwhere wiki open N`
  opens the page in the browser directly.
- Errors: "asks to log in" means the page or the wiki is not viewable anonymously (docwhere
  reads only those); "cannot reach" means this PC is not on the wiki's network. Say so and
  answer from the local documents.
