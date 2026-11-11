# Leaving files out (office-search)

Read this before running `docwhere exclude` or answering what is excluded.


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
