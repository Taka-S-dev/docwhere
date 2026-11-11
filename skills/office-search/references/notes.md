# Editing the notes file (office-search)

Read this before adding a row, drafting the map or opening the file. The file's place
and columns are in SKILL.md (The notes file).

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

