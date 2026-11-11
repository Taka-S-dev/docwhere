# When something does not work on this PC (office-search)


Run `docwhere doctor` when Office files stay unreadable, a view does not open in the window,
the background reading never seems to progress, or the user asks to check the setup
("動作確認して"). It prints one line per check (`ok` / `FAIL` / `--`); each Office check takes a few
seconds and is stopped after 60. Report each FAIL in plain words with what it means for searching. One
check, whether background reading survives the command, is only known on the second run:
when it says `--`, run it again after 10 seconds. Pass `--skill <the SKILL.md you were
loaded from>` to check that copy is current.
