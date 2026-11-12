# When something does not work on this PC (office-search)


Run `docwhere doctor` when Office files stay unreadable, a view does not open in the window,
the background reading never seems to progress, or the user asks to check the setup
("動作確認して"). It prints one line per check (`ok` / `FAIL` / `--`); each Office check takes a few
seconds and is stopped after 60. Report each FAIL in plain words with what it means for searching. One
check, whether background reading survives the command, is only known on the second run:
when it says `--`, run it again after 10 seconds. Pass `--skill <the SKILL.md you were
loaded from>` to check that copy is current.

## What makes reading slow

When the user asks what is slow ("何が遅い？", "なんでこんなに時間かかるの"), run
`docwhere status --json` and read `read_times`: per file kind, `files` read, the `seconds`
they took in all and the `average` per file, the slowest kind first. Answer with the kinds
that take most of the time, in plain words: 「時間の大半は .doc（312 件で約 8 分、1 件 1.6 秒）
です。.doc は Word で変換してから読むためです」. Then what helps, only where it fits:
- a folder of old versions, backups or archives among the slow files: offer to exclude it
  (`references/exclude.md`)
- many .doc or .ppt: these go through Office one at a time; say so, there is no switch to
  make them faster
- the reading was already done: a later search reads only new and changed files, so it is
  slow once

`read_times` only covers files read since this was added; when it is empty or small, say
the numbers fill in as files are read, and that `docwhere index --rebuild <folder>` reads
the folder again to measure all of it (it takes as long as the first reading did).
