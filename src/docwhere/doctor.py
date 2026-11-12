"""``docwhere doctor``: check, on the PC at hand, everything docwhere relies on that a test here
cannot vouch for — Office conversions, the background reader outliving the command, the
viewer window, the skill file the agent loads, and how long a folder's change check takes.

Each check prints one line, ``ok`` / ``FAIL`` / ``--`` (skipped or not known yet), so the
whole output can be pasted back as it is.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from docwhere.index import Index

OFFICE_TIMEOUT = 60  # seconds one Office probe may take; a dialog waiting for an answer runs into it
MAX_DESCRIPTION = 1024  # agents refuse to load a skill whose description is longer
PROBE_FILE = "doctor_probe.json"
PROBE_WAIT = 120  # seconds the survival probe waits for the doctor command to end
PROBE_GRACE = 5  # and then lives on, before it reports that it survived
TOKEN = "docwhere-probe-4711"  # text each probe document carries, looked for after conversion
WEBVIEW2 = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"  # the WebView2 Runtime's update client id


@dataclass
class Check:
    name: str
    ok: bool | None  # None: skipped, or not known yet
    detail: str


def register(sub) -> None:
    p = sub.add_parser("doctor", help="check that Office conversions, background reading, the viewer window "
                       "and the skill file work on this PC; paste the output when asking for help")
    p.add_argument("roots", type=Path, nargs="*", help="also time the change check of these folders")
    p.add_argument("--skill", type=Path, action="append", default=[],
                   help="a SKILL.md your agent loads (e.g. its copy in the agent's skills folder), repeatable")
    p.add_argument("--no-office", action="store_true", help="skip the Office conversions (they take a minute)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("doctor-probe", help=argparse.SUPPRESS)  # the survival probe (started by doctor)
    p.add_argument("parent", type=int)
    p.add_argument("record", type=Path)
    p.add_argument("id")
    p.set_defaults(func=cmd_probe)


def cmd_doctor(args) -> int:
    index = Index(args.db)
    checks = [*_index_checks(index), _survival(index)]
    if not args.no_office:
        checks += _office_checks()
    checks += [_window_check(), *_skill_checks(args.skill), *(_scan_check(index, Path(r)) for r in args.roots)]
    if args.json:
        print(json.dumps([asdict(c) for c in checks], ensure_ascii=False, indent=1))
    else:
        for c in checks:
            print(("ok   " if c.ok else "--   " if c.ok is None else "FAIL ") + f"{c.name}: {c.detail}")
    return 1 if any(c.ok is False for c in checks) else 0


# ---- the index ---------------------------------------------------------------------------


def _index_checks(index: Index) -> list[Check]:
    checks = []
    try:
        with index.db:
            index.db.execute("CREATE TEMP TABLE doctor_write(x)")
            index.db.execute("DROP TABLE doctor_write")
        probe = index.path.with_name("doctor_write.tmp")
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        checks.append(Check("index", True, f"{index.path} can be written"))
    except (OSError, sqlite3.Error) as e:
        checks.append(Check("index", False, f"{index.path}: {e}"))
    try:
        db = sqlite3.connect(":memory:")
        db.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
        db.close()
        checks.append(Check("search engine", True, f"SQLite {sqlite3.sqlite_version} with trigram full-text search"))
    except sqlite3.Error as e:
        checks.append(Check("search engine", False, f"SQLite {sqlite3.sqlite_version} cannot search Japanese text: {e}"))
    return checks


# ---- the background reader outliving the command ------------------------------------------


def _survival(index: Index) -> Check:
    """What the probe started by the previous doctor run found, then a new probe for the next.
    One run cannot see past its own end, which is exactly when the reader would be stopped."""
    from docwhere import background

    record = index.path.with_name(PROBE_FILE)
    try:
        last = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        last = None
    name = "background reading"
    if last is None:
        check = Check(name, None, "not known yet: run docwhere doctor again in 10 seconds")
    elif last.get("survived"):
        check = Check(name, True, "a process started by docwhere keeps running after the command ends")
    elif "pid" not in last:
        started = time.time() - last["spawned"] < 15
        check = Check(name, None, "not known yet: run docwhere doctor again in 10 seconds") if started else             Check(name, False, "the probe process did not start, so searches can read files only while they run")
    elif _alive(last["pid"]):
        check = Check(name, None, "not known yet: run docwhere doctor again in 10 seconds")
    else:
        check = Check(name, False, "the probe was stopped together with the command that started it: files are"
                      " read only while a search runs, so a large folder is read a little at a time, search by search")
    probe = uuid.uuid4().hex
    record.write_text(json.dumps({"id": probe, "spawned": time.time()}), encoding="utf-8")
    background.spawn_detached(["--db", str(index.path), "doctor-probe", str(os.getpid()), str(record), probe])
    return check


def cmd_probe(args) -> int:
    def write(**fields) -> None:
        try:
            data = json.loads(args.record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("id") == args.id:  # a later doctor run started its own probe: leave its record alone
            args.record.write_text(json.dumps({**data, **fields}), encoding="utf-8")

    write(pid=os.getpid())
    _wait_for_exit(args.parent, PROBE_WAIT)
    time.sleep(PROBE_GRACE)
    write(survived=True)
    return 0


def _alive(pid: int) -> bool:
    if os.name == "nt":
        import pywintypes
        import win32api
        import win32con
        import win32process

        try:
            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        except pywintypes.error:
            return False
        try:
            return win32process.GetExitCodeProcess(handle) == 259  # STILL_ACTIVE
        finally:
            win32api.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_for_exit(pid: int, limit: float) -> None:
    if os.name == "nt":
        import pywintypes
        import win32api
        import win32con
        import win32event

        try:
            handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
        except pywintypes.error:
            return  # already gone
        try:
            win32event.WaitForSingleObject(handle, int(limit * 1000))
        finally:
            win32api.CloseHandle(handle)
        return
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.5)


# ---- Office conversions ----------------------------------------------------------------------


def _office_checks() -> list[Check]:
    """Convert a small legacy file with each application, the same way a sync does: in a worker
    that is stopped, Office with it, when it takes too long."""
    from docwhere import workers

    if os.name != "nt":
        return [Check("Office", None, "legacy .doc/.ppt (and .xls saved as HTML or XML) need Microsoft Office on Windows")]
    names = {"word": "Word (.doc)", "excel": "Excel (.xls)", "powerpoint": "PowerPoint (.ppt)"}
    tasks = [workers.Task(kind, serial=True) for kind in names]
    checks = []
    for out in workers.run(tasks, office_probe, workers=1, timeout=OFFICE_TIMEOUT):
        name = names[out.task.path]
        if out.timed_out:
            checks.append(Check(name, False, f"stopped after {OFFICE_TIMEOUT}s and closed: the application waited on"
                                " something, most likely a dialog. Such files are given up and listed as unreadable"))
        elif out.failure:
            checks.append(Check(name, False, out.failure))
        else:
            checks.append(Check(name, *out.result))
    return checks


def office_probe(kind: str) -> tuple[bool | None, str]:
    """Runs in a worker: make a legacy file with Office and read it back through the extractor.
    An .xls is read back through Excel, the way the .xls files xlrd cannot read are."""
    import pywintypes

    from docwhere.extractors import extract
    from docwhere.extractors import legacy as legacy_module

    read = legacy_module._xls_through_excel if kind == "excel" else extract

    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = _make_legacy(kind, Path(tmp))
            text = "\n".join(line for chunk in read(legacy) for line in [chunk.title, *chunk.lines])
    except pywintypes.com_error as e:
        if e.hresult == -2147221005:  # CO_E_CLASSSTRING: the application is not installed
            if kind == "excel":
                return None, "not installed; .xls files are still read, except those saved as HTML or XML"
            return None, "not installed; these files are listed as unreadable"
        return False, f"Office refused: {e}"
    except Exception as e:  # noqa: BLE001 - report any failure as the result instead of losing the worker
        return False, f"{type(e).__name__}: {e}"
    if TOKEN not in text:
        return False, "converted, but the text did not come through"
    return True, f"converted and read in {time.monotonic() - started:.1f}s"


def _make_legacy(kind: str, folder: Path) -> Path:
    from docwhere.extractors import legacy

    if kind == "word":
        import docx

        other = folder / "linked.docx"
        docx.Document().save(other)
        doc = docx.Document()
        doc.add_heading("doctor", 1)
        doc.add_paragraph(TOKEN)
        _add_link(doc, other)  # the kind of document that made Word ask about updating links on some PCs
        source = folder / "probe.docx"
        doc.save(source)
        opened = legacy.open_word_document(source)
        out = folder / "probe.doc"
        try:
            opened.SaveAs2(str(out), FileFormat=0)  # wdFormatDocument97
        finally:
            opened.Close(SaveChanges=0)
        return out
    if kind == "excel":
        import openpyxl

        source = folder / "probe.xlsx"
        wb = openpyxl.Workbook()
        wb.active["A1"], wb.active["A2"] = "doctor", TOKEN
        wb.save(source)
        book = legacy._app("Excel.Application").Workbooks.Open(str(source), ReadOnly=True, AddToMru=False)
        out = folder / "probe.xls"
        try:
            book.SaveAs(str(out), FileFormat=56)  # xlExcel8
        finally:
            book.Close(SaveChanges=False)
        return out
    import pptx

    source = folder / "probe.pptx"
    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[0])
    slide.shapes.title.text, slide.placeholders[1].text = "doctor", TOKEN
    deck.save(source)
    pres = legacy._app("PowerPoint.Application").Presentations.Open(str(source), ReadOnly=True, WithWindow=False)
    out = folder / "probe.ppt"
    try:
        pres.SaveAs(str(out), 1)  # ppSaveAsPresentation
    finally:
        pres.Close()
    return out


def _add_link(doc, target: Path) -> None:
    """An INCLUDETEXT field: a link to another file, like the documents that made Word ask."""
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls

    path = str(target).replace("\\", "\\\\")
    doc.add_paragraph()._p.append(parse_xml(f'<w:fldSimple {nsdecls("w")} w:instr=\'INCLUDETEXT "{path}"\'/>'))


# ---- the viewer window -------------------------------------------------------------------------


def _window_check() -> Check:
    from docwhere import window

    if not window.available():
        return Check("viewer window", False, "pywebview is missing: view pages open in the browser")
    if os.name != "nt":
        return Check("viewer window", True, "pywebview is installed")
    version = _webview2_version()
    if version is None:
        return Check("viewer window", False, "the WebView2 Runtime is not installed: view pages open in the browser")
    return Check("viewer window", True, f"WebView2 Runtime {version}")


def _webview2_version() -> str | None:
    import winreg

    keys = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2}"),
    ]
    for hive, key in keys:
        try:
            with winreg.OpenKey(hive, key) as k:
                version = winreg.QueryValueEx(k, "pv")[0]
        except OSError:
            continue
        if version and version != "0.0.0.0":
            return version
    return None


# ---- the skill file ------------------------------------------------------------------------------


def _skill_checks(paths: list[Path]) -> list[Check]:
    bundled = sorted(Path(__file__).resolve().parents[2].glob("skills/*/SKILL.md"))
    checks = [_skill_check(p, None) for p in bundled]
    for path in paths:
        twin = next((b for b in bundled if b.parent.name == path.parent.name), None)  # a renamed copy is not compared
        checks.append(_skill_check(path, twin))
    if not paths:
        checks.append(Check("skill copy", None, "give --skill <the SKILL.md your agent loads> to check that copy too"))
    return checks


def _skill_check(path: Path, original: Path | None) -> Check:
    name = f"skill {path}"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return Check(name, False, f"cannot be read: {e}")
    problems = skill_problems(text, path.parent.name)
    if original is not None and original.exists() and original.read_text(encoding="utf-8") != text:
        problems.append(f"differs from {original}: copy it again")
    # the files the skill tells the agent to read must sit next to it, as current as SKILL.md
    for ref in sorted(set(re.findall(r"references/[\w.-]+\.md", text))):
        here = path.parent / ref
        if not here.exists():
            problems.append(f"{ref} is missing next to it: copy the whole skill folder")
        elif original is not None and (there := original.parent / ref).exists() and \
                there.read_text(encoding="utf-8") != here.read_text(encoding="utf-8"):
            problems.append(f"{ref} differs from {there}: copy it again")
    if problems:
        return Check(name, False, "; ".join(problems))
    return Check(name, True, "loads (header and description within the limits, and the files it refers to)")


def skill_problems(text: str, folder: str) -> list[str]:
    """What would stop an agent from loading the skill, from its header."""
    parts = text.split("---", 2)
    if not text.startswith("---") or len(parts) < 3:
        return ["no header between --- lines at the top"]
    lines = parts[1].splitlines()
    fields = {m.group(1): (i, m.group(2)) for i, line in enumerate(lines) if (m := re.match(r"^(\w+):\s*(.*)$", line))}
    problems = []
    if "name" not in fields:
        problems.append("no name")
    elif fields["name"][1].strip() != folder:
        problems.append(f"name {fields['name'][1].strip()!r} is not the folder's name {folder!r}")
    if "description" not in fields:
        return [*problems, "no description"]
    i, value = fields["description"]
    if value[:1] in (">", "|"):  # a block: the indented lines below
        body = []
        for line in lines[i + 1 :]:
            if line.strip() and not line.startswith((" ", "\t")):
                break
            body.append(line.strip())
        description = " ".join(b for b in body if b)
    else:
        description = value.strip()
        if ": " in description or " #" in description:
            problems.append("the description has ': ' or ' #', which breaks a plain YAML value; write it as a >- block")
    if len(description) > MAX_DESCRIPTION:
        problems.append(f"the description is {len(description)} characters; agents load at most {MAX_DESCRIPTION}")
    return problems


# ---- the change check ----------------------------------------------------------------------------


def _scan_check(index: Index, root: Path) -> Check:
    """How long listing a folder takes: every search pays it before answering."""
    from docwhere.index import _walk, path_key

    root = root.resolve()
    if not root.is_dir():
        return Check(f"change check {root}", False, "no such folder")
    started = time.monotonic()
    files = sum(1 for _ in _walk(root, index._matcher(path_key(root)), {}))
    seconds = time.monotonic() - started
    return Check(f"change check {root}", True if seconds < 5 else None,
                 f"{files} files listed in {seconds:.1f}s (every search there spends this before answering)")
