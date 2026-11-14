"""The index manager: a window that shows every indexed folder, what changed in it since it was
read and the background reading as it goes, and lets the user read a folder again, stop the
reading, add a folder, change what is left out, rebuild a folder or take it out of the index.
A second tab shows the notes map (``notes.csv``) and edits it; a third searches the index the
way the agent does, and shows a hit in the viewer or opens its file at the spot.

It is a process and a window of its own (``docwhere manage``), so the viewer window stays as
it is while reading goes on. The safety rules are the viewer's: the page is a static file that
docwhere wrote and signed, its CSP forbids scripts, and the manager script is injected from
outside only while that page is showing. What the page may ask of Python is ``ManageApi``;
every method acts only on folders and files that are in the index, and the reading itself is
the background reader that searches use, so the window never reads files on its own.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from multiprocessing import AuthenticationError
from multiprocessing.connection import Listener
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from docwhere import background, notes
from docwhere.index import Index, path_key, prefix_of
from docwhere.settings import load_settings, private, save_settings
from docwhere.window import bridge_method

KIND = "manage"  # the window's pipe and settings are kept apart from the viewer's
STORAGE = "webview-manage"
PAGE_NAME = "manage.html"
DEFAULT_SIZE = (1040, 800)
START_TIMEOUT = 15.0
MAX_UNREADABLE = 200  # unreadable files listed per folder; the count is always given
SLOWEST_SHOWN = 5  # files that took longest, listed per folder
SEARCH_LIMIT = 30  # hits shown in the search tab
SEARCH_POOL = 300  # hits counted by kind, format and folder, so a narrowing button tells what it keeps
SLOW_SHORT = 1.0  # seconds a search with a two-letter term may take before the tab says why
SYNONYMS_NAME = "synonyms.csv"
PREVIEW_DPI = 110  # a page drawn in the search tab's pane: about the pane's width, small enough to send at once
PREVIEW_MODES = ("text", "page")  # the search tab's pane: the section's text, or its page as a picture
PREVIEW_MAX_SIDE = 2000  # pixels: a drawing sheet (A0) is drawn smaller, not at 3600 x 5100
# PyMuPDF must not be used from two threads at once, and each bridge call runs on a thread of
# its own; one page (or one Office export) at a time also keeps the memory and Office use flat
_PAGES = threading.Lock()  # the user's own wordings searched together (search tab)


def page_path(db: Path) -> Path:
    return db.with_name("manage") / PAGE_NAME


def write_page(db: Path) -> Path:
    """Write and sign the manager page (written at every start, so an update takes effect)."""
    from docwhere import window
    from docwhere.viewer import PAGE_TEMPLATE, STATIC_CSP, THEME

    path = page_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    page = PAGE_TEMPLATE.format(csp=STATIC_CSP, title="インデックス", style=THEME + STYLE, body=BODY)
    path.write_text(page, encoding="utf-8")
    window.sign_page(db, path)
    return path


TABS = ("folders", "map", "search")


def open_manager(db: Path, tab: str = "folders") -> bool:
    """Show the manager window on ``tab``, starting it if none is running. False when the
    window cannot be used here (no pywebview, or an index folder that others can write to)."""
    from docwhere import window

    if not window.available() or not private(db.parent):
        return False
    write_page(db)
    window.allow_foreground()
    if window.send(db, {"show": True, "tab": tab}, KIND):
        return True
    background.spawn_detached(["--db", str(db), "manage-window", "--tab", tab])
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.3)
        if window.send(db, {"ping": True}, KIND):
            return True
    return False


# ---- what the page may ask -------------------------------------------------------------------


class ManageApi:
    """Methods the manager script may call. Attributes are private so pywebview exposes only these.
    Each call opens its own index connection: the bridge runs every call on a thread of its own."""

    def __init__(self, db: Path) -> None:
        self._db = db
        self._window = None
        self._trusted = False  # set while the signed manager page is showing
        self._results: tuple[list[str], list[dict]] = ([], [])  # the search tab's last hits, to open
        self._page_asked = 0  # page pictures asked for so far: one that waited behind a newer one is dropped

    def _index(self) -> Index:
        return Index(self._db)

    @staticmethod
    def _root(index: Index, path: str) -> Path:
        """``path`` as an indexed folder; anything else is refused."""
        key = path_key(Path(str(path)))
        row = index.db.execute("SELECT path FROM roots WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise LookupError("このフォルダはインデックスにありません")
        return Path(row["path"])

    @bridge_method
    def overview(self) -> dict:
        index = self._index()
        try:
            background.expire(index)
            roots = index.roots()
            keys = {path_key(Path(r["path"])): r["path"] for r in roots}
            # what the notes map says of each folder: the row for it, or how many rows lie inside it
            mapped: dict[str, dict] = {}
            for item in notes.load(self._db)["items"]:
                cells = item.get("cells") or {}
                where = cells.get("path", "").strip()
                if where and not where.lower().startswith("wiki:") and Path(where).is_absolute():
                    mapped.setdefault(path_key(Path(where)), {"kind": cells.get("kind", "").strip(), "note": cells.get("note", "").strip()})
            out = []
            for r in roots:
                key = path_key(Path(r["path"]))
                inside = sum(k.startswith(prefix_of(key)) for k in mapped)
                # a folder searched on its own inside another indexed folder: its files are the
                # outer folder's too, so the window shows it under that folder, counted once
                parent = index.enclosing(path_key(Path(r["path"])))
                out.append({
                    **r,
                    "name": Path(r["path"]).name or r["path"],
                    "parent": parent,
                    "exists": Path(r["path"]).is_dir(),
                    "map": mapped.get(key),
                    "map_inside": inside,
                })
            queued = [q["path"] for q in index.db.execute("SELECT path FROM queue ORDER BY requested_at")]
            reader = background.state(index)
            state = None
            if reader is not None:
                state = {
                    "current": reader.current, "done": reader.done, "total": reader.total, "rate": reader.rate(),
                    "seconds_left": reader.seconds_left(),
                    "queued": reader.queued, "reading": reader.reading,
                }
            return {
                "roots": out,
                "pending": [q for q in queued if path_key(Path(q)) not in keys],  # added, not read yet
                "reader": state,
                "now": time.time(),
            }
        finally:
            index.close()

    @bridge_method
    def changes(self, path: str) -> dict:
        index = self._index()
        try:
            root = self._root(index, path)
            prefix = prefix_of(path_key(root))
            inside = [Path(r["path"]) for r in index.roots() if path_key(Path(r["path"])).startswith(prefix)]
            return {"path": str(root), **index.changes(root, inside)}
        finally:
            index.close()

    @bridge_method
    def times(self, path: str | None = None) -> dict:
        """Where reading spent its time, for one folder or (``path`` None) the whole index: per
        kind of file, and the slowest files. Asked for apart from ``overview``, which runs every
        second, because it adds up every file read."""
        index = self._index()
        try:
            root = self._root(index, path) if path else None
            kinds = index.read_times(root)
            return {
                "total": round(sum(k["seconds"] for k in kinds.values()), 1),
                "kinds": [{"ext": ext, **k} for ext, k in kinds.items()],
                "slowest": index.slowest(root, SLOWEST_SHOWN) if root else [],
            }
        finally:
            index.close()

    @bridge_method
    def update(self, paths: list) -> dict:
        """Hand the folders to the background reader (it reads only what changed)."""
        index = self._index()
        try:
            roots = [self._root(index, p) for p in paths[:100]]
            for root in roots:
                background.request(index, root)
                index.keep(root)
            return {"queued": [str(r) for r in roots]}
        finally:
            index.close()

    @bridge_method
    def stop(self) -> dict:
        index = self._index()
        try:
            return {"stopped": background.stop(index)}
        finally:
            index.close()

    @bridge_method
    def add_folder(self) -> dict:
        import webview

        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER)
        if not chosen:
            return {"added": None}
        folder = Path(chosen[0]).resolve()
        if not folder.is_dir():
            raise NotADirectoryError(f"フォルダが見つかりません: {folder}")
        index = self._index()
        try:
            if index.db.execute("SELECT 1 FROM roots WHERE key = ?", (path_key(folder),)).fetchone():
                index.keep(folder)  # added by hand: no longer one searched only once
                return {"added": None, "known": str(folder)}
            background.request(index, folder)
            return {"added": str(folder)}
        finally:
            index.close()

    @bridge_method
    def add_exclude(self, path: str, pattern: str) -> dict:
        index = self._index()
        try:
            root = self._root(index, path)
            added, dropped = index.add_excludes(root, [str(pattern)[:200]])
            return {"added": added, "dropped": dropped}
        finally:
            index.close()

    @bridge_method
    def remove_exclude(self, path: str, pattern: str) -> dict:
        index = self._index()
        try:
            removed, _ = index.remove_excludes(self._root(index, path), [str(pattern)])
            return {"removed": removed}
        finally:
            index.close()

    @bridge_method
    def keep(self, path: str) -> dict:
        """Keep a folder searched with --once in the index for good."""
        index = self._index()
        try:
            return {"kept": index.keep(self._root(index, path))}
        finally:
            index.close()

    @bridge_method
    def rebuild(self, path: str) -> dict:
        """Read the folder again from nothing, keeping what it leaves out."""
        index = self._index()
        try:
            root = self._root(index, path)
            index.forget(root, keep_excludes=True)
            background.request(index, root)
            index.keep(root)
            return {"queued": str(root)}
        finally:
            index.close()

    @bridge_method
    def forget(self, path: str) -> dict:
        index = self._index()
        try:
            root = self._root(index, path)
            return {"forgotten": str(root), **index.forget(root)}
        finally:
            index.close()

    @bridge_method
    def unreadable(self, path: str) -> dict:
        index = self._index()
        try:
            errors = index.errors([self._root(index, path)])
            return {"count": len(errors), "files": [{"path": p, "error": e} for p, e in errors[:MAX_UNREADABLE]]}
        finally:
            index.close()

    @bridge_method
    def reveal(self, path: str) -> bool:
        """Show an indexed folder, or an indexed file selected in its folder, in Explorer."""
        index = self._index()
        try:
            key = path_key(Path(str(path)))
            if index.db.execute("SELECT 1 FROM roots WHERE key = ?", (key,)).fetchone():
                os.startfile(str(path))  # an indexed folder, checked above
                return True
            if index.db.execute("SELECT 1 FROM files WHERE key = ?", (key,)).fetchone():
                subprocess.Popen(["explorer", f"/select,{Path(str(path))}"])  # an indexed file, checked above
                return True
            raise LookupError("インデックスにないファイルです")
        finally:
            index.close()

    # ---- the search tab

    def _kind_folders(self) -> list[tuple[Path, str]]:
        """The folders the notes map gives a kind to (wiki rows and file rows aside)."""
        out = []
        for item in notes.load(self._db)["items"]:
            cells = item.get("cells") or {}
            where, kind = cells.get("path", "").strip(), cells.get("kind", "").strip()
            if kind and where and not where.lower().startswith("wiki:") and Path(where).is_absolute():
                out.append((Path(where), kind))
        return out

    @bridge_method
    def search_scopes(self) -> dict:
        """What the search may be limited to: an indexed folder, or a kind of the notes map."""
        index = self._index()
        try:
            tops = [r["path"] for r in index.roots() if index.enclosing(path_key(Path(r["path"]))) is None]
        finally:
            index.close()
        kinds: dict[str, int] = {}
        for _, kind in self._kind_folders():
            kinds[kind] = kinds.get(kind, 0) + 1
        return {
            "roots": [{"path": p, "name": Path(p).name or p} for p in tops],
            "kinds": [{"kind": k, "folders": n} for k, n in kinds.items()],
        }

    @bridge_method
    def search(self, query: dict) -> dict:
        """Search as ``docwhere search`` does, on what is read (no reading is started here).
        ``query``: text (words, "phrases", -left-out), scope ("" for every folder, a folder of
        the index, or "kind:仕様書"), the switches all, name, regex and variants, and facet
        ({kind, ext, folder}: keep only the hits with these values)."""
        from docwhere.search import (
            PatternError,
            expand_variants,
            load_variant_groups,
            parse_query,
            search,
            search_names,
        )

        terms, exclude = parse_query(str(query.get("text") or "")[:500])
        if not terms:
            self._results = ([], [])
            return {"hits": [], "terms": [], "exclude": exclude}
        scope = str(query.get("scope") or "")
        index = self._index()
        try:
            indexed = [Path(r["path"]) for r in index.roots()]
            missing = 0
            if scope.startswith("kind:"):
                keys = [path_key(r) for r in indexed]
                roots = []
                for folder, kind in self._kind_folders():
                    if kind != scope[5:] or not folder.is_dir():
                        continue
                    key = path_key(folder)
                    if any(key == k or key.startswith(prefix_of(k)) for k in keys):
                        roots.append(folder)
                    else:
                        missing += 1  # written in the map, never read: nothing of it to search
            elif scope:
                roots = [self._root(index, scope)]
            else:
                roots = [r for r in indexed if index.enclosing(path_key(r)) is None]
            started = time.monotonic()
            regex, name, every = bool(query.get("regex")), bool(query.get("name")), bool(query.get("all"))
            # other wordings of the terms (サーバ / サーバー, ログイン / ログオン), searched alongside them;
            # not with "every term", where a variant would count as one more term to require
            variants = []
            if query.get("variants") and not (regex or name or every):
                variants = expand_variants(terms, load_variant_groups(self._db.with_name(SYNONYMS_NAME)))

            def run(words: list[str], leave_out: list[str], require_all: bool, limit: int) -> list[dict]:
                if not roots:
                    return []
                try:
                    if name:
                        found = search_names(index, roots, words, limit=limit, exclude=leave_out, regex=regex)
                    else:
                        found = search(index, roots, words, require_all=require_all, limit=limit,
                                       exclude=leave_out, regex=regex)
                except PatternError as e:
                    raise ValueError(f"正規表現として読めません: {e}") from None
                return [h.to_dict() for h in found]

            pool = run(terms + variants, exclude, every, SEARCH_POOL)
            facets = self._facets(pool, roots)
            chosen = query.get("facet") if isinstance(query.get("facet"), dict) else {}
            kept = [h for h, f in zip(pool, facets["of"]) if all(f.get(k) == v for k, v in chosen.items() if v)]
            for n, hit in enumerate(kept, 1):
                hit["n"] = n  # numbered as shown, so the buttons name the hit they show
            self._results = (terms + variants, kept)
            hints = []
            if not pool and exclude and run(terms + variants, [], every, 1):
                hints.append(f"除外（{'、'.join(exclude)}）を外すと見つかります")
            if not pool and every and len(terms) > 1 and run(terms, exclude, False, 1):
                hints.append("「すべての語を含む」を外すと見つかります")
            # said only when it matters: a one-letter term matches nearly everything, and a term
            # under three letters is searched without the index, which shows on a large one
            if not (regex or name):
                single = [t for t in terms if len(t) == 1]
                short = [t for t in terms if len(t) < 3]
                if single:
                    hints.append(f"{'、'.join(single)} のような 1 文字の語は、ほとんどの節に当たります")
                elif short and time.monotonic() - started > SLOW_SHORT:
                    hints.append(f"{'、'.join(short)} のような 2 文字の語は索引を使わずに探すため、時間がかかります。"
                                 "3 文字以上の語を足すと速くなります")
            return {
                "hits": kept[:SEARCH_LIMIT], "total": len(kept), "pooled": len(pool) >= SEARCH_POOL,
                "terms": terms, "variants": variants, "exclude": exclude, "roots": [str(r) for r in roots],
                "missing": missing, "ms": round((time.monotonic() - started) * 1000), "limit": SEARCH_LIMIT,
                "facets": facets["counts"], "facet": chosen, "hints": hints,
            }
        finally:
            index.close()

    def _facets(self, hits: list[dict], roots: list[Path]) -> dict:
        """For each hit its kind in the notes map, its format and its folder (the first one under
        the searched folder), and how many hits each value has."""
        kinds = sorted(((prefix_of(path_key(f)), k) for f, k in self._kind_folders()), key=lambda x: -len(x[0]))
        tops = sorted(((prefix_of(path_key(r)), r) for r in roots), key=lambda x: -len(x[0]))
        of, counts = [], {"kind": {}, "ext": {}, "folder": {}}
        for hit in hits:
            key = path_key(Path(hit["path"]))
            f = {
                "kind": next((k for prefix, k in kinds if key.startswith(prefix)), ""),
                "ext": Path(hit["path"]).suffix.lower(),
                "folder": "",
            }
            for prefix, root in tops:
                if key.startswith(prefix):
                    rest = Path(hit["path"]).relative_to(root).parts
                    inner = rest[0] if len(rest) > 1 else ""
                    f["folder"] = "\\".join(x for x in ((root.name if len(roots) > 1 else ""), inner) if x) or root.name
                    break
            of.append(f)
            for k, v in f.items():
                if v:
                    counts[k][v] = counts[k].get(v, 0) + 1
        ordered = {k: sorted(({"value": v, "count": n} for v, n in c.items()), key=lambda x: -x["count"])
                   for k, c in counts.items()}
        return {"of": of, "counts": ordered}

    @bridge_method
    def preview(self, n: int) -> dict:
        """The hit's section with the two before and after it, for the pane beside the results."""
        from docwhere.rendition import SLIDE_EXTS, WORD_EXTS

        hit = self._hit(n)
        ext = Path(hit["path"]).suffix.lower()
        # whether the pane can show the page as a picture: a PDF's own, or an Office file's export
        pages = "pdf" if ext == ".pdf" and hit["anchor"].get("page") else "office" if ext in WORD_EXTS | SLIDE_EXTS else None
        if not hit["chunk_id"]:
            return {"path": hit["path"], "sections": [], "note": hit["snippet"], "pages": None}
        index = self._index()
        try:
            rows = index.chunk_with_neighbors(hit["chunk_id"], 2)
        finally:
            index.close()
        return {
            "path": hit["path"],
            "sections": [{"title": r["title"], "crumb": r["crumb"], "body": r["body"][:20000],
                          "current": r["id"] == hit["chunk_id"]} for r in rows],
            "pages": pages,
        }

    @bridge_method
    def page_image(self, n: int, convert: bool = False) -> dict:
        """The hit's page as a picture with the terms marked. A Word or PowerPoint file is drawn
        from its PDF export: one made before is used at once, a new one only with ``convert``
        (Office takes seconds), otherwise ``convert`` in the answer asks for it."""
        hit = self._hit(n)
        ext = Path(hit["path"]).suffix.lower()
        if not Path(hit["path"]).is_file():
            return {"note": "ファイルが見つかりません（読み込んだ後に移動か削除されたようです）。本文は読み込んだときのものが見られます。"}
        self._page_asked += 1
        mine = self._page_asked
        with _PAGES:
            if mine != self._page_asked and not convert:
                return {"stale": True}  # the pane has moved on to another hit while this one waited
            return self._draw(hit, ext, bool(convert))

    def _draw(self, hit: dict, ext: str, convert: bool) -> dict:
        import pymupdf

        from docwhere.rendition import SLIDE_EXTS
        from docwhere.viewer import _data_uri, _paged, render_page

        renditions = self._db.with_name("renditions")
        paged = _paged(hit, renditions, export=convert)  # Office runs in a worker process (see rendition)
        if not paged.get("pdf"):
            if ext != ".pdf" and not convert:
                return {"convert": True, "app": "PowerPoint" if ext in SLIDE_EXTS else "Word"}
            return {"note": "PDF に書き出せませんでした（Office がないか、ファイルが開けません）。本文に切り替えて見てください。"
                    if ext != ".pdf" else "この結果にはページの位置がありません。本文に切り替えて見てください。"}
        page = paged["anchor"]["page"]
        terms = list(dict.fromkeys([*self._results[0], *hit["terms"]]))
        try:
            with pymupdf.open(paged["pdf"]) as doc:
                pages = doc.page_count
                rect = doc[page - 1].rect
            dpi = max(1, int(min(PREVIEW_DPI, PREVIEW_MAX_SIDE * 72 / max(rect.width, rect.height, 1))))
            image = render_page(paged["pdf"], page, terms, dpi, "jpeg")
        except (LookupError, RuntimeError, ValueError):  # the file changed since it was read, or will not open
            return {"note": "このページを描けませんでした（読み込んだ後にファイルが変わったようです）。本文に切り替えて見てください。"}
        return {"src": _data_uri(image), "page": page, "pages": pages, "exported": ext != ".pdf"}

    @bridge_method
    def preview_mode(self, mode: str | None = None) -> str:
        """How the search tab's pane shows a hit, "text" or "page"; with ``mode``, chosen from now on.
        Kept in the settings file: the window's own storage is wiped at every start."""
        if mode in PREVIEW_MODES:
            save_settings(self._db, search_preview=mode)
            return mode
        kept = load_settings(self._db).get("search_preview")
        return kept if kept in PREVIEW_MODES else PREVIEW_MODES[0]

    @bridge_method
    def copy(self, text: str) -> bool:
        """Put ``text`` (a citation) on the clipboard."""
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(str(text)[:4000], win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
        return True

    @bridge_method
    def open_synonyms(self) -> str:
        """Open the user's wordings file (created with an explanation first) in its application."""
        path = self._db.with_name(SYNONYMS_NAME)
        if not path.exists():
            path.write_text(
                "# 同じものを指す言い方を 1 行に 1 組、カンマで区切って書きます。検索タブの「言い換えも探す」で一緒に探します。\n"
                "# 例: 帳票,レポート,出力物\n",
                encoding="utf-8-sig",
            )
        os.startfile(str(path))  # the file beside the index, nothing the page names
        return str(path)

    def _hit(self, n: int) -> dict:
        hits = self._results[1]
        if not isinstance(n, int) or not 1 <= n <= len(hits):
            raise LookupError("もう一度検索してください")
        return hits[n - 1]

    @bridge_method
    def open_hits(self, numbers: list, title: str = "") -> str:
        """Show hits of the last search here in the viewer, with their evidence framed."""
        from types import SimpleNamespace

        from docwhere.present import show_page
        from docwhere.viewer import build_html

        hits = [h for h in (self._hit(n) for n in numbers[:SEARCH_LIMIT]) if h["chunk_id"]]
        if not hits:
            raise LookupError("ファイル名で見つけたものは、ファイルを開いて見てください")
        terms = self._results[0]
        title = str(title)[:200] or " ".join(terms)
        options = SimpleNamespace(pages=0, quote=[], answer="", title=title, browser=False, steps=None)
        index = self._index()
        try:
            with _PAGES:  # the page draws PDF pages too, and may export Word or PowerPoint files
                return show_page(
                index.path,
                    lambda in_window: build_html(index, terms, hits, 1, 0, [], "", title, cut_excerpts=not in_window),
                    title, options, True,
                )
        finally:
            index.close()

    @bridge_method
    def open_file(self, n: int) -> str:
        """Open a hit's file in its application at the spot (page, sheet and cell, heading)."""
        import pythoncom

        from docwhere.opener import open_at
        from docwhere.window import pdf_app

        hit = self._hit(n)
        pythoncom.CoInitialize()  # each bridge call runs on a thread of its own; Office is reached through COM
        try:
            return open_at(hit["path"], hit["anchor"], pdf_app(self._db))
        finally:
            pythoncom.CoUninitialize()

    # ---- the notes map

    def _describe(self, index: Index, path: str, wikis: set[str]) -> dict:
        """What a map row's path points at, for the mark beside it."""
        text = str(path).strip()
        if not text:
            return {"kind": "empty"}
        if text.lower().startswith("wiki:"):
            return {"kind": "wiki", "known": text[5:].split("/")[0] in wikis}
        target = Path(text)
        if not target.is_absolute():
            return {"kind": "relative"}
        try:
            kind = "folder" if target.is_dir() else "file" if target.is_file() else "missing"
        except OSError:
            kind = "missing"
        if kind == "missing":
            return {"kind": kind}
        key = path_key(target)
        indexed = any(key == k or key.startswith(prefix_of(k)) for k in (path_key(Path(r["path"])) for r in index.roots()))
        return {"kind": kind, "indexed": indexed}

    def _wikis(self) -> list[dict]:
        from docwhere.wiki.registry import wikis

        return wikis(self._db)

    def _in_map(self, path: str) -> Path:
        """``path`` as a path written in the map; anything else is refused."""
        wanted = path_key(Path(str(path)))
        for item in notes.load(self._db)["items"]:
            cell = (item.get("cells") or {}).get("path", "").strip()
            if cell and not cell.lower().startswith("wiki:") and Path(cell).is_absolute() and path_key(Path(cell)) == wanted:
                return Path(cell)
        raise LookupError("地図にない場所です")

    @bridge_method
    def notes(self) -> dict:
        data = notes.load(self._db)
        wikis = self._wikis()
        names = {w["name"] for w in wikis}
        index = self._index()
        try:
            for item in data["items"]:
                if "cells" in item:
                    item["status"] = self._describe(index, item["cells"].get("path", ""), names)
            data["roots"] = [r["path"] for r in index.roots()]
        finally:
            index.close()
        data["wikis"] = [f"wiki:{w['name']}" for w in wikis] + [f"wiki:{w['name']}/{s}" for w in wikis for s in w.get("spaces") or []]
        return data

    @bridge_method
    def describe(self, path: str) -> dict:
        index = self._index()
        try:
            return self._describe(index, str(path)[: notes.MAX_CELL], {w["name"] for w in self._wikis()})
        finally:
            index.close()

    @bridge_method
    def save_notes(self, items: list, version: str | None) -> dict:
        try:
            notes.save(self._db, items, version)
        except notes.Changed:
            raise RuntimeError("地図のファイルがほかで変更されています。読み込み直してから編集してください") from None
        except PermissionError:
            raise RuntimeError("地図のファイルを Excel などで開いているため保存できません。閉じてから保存してください") from None
        return self.notes()

    @bridge_method
    def pick(self, kind: str) -> str | None:
        """A folder (``kind`` "folder") or file chosen in a dialog, for a map row; nothing is read."""
        import webview

        dialog = webview.FileDialog.FOLDER if kind == "folder" else webview.FileDialog.OPEN
        chosen = self._window.create_file_dialog(dialog)
        return str(Path(chosen[0]).resolve()) if chosen else None

    @bridge_method
    def open_notes(self) -> str:
        """Open the map in its default application (usually Excel), creating it if missing."""
        path = notes.notes_path(self._db)
        if not path.exists():
            notes.save(self._db, [], None)
        os.startfile(str(path))  # the map beside the index, nothing the page names
        return str(path)

    @bridge_method
    def reveal_note(self, path: str) -> bool:
        target = self._in_map(path)
        if target.is_dir():
            os.startfile(str(target))  # a folder written in the map, checked above
        elif target.is_file():
            subprocess.Popen(["explorer", f"/select,{target}"])  # a file written in the map, checked above
        else:
            raise FileNotFoundError("見つかりません")
        return True

    @bridge_method
    def index_note(self, path: str) -> dict:
        """Start reading a folder written in the map, so searches find what is in it."""
        target = self._in_map(path)
        if not target.is_dir():
            raise NotADirectoryError("フォルダではありません")
        index = self._index()
        try:
            background.request(index, target.resolve())
            index.keep(target)
            return {"queued": str(target)}
        finally:
            index.close()


# ---- the window process ----------------------------------------------------------------------


def run(db: Path, tab: str = "folders") -> None:
    """The manager window process: show the page and come to the front when asked again."""
    import pywintypes
    import webview

    from docwhere import window as viewer_window

    if not private(db.parent):
        return
    page = write_page(db).resolve()
    try:
        listener = Listener(viewer_window.pipe_address(db, KIND), family="AF_PIPE", authkey=viewer_window.pipe_key(db))
    except OSError:
        viewer_window.send(db, {"show": True}, KIND)  # another manager took the pipe first
        return
    viewer_window.setup_log(db)  # bridge errors and slow calls go to window.log, as the viewer's do
    log = logging.getLogger("docwhere.window")  # the viewer's log: one window.log for both
    log.info("index window started")
    win32_errors = (OSError, pywintypes.error)

    geometry = viewer_window.usable_geometry(load_settings(db).get("manage_window") or {})
    api = ManageApi(db)
    win = webview.create_window(
        f"{viewer_window.title(db)} - インデックス", url=page.as_uri() + (f"#{tab}" if tab in TABS[1:] else ""), js_api=api,
        width=geometry.get("width", DEFAULT_SIZE[0]), height=geometry.get("height", DEFAULT_SIZE[1]),
        x=geometry.get("x"), y=geometry.get("y"), maximized=bool(geometry.get("maximized")),
        min_size=(720, 520), text_select=True,
    )
    api._window = win

    def distrust() -> None:
        api._trusted = False

    def inject() -> None:
        url = urlparse(win.get_current_url() or "")
        shown = Path(url2pathname(url.path)).resolve() if url.scheme == "file" else None
        if shown == page and viewer_window.page_signed(db, shown):
            api._trusted = True
            win.evaluate_js(MANAGE_JS)

    def remember() -> None:
        try:
            if (hwnd := viewer_window.own_window()) is not None:
                save_settings(db, manage_window=viewer_window.placement(hwnd))
        except win32_errors:
            log.exception("saving the index window's placement failed")

    closing = threading.Event()

    def serve() -> None:
        while not closing.is_set():
            try:
                conn = listener.accept()
            except (OSError, EOFError, AuthenticationError):
                if closing.is_set():
                    return
                log.exception("accepting a request for the index window failed")  # e.g. a caller without the key
                time.sleep(0.2)
                continue
            message = {}
            try:
                message = viewer_window.unpack(conn.recv_bytes(viewer_window.MAX_MESSAGE))
                conn.send_bytes(b"ok")
            except (OSError, EOFError, ValueError):
                log.exception("answering a request for the index window failed")
            finally:
                conn.close()
            if message.get("tab") in TABS and api._trusted:
                win.evaluate_js(f"window.__docwhereTab && window.__docwhereTab({message['tab']!r})")
            try:
                viewer_window.bring_to_front(False)
            except win32_errors:
                log.exception("raising the index window failed")

    win.events.before_load += distrust
    win.events.loaded += inject
    win.events.shown += lambda: viewer_window.bring_to_front(False)
    win.events.closing += remember
    win.events.closing += closing.set
    threading.Thread(target=serve, daemon=True).start()
    try:
        webview.start(private_mode=True, gui="edgechromium", storage_path=str(viewer_window.fresh_storage(db, STORAGE)))
    finally:
        closing.set()
        listener.close()


# ---- the page --------------------------------------------------------------------------------

# Without the window (opened in a browser), the page says where it works; the script replaces it.
BODY = """<div class="wrap" id="app">
<nav class="tabs" id="tabs" role="tablist" aria-label="表示するもの" hidden>
  <button type="button" role="tab" id="tab-folders" aria-controls="panel-folders" aria-selected="true" data-tab="folders">フォルダ</button>
  <button type="button" role="tab" id="tab-map" aria-controls="panel-map" aria-selected="false" data-tab="map" tabindex="-1">地図</button>
  <button type="button" role="tab" id="tab-search" aria-controls="panel-search" aria-selected="false" data-tab="search" tabindex="-1">検索</button>
</nav>
<div id="panel-folders" role="tabpanel" aria-labelledby="tab-folders">
<header class="head">
  <div class="head-text">
    <h1>インデックス</h1>
    <p class="summary" id="summary">docwhere manage で開いた窓の中で使えます。</p>
    <p class="checked" id="checked" hidden><span id="checked-text"></span><button type="button" class="link" data-act="recheck">再確認</button></p>
  </div>
  <div class="head-actions" id="head-actions" hidden>
    <button type="button" class="btn" data-act="add">フォルダを追加</button>
    <button type="button" class="btn primary" data-act="update-all" hidden>変更のあるフォルダを更新</button>
  </div>
</header>
<section class="costs" id="costs" hidden></section>
<section class="band" id="band" hidden aria-live="polite"></section>
<div class="filter" id="filter-box" hidden>
  <input type="search" id="filter" placeholder="フォルダを絞り込む（名前やパスの一部。/ で移動）" autocomplete="off" spellcheck="false"
    aria-label="フォルダを絞り込む" aria-controls="folders">
  <span class="filter-count" id="filter-count" aria-live="polite"></span>
</div>
<ul class="folders" id="folders"></ul>
<p class="no-match" id="no-match" hidden></p>
<div class="empty" id="empty" hidden>
  <p class="empty-title">検索するフォルダがまだありません</p>
  <p>フォルダを追加すると、すぐに更新を始めます。終わる前でも、読み込めた分から検索できます。</p>
  <button type="button" class="btn primary" data-act="add">フォルダを追加</button>
</div>
</div>
<div id="panel-map" role="tabpanel" aria-labelledby="tab-map" hidden>
<header class="head">
  <div class="head-text">
    <h1>資料の地図</h1>
    <p class="summary" id="map-summary">読み込んでいます…</p>
  </div>
  <div class="head-actions">
    <button type="button" class="btn" data-act="map-excel">Excel で開く</button>
    <button type="button" class="btn primary" data-act="map-add">行を追加</button>
  </div>
</header>
<div class="filter" id="map-filter-box" hidden>
  <input type="search" id="map-filter" placeholder="地図を絞り込む（場所・種類・メモ。/ で移動）" autocomplete="off" spellcheck="false"
    aria-label="地図を絞り込む" aria-controls="map-rows">
  <span class="filter-count" id="map-filter-count" aria-live="polite"></span>
</div>
<div class="map-wrap" id="map-wrap" hidden>
<table class="map">
<colgroup><col class="c-path"><col class="c-section"><col class="c-kind"><col class="c-note"><col class="c-ops"></colgroup>
<thead><tr><th scope="col">場所</th><th scope="col">見出し・位置</th><th scope="col">種類</th><th scope="col">メモ</th><th scope="col"><span class="sr">操作</span></th></tr></thead>
<tbody id="map-rows"></tbody>
</table>
</div>
<p class="no-match" id="map-no-match" hidden></p>
<div class="empty" id="map-empty" hidden>
  <p class="empty-title">地図はまだありません</p>
  <p>どの種類の資料がどのフォルダにあるかを書いておくと、検索するフォルダを選ぶのに使われます。AI に「地図作って」と頼むと下書きを作ります。</p>
  <button type="button" class="btn primary" data-act="map-add">行を追加</button>
</div>
<p class="hint map-hint" id="map-hint" hidden>場所はフォルダかファイルの絶対パス、または wiki:名前。ファイルの行には見出しやページ（3.2、p.12、slide 4、Sheet!D5）を書けます。# で始まる行はメモとして残ります。</p>
<datalist id="map-paths"></datalist><datalist id="map-kinds"></datalist>
<div class="unsaved" id="map-unsaved" role="status" hidden>
  <span>保存していない変更があります</span>
  <button type="button" class="btn" data-act="map-revert">変更を取り消す</button>
  <button type="button" class="btn primary" data-act="map-save">保存（Ctrl+S）</button>
</div>
</div>
<div id="panel-search" role="tabpanel" aria-labelledby="tab-search" hidden>
<header class="head">
  <div class="head-text">
    <h1>検索</h1>
    <p class="summary">インデックスにある資料を、AI と同じしくみで探します。</p>
  </div>
</header>
<div class="search-bar" id="search-bar-box">
  <input type="search" id="q" placeholder="探す語（空白で区切る。-旧版 で除外、&quot;…&quot; でひと続きの語句）" autocomplete="off"
    spellcheck="false" aria-label="探す語" aria-controls="results">
  <select id="scope" aria-label="探す範囲"><option value="">すべてのフォルダ</option></select>
</div>
<div class="search-opts">
  <label><input type="checkbox" id="opt-all">すべての語を含む</label>
  <label><input type="checkbox" id="opt-name">ファイル名で探す</label>
  <label><input type="checkbox" id="opt-regex">正規表現</label>
  <label title="サーバ／サーバー、ログイン／ログオンのような言い方の違いも一緒に探します"><input type="checkbox" id="opt-variants" checked>言い換えも探す</label>
  <button type="button" class="link" data-act="synonyms" title="自分の言い換え（帳票／レポートなど）を書き足します">言い換えを編集</button>
  <span class="search-meta" id="search-meta" aria-live="polite"></span>
</div>
<p class="search-note" id="search-note" hidden></p>
<div class="facets" id="facets" hidden></div>
<div class="search-body" id="search-body" hidden>
  <div class="search-list">
    <ol class="results" id="results" role="listbox" aria-label="検索結果"></ol>
    <div class="search-more" id="search-more" hidden>
      <button type="button" class="btn" data-act="view-all">上位の結果をまとめて読み取り画面で見る</button>
    </div>
  </div>
  <aside class="preview" id="preview" aria-label="選んだ結果の中身"></aside>
</div>
<div class="empty search-tips" id="search-tips">
  <p class="empty-title">インデックスにある資料から探します</p>
  <p>語を空白で区切ると、どれかを含む節が出ます。多くの語を含む節や、見出しに語がある節ほど上に出ます。</p>
  <p>-旧版 のように前に - を付けると、その語を含む節やフォルダを外します。&quot;接続 タイムアウト&quot; のように引用符で囲むと、ひと続きの語句として探します。</p>
  <p>↑↓ で結果を選ぶと、右にその箇所の中身が出ます。Enter（またはダブルクリック）で根拠に枠を付けた読み取り画面が開き、Ctrl+Shift+C で出典をコピーできます。</p>
</div>
</div>
</div>
<dialog id="confirm"><form method="dialog" class="confirm">
  <h2 id="confirm-title"></h2><p id="confirm-text"></p>
  <div class="buttons"><button value="cancel" class="btn">やめる</button><button value="ok" class="btn" id="confirm-ok"></button></div>
</form></dialog>"""

STYLE = """
* { box-sizing: border-box; }
[hidden] { display: none !important; }  /* the flex and grid boxes below would otherwise show */
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.6 "BIZ UDPGothic", "Yu Gothic UI", "Meiryo", sans-serif; }
.wrap { max-width: 1400px; margin: 0 auto; padding: 24px 24px 80px; }  /* one width for every tab and state: nothing jumps sideways */
button { font: inherit; }
.btn { padding: 5px 14px; color: var(--ink); background: var(--surface); border: 1px solid var(--rule);
  border-radius: 6px; cursor: pointer; white-space: nowrap; }
.btn:hover:not(:disabled) { border-color: var(--muted); }
.btn:disabled { opacity: .45; cursor: default; }
.btn.primary { color: var(--surface); background: var(--blue); border-color: var(--blue); }
.btn.danger { color: var(--warn); }
.btn.quiet { border-color: transparent; background: transparent; color: var(--blue); padding: 3px 6px; }
:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; border-radius: 4px; }

.tabs { display: flex; gap: 4px; margin: -8px 0 20px; border-bottom: 1px solid var(--rule); }
.tabs button { margin-bottom: -1px; padding: 8px 18px; border: 0; border-bottom: 3px solid transparent; background: none;
  color: var(--muted); font-weight: 700; cursor: pointer; }
.tabs button:hover { color: var(--ink); }
.tabs button[aria-selected="true"] { color: var(--ink); border-bottom-color: var(--marker); }
.tabs button { position: relative; }
.tabs button.unsaved::after { content: ""; position: absolute; top: 9px; right: 6px; width: 7px; height: 7px;
  border-radius: 50%; background: var(--marker); }  /* beside the label, so the tab keeps its size */
.sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
/* the map: a sheet whose cells are typed into in place */
.map-wrap { background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; overflow-x: auto; }
.map { width: 100%; min-width: 760px; border-collapse: collapse; table-layout: fixed; }
.map .c-path { width: 38%; } .map .c-section { width: 15%; } .map .c-kind { width: 12%; } .map .c-ops { width: 84px; }
.map th { padding: 8px 12px; text-align: left; font-size: 12px; font-weight: 700; color: var(--muted);
  border-bottom: 1px solid var(--rule); }
.map td { padding: 4px 6px; vertical-align: top; }
.map tr + tr td { border-top: 1px solid var(--rule); }
.map input { width: 100%; padding: 5px 6px; font: inherit; color: var(--ink); background: transparent;
  border: 1px solid transparent; border-radius: 4px; }
.map input:hover { border-color: var(--rule); }
.map input:focus { outline: none; border-color: var(--blue); background: var(--bg); }
.map input::placeholder { color: var(--muted); opacity: .7; }
.map .path-cell { display: flex; gap: 2px; }
.map .shown { flex: 1; min-width: 0; padding: 4px 6px; text-align: left; color: var(--ink); background: none;
  border: 1px solid transparent; border-radius: 4px; cursor: text; }
.map .shown:hover { border-color: var(--rule); }
.map .shown b { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 700; }
.map .shown small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 12px; }
.map .shown.blank { color: var(--muted); }
.map .mark { display: block; padding: 0 7px 4px; font-size: 12px; color: var(--muted); }
.map .mark.warn { color: var(--warn); }
.map .mark button { margin-left: 8px; padding: 0; border: 0; background: none; color: var(--blue); cursor: pointer; font-size: 12px; }
.map tr.edited > td:first-child { box-shadow: inset 4px 0 0 var(--marker); }
.map tr.comment input { font-style: italic; color: var(--muted); }
.map .ops { display: flex; justify-content: flex-end; gap: 2px; }
.icon { flex: none; width: 30px; height: 30px; padding: 0; border: 0; border-radius: 6px; background: transparent;
  color: var(--muted); cursor: pointer; }
.icon:hover { background: var(--bg); color: var(--ink); }
.icon.danger:hover { color: var(--warn); }
.map-hint { margin: 10px 2px 0; }
.unsaved { position: sticky; bottom: 16px; z-index: 5; display: flex; align-items: center; gap: 10px; margin-top: 16px;
  padding: 10px 12px 10px 18px; background: var(--surface); border-left: 6px solid var(--marker);
  border-radius: 0 8px 8px 0; box-shadow: 0 4px 16px rgba(0, 0, 0, .18); }
.unsaved span { flex: 1; font-weight: 700; }
/* search: one field, the scope beside it, the switches under it */
.search-bar { position: relative; display: flex; gap: 8px; margin: 0 0 10px; }
/* the scrollbar's room is kept even before the results make the page long, so nothing narrows when it appears */
html { scrollbar-gutter: stable; }
/* a search taking a moment: a line runs under the box, and the results it will replace are dimmed in place */
.search-bar.busy::after { content: ""; position: absolute; left: 0; right: 0; bottom: -5px; height: 2px; border-radius: 1px;
  background: linear-gradient(90deg, transparent, var(--blue), transparent) no-repeat; background-size: 35% 100%;
  animation: seeking 1.1s linear infinite; }
@keyframes seeking { from { background-position: -60% 0; } to { background-position: 160% 0; } }
@media (prefers-reduced-motion: reduce) { .search-bar.busy::after { animation: none; background: var(--blue); opacity: 0.5; } }
.stale { opacity: 0.5; transition: opacity 0.15s; }
.search-bar input { flex: 1; min-width: 0; padding: 10px 14px; font: inherit; font-size: 16px; color: var(--ink);
  background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; }
.search-bar input:focus-visible { outline: 2px solid var(--blue); outline-offset: 0; border-color: transparent; }
.search-bar select { max-width: 38%; padding: 0 10px; font: inherit; color: var(--ink); background: var(--surface);
  border: 1px solid var(--rule); border-radius: 8px; }
.search-opts { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 20px; margin: 0 0 14px; font-size: 13px;
  color: var(--muted); }
.search-opts label { display: inline-flex; align-items: center; gap: 6px; cursor: pointer; }
.search-meta { margin-left: auto; font-variant-numeric: tabular-nums; }
.search-note { margin: 0 0 12px; padding: 8px 14px; font-size: 13px; background: var(--surface);
  border-left: 4px solid var(--marker); border-radius: 0 6px 6px 0; }
.search-note.error { border-left-color: var(--warn); color: var(--warn); }
.results { margin: 0; padding: 0; list-style: none; background: var(--surface); border: 1px solid var(--rule);
  border-radius: 8px; }
.results:empty { display: none; }
.result { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 3px 16px; padding: 12px 16px; cursor: pointer; }
.result + .result { border-top: 1px solid var(--rule); }
.result:hover { background: color-mix(in srgb, var(--bg) 55%, transparent); }
.result.active { background: color-mix(in srgb, var(--blue) 14%, transparent); }
.r-head { display: flex; align-items: baseline; gap: 12px; min-width: 0; }
.r-name { flex: none; max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 700; }
.r-where { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 13px; }
.r-snippet { margin: 0; overflow-wrap: anywhere; }
.r-path { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--muted); font-size: 12px; }
.r-flag { color: var(--warn); font-size: 12px; }
/* results on the left, the chosen one's text on the right: leafing through needs no window */
.search-body { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); gap: 16px; align-items: start; }
.result { grid-template-columns: minmax(0, 1fr); }
.preview { position: sticky; top: 8px; max-height: calc(100vh - 16px); overflow: auto; padding: 0 18px 18px;
  background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; }
.preview:empty { display: none; }
.pv-head { position: sticky; top: 0; z-index: 1; margin: 0 -18px; padding: 14px 18px 12px; background: var(--surface);
  border-bottom: 1px solid var(--rule); }
.pv-name { margin: 0; font-size: 17px; overflow-wrap: anywhere; }
.pv-where { margin: 2px 0 0; color: var(--ink); font-size: 13px; }
.pv-path { margin: 2px 0 0; color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
.pv-ops { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.pv-sec { margin: 16px 0 0; color: var(--muted); }
.pv-sec h3 { margin: 0 0 4px; font-size: 13px; }
.pv-sec.current { color: var(--ink); padding: 8px 12px; margin-left: -12px; margin-right: -12px;
  border-left: 4px solid var(--marker); background: color-mix(in srgb, var(--marker-soft) 18%, transparent); border-radius: 0 6px 6px 0; }
.pv-text p { margin: 0 0 4px; overflow-wrap: anywhere; }
.pv-loading { color: var(--muted); margin: 16px 0 0; overflow-wrap: anywhere; }
/* text or page: a two-way switch at the end of the actions */
.pv-mode { display: inline-flex; margin-left: auto; border: 1px solid var(--rule); border-radius: 6px; overflow: hidden; }
.pv-mode .seg { padding: 4px 12px; font: inherit; font-size: 13px; color: var(--ink); background: var(--surface); border: 0; cursor: pointer; }
.pv-mode .seg + .seg { border-left: 1px solid var(--rule); }
.pv-mode .seg[aria-pressed="true"] { color: var(--surface); background: var(--blue); }
.pv-mode .seg:disabled { color: var(--muted); cursor: not-allowed; }
.pv-page .btn { margin-top: 10px; }
.pv-img { display: block; width: 100%; height: auto; margin: 16px 0 0; background: #fff; border: 1px solid var(--rule);
  border-radius: 4px; cursor: zoom-in; }
.pv-cap { margin: 6px 0 0; color: var(--muted); font-size: 12px; }
.facets { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 20px; margin: 0 0 12px; }
.facet { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.facet-label { color: var(--muted); font-size: 12px; font-weight: 700; }
.chip-btn { padding: 2px 10px; font-size: 13px; color: var(--ink); background: var(--surface); border: 1px solid var(--rule);
  border-radius: 999px; cursor: pointer; }
.chip-btn small { color: var(--muted); }
.chip-btn:hover { border-color: var(--muted); }
.chip-btn[aria-pressed="true"] { color: var(--surface); background: var(--blue); border-color: var(--blue); }
.chip-btn[aria-pressed="true"] small { color: inherit; }
.search-note span { display: block; }
/* too narrow for two columns: a pane below thirty results is never seen, so Enter's reading page stands in */
@media (max-width: 900px) { .search-body { grid-template-columns: minmax(0, 1fr); } .preview { display: none; } }
.search-more { margin: 12px 0 0; }
.search-tips p { max-width: 640px; margin: 0 auto 8px; color: var(--muted); }
.search-tips p.empty-title { color: var(--ink); }
.head { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; flex-wrap: wrap;
  margin-bottom: 18px; }
.head-text { flex: 1 1 360px; min-width: 0; }
.head h1 { margin: 0; font-size: 24px; line-height: 1.3; }
.summary { margin: 4px 0 0; }
.checked { margin: 2px 0 0; color: var(--muted); font-size: 13px; }
.link { margin-left: 10px; padding: 0; border: 0; background: none; color: var(--blue); cursor: pointer; font-size: 13px; }
.link:hover { text-decoration: underline; }
.head-actions { display: flex; gap: 8px; flex-wrap: wrap; }

/* the one thing on the page that moves: the reading in progress */
.band { position: sticky; top: 8px; z-index: 5; margin: 0 0 18px; padding: 16px 20px 18px; background: var(--surface);
  border-left: 6px solid var(--marker); border-radius: 0 8px 8px 0; box-shadow: 0 4px 16px rgba(0, 0, 0, .18); }
.band-top { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.band-title { margin: 0; font-size: 17px; font-weight: 700; }
.band-count { font-variant-numeric: tabular-nums; color: var(--muted); }
.bar { height: 8px; margin: 10px 0 8px; background: var(--bg); border-radius: 4px; overflow: hidden; }
.bar > span { display: block; height: 100%; width: 0; background: var(--marker); border-radius: 4px;
  transition: width .6s ease; }
.bar.scanning > span { width: 30%; animation: sweep 1.4s ease-in-out infinite; }
@keyframes sweep { from { transform: translateX(-100%); } to { transform: translateX(340%); } }
.band-meta { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;
  color: var(--muted); }
.band-files { margin: 8px 0 0; padding: 0; list-style: none; color: var(--muted); font-size: 13px; }
.band-files .slow { color: var(--warn); }

.filter { display: flex; align-items: center; gap: 12px; margin: 0 0 10px; }
.filter input { flex: 1; min-width: 0; padding: 7px 12px; font: inherit; color: var(--ink); background: var(--surface);
  border: 1px solid var(--rule); border-radius: 8px; }
.filter input:focus-visible { outline: 2px solid var(--blue); outline-offset: 0; border-color: transparent; }
.filter-count { color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.no-match { margin: 0; padding: 28px; text-align: center; color: var(--muted); background: var(--surface);
  border: 1px dashed var(--rule); border-radius: 8px; }
mark { color: inherit; background: var(--marker-soft); border-radius: 2px; }
.folder.active > .row { background: color-mix(in srgb, var(--blue) 14%, transparent); }
.filtering .facts .where { white-space: normal; word-break: break-all; }
.row-bar { grid-column: 2 / -1; height: 4px; margin: 6px 0 0; background: var(--bg); border-radius: 2px;
  overflow: hidden; }
.row-bar > span { display: block; height: 100%; width: 0; background: var(--marker); transition: width .6s ease; }
.row-bar.scanning > span { width: 30%; animation: sweep 1.4s ease-in-out infinite; }
/* where reading time goes: one bar of shares, the largest in the accent, the rest quieter */
.costs { margin: 0 0 18px; }
.costs-head { display: flex; align-items: baseline; gap: 12px; margin: 0 0 6px; font-size: 13px; color: var(--muted); }
.costs-head strong { color: var(--ink); font-weight: 700; }
.share { display: flex; height: 10px; border-radius: 5px; overflow: hidden; background: var(--bg); }
.share > span { height: 100%; }
.share > span + span { border-left: 2px solid var(--bg); }
.legend { display: flex; flex-wrap: wrap; gap: 4px 18px; margin: 8px 0 0; padding: 0; list-style: none; font-size: 13px;
  font-variant-numeric: tabular-nums; }
.legend li::before { content: ""; display: inline-block; width: 9px; height: 9px; margin-right: 6px; border-radius: 2px;
  background: var(--dot); vertical-align: 0; }
.legend small { color: var(--muted); }
.cost-table { width: 100%; max-width: 560px; border-collapse: collapse; font-size: 13px; font-variant-numeric: tabular-nums; }
.cost-table th { color: var(--muted); font-weight: 400; text-align: right; padding: 2px 10px 4px 0; }
.cost-table th:first-child, .cost-table td:first-child { text-align: left; }
.cost-table td { text-align: right; padding: 3px 10px 3px 0; border-top: 1px solid var(--rule); }
.cost-table .pct { width: 150px; }
.cost-table .pct > span { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
.cost-table .track { flex: 1; height: 6px; border-radius: 3px; background: var(--bg); overflow: hidden; }
.cost-table .track i { display: block; height: 100%; background: var(--marker); }
.cost-table .pct b { min-width: 3.2em; font-weight: 400; text-align: right; }
.slow-files { max-width: 560px; margin: 0; padding: 0; list-style: none; }
.slow-files li { display: flex; justify-content: space-between; gap: 12px; padding: 4px 0; border-top: 1px solid var(--rule);
  font-size: 13px; font-variant-numeric: tabular-nums; }
.slow-files button { padding: 0; border: 0; background: none; color: var(--blue); cursor: pointer; text-align: left;
  word-break: break-all; }
.slow-files .gave-up { color: var(--warn); white-space: nowrap; }
.folders { margin: 0; padding: 0; list-style: none; background: var(--surface); border: 1px solid var(--rule);
  border-radius: 8px; }
.folders:empty { display: none; }
.folder + .folder { border-top: 1px solid var(--rule); }
.row { display: grid; grid-template-columns: 32px minmax(0, 1fr) 132px 96px; align-items: center; gap: 2px 12px;
  padding: 10px 16px 10px 6px; cursor: pointer; }
.folder.child .row { padding-left: calc(var(--depth, 1) * 28px + 4px); }
.row:hover { background: color-mix(in srgb, var(--bg) 55%, transparent); }
.disclose { display: grid; place-items: center; width: 32px; height: 32px; padding: 0; border: 0;
  background: transparent; cursor: pointer; border-radius: 6px; }
.disclose::before { content: ""; width: 7px; height: 7px; border: solid var(--muted); border-width: 0 2px 2px 0;
  transform: rotate(-45deg); transition: transform .15s; }
.disclose[aria-expanded="true"]::before { transform: rotate(45deg); }
.row:hover .disclose::before { border-color: var(--ink); }
/* a folder with changes carries the same mark as the reading band: the two things to act on */
.folder.has-changes { box-shadow: inset 4px 0 0 var(--marker); }
.ident { min-width: 0; }
.name { font-weight: 700; font-size: 15px; }
.facts { display: flex; flex-wrap: wrap; gap: 2px 14px; margin-top: 1px; color: var(--muted); font-size: 12px;
  font-variant-numeric: tabular-nums; }
.facts .where { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; max-width: 100%; }
.pill-cell { justify-self: end; }
.actions { justify-self: end; }
.facts .bad { color: var(--warn); }
.pill { padding: 2px 10px; border-radius: 999px; font-size: 12px; white-space: nowrap; color: var(--muted);
  border: 1px solid var(--rule); }
.pill.changed { color: var(--ink); background: var(--marker-soft); border-color: transparent; font-weight: 700; }
.pill.reading, .pill.queued { color: var(--ink); background: var(--term); border-color: transparent; }
.pill.missing { color: var(--warn); border-color: currentColor; }
.pill.fresh::before { content: "✓ "; }

.details { padding: 4px 16px 18px 44px; display: grid; gap: 16px; }
.folder.child .details { padding-left: calc(var(--depth, 1) * 28px + 40px); }
.facts .on-map { color: var(--ink); }
/* a folder searched once leaves the index on its own: said in the warning colour, with the way to keep it */
.facts .once { display: inline-flex; gap: 8px; color: var(--warn); }
.facts .map-link { padding: 0; border: 0; background: none; color: var(--blue); cursor: pointer; font: inherit; }
.facts .kids { padding: 0; border: 0; background: none; color: var(--blue); cursor: pointer; font: inherit; }
.facts .kids::after { content: " ▸"; }
.facts .kids[aria-expanded="true"]::after { content: " ▾"; }
.details h3 { margin: 0 0 6px; font-size: 13px; color: var(--muted); font-weight: 700; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 8px; padding: 0; list-style: none; }
.chip { display: inline-flex; align-items: center; gap: 4px; padding: 2px 4px 2px 10px; background: var(--bg);
  border: 1px solid var(--rule); border-radius: 999px; font-size: 13px; }
.chip small { color: var(--muted); }
.chip button { width: 22px; height: 22px; padding: 0; border: 0; border-radius: 50%; background: transparent;
  color: var(--muted); cursor: pointer; }
.chip button:hover { background: var(--rule); color: var(--ink); }
.add-row { display: flex; gap: 6px; max-width: 520px; }
.add-row input { flex: 1; min-width: 0; padding: 5px 10px; font: inherit; color: var(--ink); background: var(--bg);
  border: 1px solid var(--rule); border-radius: 6px; }
.hint { margin: 6px 0 0; color: var(--muted); font-size: 12px; }
.none { margin: 0; color: var(--muted); }
.errors { margin: 0; padding: 0; list-style: none; max-height: 260px; overflow: auto; border: 1px solid var(--rule);
  border-radius: 6px; scrollbar-width: thin; }
.errors li { display: grid; grid-template-columns: minmax(0, 1fr); padding: 6px 10px; }
.errors li + li { border-top: 1px solid var(--rule); }
.errors button { justify-self: start; padding: 0; border: 0; background: none; color: var(--blue); cursor: pointer;
  text-align: left; word-break: break-all; }
.errors span { color: var(--muted); font-size: 12px; word-break: break-all; }
.ops { display: flex; flex-wrap: wrap; gap: 8px; }
.place { margin: 0 0 8px; color: var(--muted); font-size: 13px; word-break: break-all; }

.empty { padding: 40px 24px; text-align: center; background: var(--surface); border: 1px dashed var(--rule);
  border-radius: 8px; }
.empty-title { margin: 0 0 4px; font-size: 17px; font-weight: 700; }
.empty p { color: var(--muted); }

#toast[hidden] { display: none; }
#toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); display: flex; align-items: center;
  gap: 14px; max-width: min(640px, 92vw); padding: 10px 12px 10px 18px; border-radius: 8px; background: var(--ink);
  color: var(--bg); box-shadow: 0 6px 24px rgba(0, 0, 0, .25); z-index: 10; }
#toast button { padding: 3px 10px; border: 1px solid currentColor; border-radius: 6px; background: transparent;
  color: inherit; cursor: pointer; }
dialog#confirm { width: min(480px, 92vw); padding: 0; color: var(--ink); background: var(--surface);
  border: 1px solid var(--rule); border-radius: 10px; }
dialog#confirm::backdrop { background: rgba(0, 0, 0, .45); }
.confirm { padding: 20px 22px; }
.confirm h2 { margin: 0 0 8px; font-size: 17px; }
.confirm p { margin: 0 0 18px; color: var(--muted); white-space: pre-line; }
.confirm .buttons { display: flex; justify-content: flex-end; gap: 8px; }
@media (prefers-reduced-motion: reduce) {
  .bar > span, .disclose::before { transition: none; }
  .bar.scanning > span, .row-bar.scanning > span { animation: none; width: 100%; opacity: .5; }
  .row-bar > span { transition: none; }
}
@media (max-width: 720px) {
  .row { grid-template-columns: 32px minmax(0, 1fr) auto; }
  .row .actions { grid-column: 2 / -1; justify-self: start; }
}
"""

# Injected by the manager window after the signed page loads (the page's CSP forbids scripts).
# Plain DOM, no libraries; text from the index (paths, errors) is only ever set as text.
MANAGE_JS = r"""(() => {
  "use strict";
  if (window.__docwhereManage) return;
  window.__docwhereManage = true;

  const $ = (sel, root = document) => root.querySelector(sel);
  const h = (tag, props, ...kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
    return el;
  };
  // replaceChildren() would write a skipped part (null) as the text "null"
  const put = (el, ...kids) => el.replaceChildren(...kids.flat().filter((k) => k != null && k !== false));
  const fmt = (n) => Number(n || 0).toLocaleString("ja-JP");
  // the end of a path is what tells folders apart; the whole path is in the tooltip and the details
  const shortPath = (p) => {
    const parts = String(p).split(/[\\/]/).filter(Boolean);
    return parts.length > 4 ? `${parts[0]}\\…\\${parts.slice(-3).join("\\")}` : String(p);
  };
  // fzf-like matching: each space-separated word must match as an in-order subsequence of the
  // path. A match at the start of a segment (after \ / _ - or a space) or right after the
  // previous matched character scores higher, so "specv2" ranks specs_v2 above s-p-e-c-v-2.
  const fold = (t) => String(t).normalize("NFKC").toLowerCase();
  const matchWord = (word, text, from) => {
    // the best of: greedy from each place the first character occurs (texts here are short)
    let best = null;
    for (let start = text.indexOf(word[0], from); start !== -1; start = text.indexOf(word[0], start + 1)) {
      const at = [start];
      let i = start;
      for (const ch of word.slice(1)) {
        i = text.indexOf(ch, i + 1);
        if (i === -1) break;
        at.push(i);
      }
      if (at.length !== word.length) break;
      let score = 0;
      at.forEach((pos, n) => {
        score += 1;
        if (pos === 0 || "\\/_- .".includes(text[pos - 1])) score += 3;
        if (n && pos === at[n - 1] + 1) score += 2;
        else if (n) score -= Math.min(3, (pos - at[n - 1]) / 8);
      });
      if (!best || score > best.score) best = { score, at };
    }
    return best;
  };
  const fuzzy = (query, path) => {
    const words = fold(query).split(/\s+/).filter(Boolean);
    if (!words.length) return { score: 0, at: [] };
    const text = fold(path);
    let score = 0;
    const at = [];
    for (const word of words) {
      const m = matchWord(word, text, 0);
      if (!m) return null;
      score += m.score;
      at.push(...m.at);
    }
    // the folder's own name counts more than the folders above it
    const nameStart = String(path).replace(/[\\/]+$/, "").search(/[^\\/]*$/);
    score += at.filter((i) => i >= nameStart).length;
    score -= text.length / 1000;  // of equal matches, the shorter path first
    return { score, at };
  };
  // text with the matched characters (positions from ``offset`` in the full string) marked
  const marked = (text, at, offset = 0) => {
    const hits = new Set(at.map((i) => i - offset));
    const out = [];
    let run = "", inMark = false;
    const flush = () => { if (run) out.push(inMark ? h("mark", { text: run }) : run); run = ""; };
    [...text].forEach((ch, i) => {
      const hit = hits.has(i);
      if (hit !== inMark) { flush(); inMark = hit; }
      run += ch;
    });
    flush();
    return out;
  };

  const base = (p) => String(p).replace(/[\\/]+$/, "").split(/[\\/]/).pop() || String(p);
  const keyOf = (p) => String(p).replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
  const ago = (t, now) => {
    if (!t) return "未更新";
    const s = Math.max(0, now - t);
    if (s < 60) return "たった今";
    if (s < 3600) return `${Math.floor(s / 60)} 分前`;
    if (s < 86400) return `${Math.floor(s / 3600)} 時間前`;
    if (s < 172800) return "昨日";
    if (s < 30 * 86400) return `${Math.floor(s / 86400)} 日前`;
    const d = new Date(t * 1000);
    return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
  };
  const kindName = (ext) => ext || "(拡張子なし)";
  const seconds = (sec) => (sec < 0.1 ? "0.1 秒未満" : sec < 60 ? `${sec.toFixed(1)} 秒` : duration(sec));
  const share = (part, whole) => (whole ? Math.round((part / whole) * 100) : 0);
  // the largest kind in the accent, the next ones in steps of the link colour, the rest grey
  const SHADES = ["var(--marker)", "var(--blue)", "color-mix(in srgb, var(--blue) 60%, var(--bg))",
    "color-mix(in srgb, var(--blue) 35%, var(--bg))"];
  const OTHER = "var(--muted)";
  const duration = (sec) => {
    sec = Math.max(1, Math.round(sec));
    if (sec < 60) return `${sec} 秒`;
    const m = Math.round(sec / 60);
    return m < 60 ? `${m} 分` : `${Math.floor(m / 60)} 時間 ${m % 60} 分`;
  };

  // ---- the bridge
  let api = null;
  const ready = (async () => {
    for (let i = 0; i < 200; i++) {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.overview) { api = window.pywebview.api; return; }
      await new Promise((r) => setTimeout(r, 50));
    }
    throw new Error("bridge unavailable");
  })();
  const reason = (e) => String((e && e.message) || e || "").replace(/^[A-Za-z]+Error:\s*/, "");

  // ---- toast, with an optional action (undo)
  const toastBox = $("#toast");
  let toastTimer = 0;
  // a time as month/day hour:minute, for when a folder searched once leaves the index
  const clock = (t) => { const d = new Date(t * 1000), two = (n) => String(n).padStart(2, "0");
    return `${d.getMonth() + 1}/${d.getDate()} ${two(d.getHours())}:${two(d.getMinutes())}`; };
  const toast = (message, action) => {
    toastBox.replaceChildren(h("span", { text: message }));
    if (action) {
      toastBox.append(h("button", { type: "button", text: action.label, onclick: () => { toastBox.hidden = true; action.run(); } }));
    }
    toastBox.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastBox.hidden = true; }, action ? 8000 : 4000);
  };
  const failed = (message) => (e) => toast(`${message}: ${reason(e)}`);

  // ---- confirmation
  const dialog = $("#confirm");
  const confirmAction = (title, text, okLabel, danger) => new Promise((resolve) => {
    $("#confirm-title").textContent = title;
    $("#confirm-text").textContent = text;
    const ok = $("#confirm-ok");
    ok.textContent = okLabel;
    ok.className = danger ? "btn danger" : "btn primary";
    dialog.returnValue = "cancel";
    dialog.onclose = () => resolve(dialog.returnValue === "ok");
    dialog.showModal();
    $("button[value=cancel]", dialog).focus();
  });

  // ---- state
  let data = null;
  const checks = new Map();  // folder key -> {state: "checking" | "done" | "error", result, at}
  const rows = new Map();    // folder key -> row parts
  const expanded = new Set();
  const loaded = new Map();  // folder key -> unreadable list, loaded when the row is opened
  const costs = new Map();   // folder key -> where its reading time went, loaded when the row is opened
  const loadCost = (root, parts) => api.times(root.path).then((t) => {
    costs.set(keyOf(root.path), t);
    parts.sig = "";
    fillRow(parts.root, parts);
  }).catch(failed("読み込み時間を表示できませんでした"));
  let lastCurrent = null;

  const readerOf = () => (data && data.reader) || null;
  // folders inside another indexed folder: their files are the outer folder's too, so they are
  // shown under it (folded until opened), checked with it and counted once
  const within = (key, outer) => key.startsWith(`${outer}\\`);
  const topOf = (root) => {
    let r = root;
    while (r && r.parent) r = data.roots.find((x) => keyOf(x.path) === keyOf(r.parent)) || null;
    return r || root;
  };
  const kidsOf = (root) => data.roots.filter((r) => r.parent && keyOf(r.parent) === keyOf(root.path));
  const inner = (root) => data.roots.filter((r) => within(keyOf(r.path), keyOf(root.path)));
  const openKids = new Set();  // folders whose inner folders are shown
  const depthOf = (root) => { let n = 0; for (let r = root; r && r.parent; n++) r = data.roots.find((x) => keyOf(x.path) === keyOf(r.parent)); return n; };
  const label = (root) => {
    if (!root.parent) return root.name;
    const outer = String(root.parent).replace(/[\\/]+$/, "");
    return String(root.path).slice(outer.length).replace(/^[\\/]+/, "") || root.name;
  };
  // folders handed to the reader from this window, until it is seen to have finished them:
  // a folder with nothing to read is done within the polling gap, so without this the button
  // would seem to do nothing
  const requested = new Map();  // folder key -> {name, at, seen}
  const handedOver = (key) => {
    const reader = readerOf();
    return Boolean(reader && ((reader.current && keyOf(reader.current) === key) || reader.queued.some((q) => keyOf(q) === key)));
  };

  const statusOf = (root) => {
    const key = keyOf(root.path);
    const reader = readerOf();
    if (!root.exists) return { cls: "missing", text: "フォルダが見つかりません" };
    if (reader && reader.current && (keyOf(reader.current) === key || within(key, keyOf(reader.current)))) {
      const pct = reader.total ? Math.floor((reader.done / reader.total) * 100) : 0;
      return { cls: "reading", text: reader.total ? `更新中 ${pct}%` : "変更を確認中", busy: true };
    }
    if (reader && reader.current && within(keyOf(reader.current), key)) {
      return { cls: "reading", text: "中のフォルダを更新中", busy: true };
    }
    if (reader && reader.queued.some((q) => keyOf(q) === key)) return { cls: "queued", text: "順番待ち", busy: true };
    if (requested.has(key)) return { cls: "queued", text: "更新を準備中", busy: true };
    const check = checks.get(key);
    if (!check || check.state === "checking") return { cls: "", text: "確認中…" };
    if (check.state === "error") return { cls: "", text: "確認できませんでした", title: check.result };
    const r = check.result;
    if (!r.exists) return { cls: "missing", text: "フォルダが見つかりません" };
    const n = r.new + r.changed + r.gone;
    if (!n) return { cls: "fresh", text: "最新" };
    const parts = [r.new && `追加 ${fmt(r.new)} 件`, r.changed && `変更 ${fmt(r.changed)} 件`, r.gone && `削除 ${fmt(r.gone)} 件`]
      .filter(Boolean).join("、");
    return { cls: "changed", text: `変更 ${fmt(n)} 件`, title: parts, changes: n };
  };

  // ---- change checks, two at a time, never for a folder being read
  const waiting = [];
  let checking = 0;
  const queueCheck = (path) => {
    const root = data && data.roots.find((r) => keyOf(r.path) === keyOf(path));
    const top = root ? topOf(root) : null;
    if (top) path = top.path;
    const key = keyOf(path);
    if (waiting.some((p) => keyOf(p) === key)) return;
    checks.set(key, { state: "checking", at: Date.now() });
    if (top) for (const r of inner(top)) checks.set(keyOf(r.path), { state: "checking", at: Date.now() });
    waiting.push(path);
    pump();
  };
  const checked = (path, result) => {
    const at = Date.now();
    checks.set(keyOf(path), { state: "done", result, at });
    const found = new Map(Object.entries(result.inside || {}).map(([p, r]) => [keyOf(p), r]));
    const root = data && data.roots.find((r) => keyOf(r.path) === keyOf(path));
    for (const r of root ? inner(root) : []) {
      const k = keyOf(r.path);
      checks.set(k, found.has(k) ? { state: "done", result: found.get(k), at } : { state: "error", result: "もう一度確認してください", at });
    }
  };
  const pump = () => {
    while (checking < 2 && waiting.length) {
      const path = waiting.shift();
      checking++;
      api.changes(path)
        .then((result) => checked(path, result))
        .catch((e) => checks.set(keyOf(path), { state: "error", result: reason(e), at: Date.now() }))
        .finally(() => { checking--; render(); pump(); });
    }
    render();
  };
  const recheckAll = () => { if (data) data.roots.filter((r) => r.exists && !r.parent).forEach((r) => queueCheck(r.path)); };

  // ---- actions
  const update = (paths) => api.update(paths).then(() => {
    paths.forEach((p) => { checks.delete(keyOf(p)); requested.set(keyOf(p), { name: base(p), at: Date.now(), seen: false }); });
    toast(paths.length === 1 ? `「${base(paths[0])}」を更新します` : `${paths.length} つのフォルダを更新します`);
    refresh();
  }).catch(failed("更新を始められませんでした"));

  const stop = () => api.stop().then(() => {
    toast("更新を中止しました。ここまでに読み込んだ分は検索に使えます。続きは次の検索か「更新」で再開します");
    refresh();
  }).catch(failed("中止できませんでした"));

  const addFolder = () => api.add_folder().then((r) => {
    if (r.known) toast(`「${base(r.known)}」はもうインデックスにあります`);
    else if (r.added) { toast(`「${base(r.added)}」を追加しました。更新が終わる前でも、読み込めた分から検索できます`); refresh(); }
  }).catch(failed("フォルダを追加できませんでした"));

  const addExclude = (root, input) => {
    const pattern = input.value.trim();
    if (!pattern) { input.focus(); return; }
    api.add_exclude(root.path, pattern).then((r) => {
      input.value = "";
      if (!r.added.length) { toast(`「${pattern}」はもう除外しています`); return; }
      const dropped = r.dropped ? `（インデックスから ${fmt(r.dropped)} 件外しました）` : "";
      toast(`「${pattern}」を除外しました${dropped}`, { label: "元に戻す", run: () => removeExclude(root, pattern, true) });
      checks.delete(keyOf(root.path));
      refresh().then(() => queueCheck(root.path));
    }).catch(failed("除外できませんでした"));
  };
  const removeExclude = (root, pattern, quiet) => api.remove_exclude(root.path, pattern).then(() => {
    if (!quiet) {
      toast(`「${pattern}」の除外をやめました。次の更新から検索に含めます`, {
        label: "元に戻す", run: () => api.add_exclude(root.path, pattern).then(() => refresh()) });
    }
    refresh().then(() => queueCheck(root.path));
  }).catch(failed("除外をやめられませんでした"));

  const rebuild = async (root) => {
    const ok = await confirmAction(`「${root.name}」を作り直しますか？`,
      `${fmt(root.files)} 件のファイルをすべて読み込み直します。除外の設定は残ります。\n終わる前でも、読み込めた分から検索できます。`, "作り直す");
    if (!ok) return;
    api.rebuild(root.path).then(() => { toast(`「${root.name}」を作り直しています`); refresh(); }).catch(failed("作り直せませんでした"));
  };
  const forget = async (root) => {
    const outer = root.parent ? base(root.parent) : null;
    const kids = inner(root).length;
    const text = outer
      ? `「${outer}」の中にあるので、ファイルは「${outer}」の一部として検索に残ります。一覧から外れ、このフォルダに設定した除外は消えます。`
      : "このフォルダは検索の対象から外れます。ファイル自体は削除されません。\nこのフォルダに設定した除外も消えます。"
        + (kids ? `\n中のフォルダ ${kids} か所も一緒に外れます。` : "");
    const ok = await confirmAction(`「${label(root)}」をインデックスから外しますか？`, text, "外す", true);
    if (!ok) return;
    api.forget(root.path).then(() => {
      expanded.delete(keyOf(root.path));
      toast(outer ? `「${label(root)}」を一覧から外しました（「${outer}」の一部として検索できます）` : `「${root.name}」をインデックスから外しました`);
      refresh();
    }).catch(failed("インデックスから外せませんでした"));
  };
  const reveal = (path) => api.reveal(path).catch(failed("開けませんでした"));

  // ---- rendering: rows are kept and updated in place, so focus and typed text survive refreshes
  let totals = null;  // where the whole index spends its time; asked for when it may have changed
  const loadTotals = () => api.times(null).then((t) => { totals = t; renderCosts(); }).catch(() => {});
  const renderCosts = () => {
    const box = $("#costs");
    if (!totals || !data || !data.roots.length) { box.hidden = true; return; }
    box.hidden = false;
    if (!totals.total) {
      put(box, h("p", { class: "costs-head", text: "読み込みにかかった時間は、次に更新したファイルから記録されます。全体を測るには、フォルダを「作り直す」と記録されます。" }));
      return;
    }
    const top = totals.kinds.slice(0, SHADES.length);
    const rest = totals.kinds.slice(SHADES.length);
    const parts = top.map((k, i) => ({ label: kindName(k.ext), files: k.files, seconds: k.seconds, color: SHADES[i] }));
    if (rest.length) {
      parts.push({ label: "その他", files: rest.reduce((n, k) => n + k.files, 0),
        seconds: rest.reduce((n, k) => n + k.seconds, 0), color: OTHER });
    }
    put(box,
      h("p", { class: "costs-head" }, h("strong", { text: "読み込みにかかった時間" }), `合計 ${duration(totals.total)}`),
      h("div", { class: "share", role: "img", "aria-label": parts.map((x) => `${x.label} ${share(x.seconds, totals.total)}%`).join("、") },
        parts.map((x) => h("span", { style: `width:${(x.seconds / totals.total) * 100}%;background:${x.color}`,
          title: `${x.label} ${share(x.seconds, totals.total)}%（${fmt(x.files)} 件、${duration(x.seconds)}）` }))),
      h("ul", { class: "legend" }, parts.map((x) => h("li", { style: `--dot:${x.color}` },
        `${x.label} ${share(x.seconds, totals.total)}% `, h("small", { text: `${fmt(x.files)} 件、${duration(x.seconds)}` })))),
    );
  };

  const band = $("#band");
  const renderBand = () => {
    const reader = readerOf();
    if (!reader) { band.hidden = true; band.replaceChildren(); return; }
    const name = reader.current ? base(reader.current) : "フォルダ";
    const scanning = !reader.total;
    const pct = scanning ? 0 : Math.min(100, (reader.done / reader.total) * 100);
    const left = reader.total - reader.done;
    // the reader estimates again as files finish, weighing each file left by its kind
    const secondsLeft = reader.seconds_left;
    const perSecond = reader.rate ? `毎秒 ${reader.rate >= 10 ? fmt(Math.round(reader.rate)) : reader.rate.toFixed(1)} 件` : "";
    const pace = scanning ? "対象のファイルを数えています"
      : secondsLeft == null || !reader.done ? "残り時間を見積もっています"
      : left <= 0 ? "仕上げています"
      : `残り約 ${secondsLeft < 1 ? "1 秒" : duration(secondsLeft)}` + (perSecond ? `（${perSecond}）` : "");
    const next = reader.queued.length ? `次は ${reader.queued.map((q) => `「${base(q)}」`).join("、")}` : "";
    const slow = reader.reading.filter(([, s]) => s >= 5);
    band.hidden = false;
    put(band,
      h("div", { class: "band-top" },
        h("p", { class: "band-title", text: scanning ? `「${name}」の変更を確認しています` : `「${name}」を更新しています` }),
        scanning ? null : h("span", { class: "band-count", text: `${fmt(reader.done)} / ${fmt(reader.total)} 件` })),
      h("div", { class: `bar${scanning ? " scanning" : ""}`, role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100",
        "aria-valuenow": scanning ? null : String(Math.round(pct)), "aria-label": `「${name}」の更新` },
        h("span", { style: scanning ? null : `width:${pct}%` })),
      h("div", { class: "band-meta" },
        h("span", { text: [pace, next].filter(Boolean).join("。") }),
        h("button", { type: "button", class: "btn", text: "中止", onclick: stop })),
      slow.length ? h("ul", { class: "band-files" }, slow.map(([p, s]) => h("li", { class: s >= 60 ? "slow" : null,
        text: `${base(p)} を読み込み中（${duration(s)}）${s >= 60 ? "。3 分を超えたファイルは読み飛ばし、読めないファイルとして記録します" : ""}` }))) : null,
    );
  };

  const detailsOf = (root, parts) => {
    const key = keyOf(root.path);
    const box = parts.details;
    // keep the pattern being typed across refreshes
    const typed = box.querySelector("input") ? box.querySelector("input").value : "";
    const hadFocus = box.contains(document.activeElement) && document.activeElement.tagName === "INPUT";
    const input = h("input", { type: "text", placeholder: "例: 旧版、*_bak*、old/2019", "aria-label": `${root.name} で除外するもの` });
    input.value = typed;
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") addExclude(root, input); });
    const chips = root.excludes.map((x) => h("li", { class: "chip" },
      h("span", { text: x.pattern }),
      x.matched ? h("small", { text: `${fmt(x.matched)} か所` }) : null,
      h("button", { type: "button", "aria-label": `${x.pattern} の除外をやめる`, title: "除外をやめる", text: "×",
        onclick: (e) => { e.stopPropagation(); removeExclude(root, x.pattern); } })));
    const errors = loaded.get(key);
    const errorList = root.errors === 0 ? h("p", { class: "none", text: "ありません" })
      : !errors ? h("p", { class: "none", text: "取得しています…" })
      : h("ul", { class: "errors" }, errors.files.map((f) => h("li", {},
          h("button", { type: "button", text: base(f.path), title: `${f.path}\nエクスプローラーで表示`, onclick: () => reveal(f.path) }),
          h("span", { text: f.error }))),
          errors.count > errors.files.length ? h("li", {}, h("span", { text: `ほか ${fmt(errors.count - errors.files.length)} 件（docwhere status --errors）` })) : null);
    const cost = costs.get(key);
    let costSection = null;
    const slow = cost ? cost.slowest.filter((f) => f.seconds >= 1) : [];  // a list of 0.1 s files tells nothing
    if (cost && cost.total) {
      costSection = h("section", {}, h("h3", { text: `読み込みにかかった時間（合計 ${duration(cost.total)}）` }),
        h("table", { class: "cost-table" },
          h("thead", {}, h("tr", {}, ["形式", "件数", "合計", "1 件平均", "割合"].map((t) => h("th", { text: t })))),
          h("tbody", {}, cost.kinds.map((k) => h("tr", {},
            h("td", { text: kindName(k.ext) }), h("td", { text: `${fmt(k.files)} 件` }), h("td", { text: duration(k.seconds) }),
            h("td", { text: seconds(k.average) }),
            h("td", { class: "pct" }, h("span", {}, h("span", { class: "track" }, h("i", { style: `width:${share(k.seconds, cost.total)}%` })),
              h("b", { text: `${share(k.seconds, cost.total)}%` }))))))),
        slow.length ? h("h3", { text: "1 秒以上かかったファイル（長い順）", style: "margin-top:12px" }) : null,
        slow.length ? h("ul", { class: "slow-files" }, slow.map((f) => h("li", {},
          h("button", { type: "button", text: base(f.path), title: `${f.path}\nエクスプローラーで表示`, onclick: () => reveal(f.path) }),
          f.error && /timed out/.test(f.error) ? h("span", { class: "gave-up", text: `${seconds(f.seconds)}で打ち切り` })
            : h("span", { text: seconds(f.seconds) })))) : null);
    } else if (cost) {
      costSection = h("section", {}, h("h3", { text: "読み込みにかかった時間" }),
        h("p", { class: "none", text: "まだ記録がありません。次に更新したファイルから記録されます（全体を測るには「作り直す」）。" }));
    }
    put(box,
      h("section", {}, h("h3", { text: "除外するもの" }),
        chips.length ? h("ul", { class: "chips" }, chips) : h("p", { class: "none", text: "除外しているものはありません" }),
        h("div", { class: "add-row" }, input, h("button", { type: "button", class: "btn", text: "追加", onclick: () => addExclude(root, input) })),
        h("p", { class: "hint", text: "名前はどの深さのフォルダやファイルにも当たります。/ を含めると、このフォルダからのパスになります。大文字小文字は区別しません。" })),
      h("section", {}, h("h3", { text: `読めないファイル${root.errors ? `（${fmt(root.errors)} 件）` : ""}` }), errorList),
      costSection,
      h("section", {}, h("h3", { text: "場所" }), h("p", { class: "place", text: root.path })),
      h("section", {}, h("h3", { text: "操作" }), h("div", { class: "ops" },
        h("button", { type: "button", class: "btn", text: "今すぐ更新", disabled: Boolean(statusOf(root).busy) || !root.exists,
          onclick: () => update([root.path]) }),
        h("button", { type: "button", class: "btn", text: "エクスプローラーで開く", onclick: () => reveal(root.path) }),
        h("button", { type: "button", class: "btn", text: "作り直す", onclick: () => rebuild(root) }),
        h("button", { type: "button", class: "btn danger", text: "インデックスから外す", onclick: () => forget(root) }))),
    );
    if (hadFocus) input.focus();
  };

  const toggle = (root, parts) => {
    const key = keyOf(root.path);
    const open = !expanded.has(key);
    if (open) expanded.add(key); else expanded.delete(key);
    parts.disclose.setAttribute("aria-expanded", String(open));
    parts.details.hidden = !open;
    if (open) {
      parts.sig = "";
      fillRow(root, parts);
      if (root.errors && !loaded.get(key)) {
        api.unreadable(root.path).then((r) => { loaded.set(key, r); parts.sig = ""; fillRow(parts.root, parts); })
          .catch(failed("読めないファイルを表示できませんでした"));
      }
      if (!costs.has(key)) loadCost(root, parts);
    }
  };

  const rowFor = (root) => {
    const key = keyOf(root.path);
    let parts = rows.get(key);
    if (!parts) {
      const id = `d${rows.size}`;
      parts = {};
      parts.disclose = h("button", { type: "button", class: "disclose", "aria-expanded": "false", "aria-controls": id });
      parts.name = h("span", { class: "name" });
      parts.kids = h("button", { type: "button", class: "kids", "aria-expanded": "false" });
      parts.kids.addEventListener("click", (e) => {
        e.stopPropagation();
        const k = keyOf(parts.root.path);
        if (openKids.has(k)) openKids.delete(k); else openKids.add(k);
        render();
      });
      parts.facts = h("div", { class: "facts" });
      parts.pill = h("span", { class: "pill" });
      parts.pillCell = h("div", { class: "pill-cell" }, parts.pill);
      parts.action = h("button", { type: "button", class: "btn" });
      parts.details = h("div", { class: "details", id, hidden: true });
      parts.barFill = h("span");
      parts.bar = h("div", { class: "row-bar", hidden: true, "aria-hidden": "true" }, parts.barFill);
      parts.row = h("div", { class: "row" }, parts.disclose, h("div", { class: "ident" }, parts.name, parts.facts),
        parts.pillCell, h("div", { class: "actions" }, parts.action), parts.bar);
      parts.el = h("li", { class: "folder" }, parts.row, parts.details);
      parts.row.addEventListener("click", (e) => { if (!e.target.closest(".actions")) toggle(parts.root, parts); });
      parts.action.addEventListener("click", (e) => { e.stopPropagation(); update([parts.root.path]); });
      rows.set(key, parts);
    }
    parts.root = root;
    return parts;
  };

  const fillRow = (root, parts) => {
    const status = statusOf(root);
    const depth = parts.match ? 0 : depthOf(root);  // a filtered list is flat
    parts.el.classList.toggle("child", depth > 0);
    parts.el.style.setProperty("--depth", String(depth));
    const match = parts.match;
    const name = label(root);
    const nameAt = root.path.replace(/[\\/]+$/, "").length - name.length;
    if (match && match.at.length) put(parts.name, marked(name, match.at, nameAt));
    else parts.name.textContent = name;
    const kids = kidsOf(root);
    const open = openKids.has(keyOf(root.path));
    parts.kids.textContent = `中のフォルダ ${kids.length}`;
    parts.kids.setAttribute("aria-expanded", String(open));
    parts.kids.title = open ? "中のフォルダをたたむ" : `${kids.map(label).join("、")}（このフォルダの一部として、別に検索したフォルダ）`;
    parts.row.title = root.path;
    put(parts.facts,
      h("span", { text: `${fmt(root.files)} ファイル` }),
      root.errors ? h("span", { class: "bad", text: `読めない ${fmt(root.errors)} 件` }) : null,
      h("span", { text: `最終更新 ${ago(root.synced_at, data.now)}` }),
      root.excludes.length ? h("span", { text: `除外 ${root.excludes.length} 件` }) : null,
      root.once_until ? h("span", { class: "once" }, h("span", { text: `一時: ${clock(root.once_until)} にインデックスから外れます`,
          title: "一度だけ探すつもりで読んだフォルダです（search --once）。この時刻を過ぎると外れます。元のファイルはそのままです" }),
        h("button", { type: "button", class: "map-link", text: "残す", title: "インデックスに残し、外れないようにします",
          onclick: (e) => { e.stopPropagation(); api.keep(root.path).then(() => { toast("インデックスに残します"); refresh(); }).catch(failed("残せませんでした")); } })) : null,
      kids.length && !match ? parts.kids : null,
      root.map ? h("span", { class: "on-map", title: root.map.note ? `地図のメモ: ${root.map.note}` : "地図にある種類" },
          `地図: ${root.map.kind || "種類なし"}`)
        : root.map_inside ? h("span", { text: `地図: 中に ${root.map_inside} 行` })
        : !root.parent && !root.once_until ? h("button", { type: "button", class: "map-link", text: "地図に追加",
            title: "資料の地図にこのフォルダの行を足します。AI はこの地図を見て検索するフォルダを選びます",
            onclick: (e) => { e.stopPropagation(); addToMap(root); } }) : null,
      match && match.at.length ? h("span", { class: "where" }, marked(root.path, match.at)) : h("span", { class: "where", text: shortPath(root.path) }),
    );
    parts.pill.className = `pill ${status.cls}`;
    parts.pill.textContent = status.text;
    parts.pill.title = status.title || "";
    parts.el.classList.toggle("has-changes", Boolean(status.changes));
    const reader = readerOf();
    const reading = status.cls === "reading" && reader;
    parts.bar.hidden = !(reading || requested.has(keyOf(root.path)));  // not while waiting behind another folder
    parts.bar.classList.toggle("scanning", !reading || !reader.total);
    parts.barFill.style.width = reading && reader.total ? `${Math.min(100, (reader.done / reader.total) * 100)}%` : "";
    parts.action.textContent = "更新";
    parts.action.hidden = !status.changes;
    parts.action.className = "btn primary";
    parts.action.setAttribute("aria-label", `「${root.name}」を更新`);
    // the details only when what they show changed: rebuilding them every second would reset
    // the scroll of the unreadable list and the caret in the pattern field
    if (!expanded.has(keyOf(root.path))) return;
    const errors = loaded.get(keyOf(root.path));
    if (errors && errors.count !== root.errors) {
      loaded.delete(keyOf(root.path));
      api.unreadable(root.path).then((r) => { loaded.set(keyOf(root.path), r); parts.sig = ""; fillRow(parts.root, parts); })
        .catch(failed("読めないファイルを表示できませんでした"));
    }
    const sig = JSON.stringify([root.excludes, root.errors, Boolean(loaded.get(keyOf(root.path))), costs.get(keyOf(root.path)) || null]);
    if (sig !== parts.sig) { parts.sig = sig; detailsOf(root, parts); }
  };

  const checkedWhen = (t) => {
    const when = ago(t, Date.now() / 1000);
    return when === "たった今" ? "たった今、変更を確認しました" : `${when}に変更を確認しました`;
  };

  const filterInput = $("#filter");
  let active = null;  // the key of the row the arrow keys are on
  let shown = [];     // the parts of the rows shown, in order

  const render = () => {
    if (!data) return;
    const list = $("#folders");
    const query = filterInput.value.trim();
    const keep = new Set();
    const matched = [];
    for (const root of data.roots) {
      const parts = rowFor(root);
      keep.add(keyOf(root.path));
      parts.match = query ? fuzzy(query, root.path) : null;
      if (!query || parts.match) matched.push(parts);
    }
    if (query) matched.sort((a, b) => b.match.score - a.match.score);
    else {
      // outer folders in order, each followed by its inner folders when opened
      const byKey = new Map(matched.map((p) => [keyOf(p.root.path), p]));
      const tree = [];
      const visit = (p) => {
        tree.push(p);
        if (openKids.has(keyOf(p.root.path))) kidsOf(p.root).forEach((r) => byKey.has(keyOf(r.path)) && visit(byKey.get(keyOf(r.path))));
      };
      matched.filter((p) => !p.root.parent).forEach(visit);
      matched.splice(0, matched.length, ...tree);
    }
    for (const parts of matched) fillRow(parts.root, parts);
    shown = matched;
    if (query && !shown.some((p) => keyOf(p.root.path) === active)) active = shown.length ? keyOf(shown[0].root.path) : null;
    if (!query) active = null;
    for (const parts of rows.values()) parts.el.classList.toggle("active", keyOf(parts.root.path) === active);
    const order = shown.map((p) => p.el);
    $("#app").classList.toggle("filtering", Boolean(query));
    const total = data.roots.length;
    $("#filter-box").hidden = total < 2;
    $("#filter-count").textContent = query ? `${shown.length} / ${total} フォルダ` : "";
    $("#no-match").hidden = !(query && !shown.length);
    $("#no-match").textContent = `「${query}」に合うフォルダはありません。Esc で絞り込みを消せます`;
    for (const path of query ? data.pending.filter((p) => fuzzy(query, p)) : data.pending) {
      order.push(h("li", { class: "folder" }, h("div", { class: "row" }, h("span"),
        h("div", { class: "ident" }, h("span", { class: "name", text: base(path) }),
          h("div", { class: "facts" }, h("span", { text: "未更新" }), h("span", { class: "where", text: shortPath(path) }))),
        h("div", { class: "pill-cell" }, h("span", { class: "pill queued", text: "順番待ち" })), h("span"))));
    }
    for (const key of rows.keys()) if (!keep.has(key)) rows.delete(key);
    list.replaceChildren(...order);

    const tops = data.roots.filter((r) => !r.parent);  // inner folders' files are counted in their outer folder
    const files = tops.reduce((n, r) => n + r.files, 0);
    const errors = tops.reduce((n, r) => n + r.errors, 0);
    $("#summary").textContent = data.roots.length
      ? `${fmt(files)} ファイルを検索できます（${tops.length} フォルダ）。` + (errors ? `読めないファイルが ${fmt(errors)} 件あります。` : "")
      : "検索できるフォルダはまだありません。";
    const done = [...checks.values()].filter((c) => c.state !== "checking");
    const busy = [...checks.values()].some((c) => c.state === "checking");
    $("#checked").hidden = !data.roots.length;
    $("#checked-text").textContent = busy ? "変更を確認しています…"
      : done.length ? checkedWhen(Math.min(...done.map((c) => c.at)) / 1000) : "";
    $("#empty").hidden = data.roots.length > 0 || data.pending.length > 0;
    $("#head-actions").hidden = false;
    const changed = data.roots.filter((r) => !r.parent && statusOf(r).changes);
    const all = $("[data-act=update-all]");
    all.hidden = changed.length === 0;
    all.textContent = changed.length > 1 ? `変更のあるフォルダを更新（${changed.length}）` : "変更のあるフォルダを更新";
    renderBand();
    renderCosts();
  };

  // ---- the notes map: the user's file, edited in place and written only when saved
  const map = { loaded: false, version: null, items: [], saved: "", path: "" };
  const savedById = new Map();  // row id -> its content as last loaded or saved
  let nextId = 1;
  const contentOf = (it) => JSON.stringify(it.cells ? it.cells : it.comment);
  const payload = () => map.items
    .filter((it) => !it.cells || Object.values(it.cells).some((v) => String(v).trim()))  // a blank row is dropped
    .map((it) => (it.cells ? { cells: it.cells } : { comment: it.comment }));
  const mapDirty = () => map.loaded && JSON.stringify(payload()) !== map.saved;
  const mapFilter = $("#map-filter");

  const markOf = (st) => {
    if (!st) return null;
    // only what needs a look is marked; a row in order says nothing
    if (st.kind === "folder") return st.indexed ? null : { text: "インデックスにないフォルダです", warn: true, add: true };
    if (st.kind === "file") return st.indexed ? null : { text: "入っているフォルダがインデックスにありません", warn: true };
    if (st.kind === "wiki") return st.known ? null : { text: "この PC に追加されていない wiki です", warn: true };
    if (st.kind === "missing") return { text: "見つかりません", warn: true };
    if (st.kind === "relative") return { text: "C:\\ や \\\\ から始まる絶対パスで書いてください", warn: true };
    return null;
  };

  const updateDirty = () => {
    const dirty = mapDirty();
    $("#map-unsaved").hidden = !dirty;
    $("#tab-map").classList.toggle("unsaved", dirty);
  };
  const touched = (it, tr) => {
    tr.classList.toggle("edited", contentOf(it) !== savedById.get(it.id));
    updateDirty();
  };

  const removeRow = (it) => {
    const at = map.items.indexOf(it);
    if (at === -1) return;
    map.items.splice(at, 1);
    renderMap();
    updateDirty();
    toast("行を削除しました（保存するまでファイルは変わりません）", { label: "元に戻す", run: () => {
      map.items.splice(Math.min(at, map.items.length), 0, it);
      renderMap();
      updateDirty();
    } });
  };

  const mapRow = (it) => {
    const tr = h("tr", { class: contentOf(it) !== savedById.get(it.id) ? "edited" : null });
    const del = h("button", { type: "button", class: "icon danger", text: "×", title: "行を削除", "aria-label": "行を削除",
      onclick: () => removeRow(it) });
    if (!it.cells) {
      tr.classList.add("comment");
      const input = h("input", { type: "text", "aria-label": "メモの行（# で始まる行）" });
      input.value = it.comment;
      input.addEventListener("input", () => { it.comment = input.value; touched(it, tr); });
      tr.append(h("td", { colspan: "4" }, input), h("td", {}, h("div", { class: "ops" }, del)));
      return tr;
    }
    const cell = (name, label, extra) => {
      const input = h("input", { type: "text", "aria-label": label, ...extra });
      input.value = it.cells[name] || "";
      input.addEventListener("input", () => { it.cells[name] = input.value; touched(it, tr); });
      return input;
    };
    const fresh = !savedById.has(it.id);
    const path = cell("path", "場所", { list: "map-paths", spellcheck: "false", placeholder: fresh ? "D:\\資料\\仕様書 や wiki:名前" : null });
    // shown as the folder's name over a short path (full paths all start alike); typed into once clicked
    const shown = h("button", { type: "button", class: "shown" });
    const showPath = () => {
      const v = path.value.trim();
      shown.classList.toggle("blank", !v);
      shown.title = v ? `${v}\nクリックで編集` : "";
      put(shown, v ? [h("b", { text: v.toLowerCase().startsWith("wiki:") ? v : base(v) }),
        v.toLowerCase().startsWith("wiki:") ? null : h("small", { text: shortPath(v) })] : ["場所を入力"]);
    };
    const editing = (on) => { path.hidden = !on; shown.hidden = on; if (on) { path.focus(); path.select(); } else showPath(); };
    shown.addEventListener("click", () => editing(true));
    path.addEventListener("blur", () => { if (path.value.trim()) editing(false); });
    path.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === "Escape") { e.preventDefault(); path.blur(); } });
    const mark = h("span", { class: "mark" });
    const saved = () => contentOf(it) === savedById.get(it.id);
    const showMark = () => {
      const m = markOf(it.status);
      mark.hidden = !m;
      if (!m) return;
      mark.className = `mark${m.warn ? " warn" : ""}`;
      put(mark, m.text, m.add && saved() ? h("button", { type: "button", text: "インデックスに追加",
        onclick: () => api.index_note(it.cells.path).then(() => {
          toast("インデックスに追加しました。読み込みが終わる前でも、読めた分から検索できます");
          refresh();
          loadMap(true);
        }).catch(failed("インデックスに追加できませんでした")) }) : null);
    };
    path.addEventListener("change", () => api.describe(path.value).then((st) => { it.status = st; showMark(); }).catch(() => {}));
    const pick = h("button", { type: "button", class: "icon", text: "…", title: "フォルダを選ぶ", "aria-label": "フォルダを選ぶ",
      onclick: () => api.pick("folder").then((chosen) => {
        if (!chosen) return;
        path.value = chosen;
        it.cells.path = chosen;
        touched(it, tr);
        editing(false);
        path.dispatchEvent(new Event("change"));
      }).catch(failed("フォルダを選べませんでした")) });
    const open = h("button", { type: "button", class: "icon", text: "↗", title: "エクスプローラーで開く", "aria-label": "エクスプローラーで開く",
      onclick: () => {
        if (!saved()) { toast("保存してから開いてください"); return; }
        api.reveal_note(it.cells.path).catch(failed("開けませんでした"));
      } });
    showMark();
    const typing = fresh && !it.cells.path;
    path.hidden = !typing;
    shown.hidden = typing;
    showPath();
    const isFile = it.status && it.status.kind === "file";
    tr.append(
      h("td", {}, h("div", { class: "path-cell" }, shown, path, pick), mark),
      h("td", {}, cell("section", "見出し・位置", { placeholder: isFile || fresh ? "例: 3.2、p.12" : null })),
      h("td", {}, cell("kind", "種類", { list: "map-kinds", placeholder: fresh ? "仕様書 など" : null })),
      h("td", {}, cell("note", "メモ", { placeholder: fresh ? "検索の手がかりになる言葉" : null })),
      h("td", {}, h("div", { class: "ops" }, it.status && ["folder", "file"].includes(it.status.kind) ? open : null, del)));
    return tr;
  };

  const renderMap = () => {
    const query = mapFilter.value.trim();
    const rowsShown = map.items.filter((it) => !query || fuzzy(query, it.cells ? Object.values(it.cells).join(" ") : it.comment));
    put($("#map-rows"), rowsShown.map(mapRow));
    const rows = map.items.filter((it) => it.cells);
    const count = (kind) => rows.filter((it) => it.status && it.status.kind === kind).length;
    const warns = rows.filter((it) => (markOf(it.status) || {}).warn).length;
    $("#map-empty").hidden = map.items.length > 0;
    $("#map-wrap").hidden = !rowsShown.length;
    $("#map-hint").hidden = !map.items.length;
    $("#map-filter-box").hidden = map.items.length < 6;
    $("#map-filter-count").textContent = query ? `${rowsShown.length} / ${map.items.length} 行` : "";
    $("#map-no-match").hidden = !(query && !rowsShown.length);
    $("#map-no-match").textContent = `「${query}」に合う行はありません。Esc で絞り込みを消せます`;
    $("#map-summary").textContent = rows.length
      ? `${fmt(rows.length)} 行（フォルダ ${count("folder")}、ファイル ${count("file")}、wiki ${count("wiki")}）。`
        + (warns ? `確かめたほうがよい行が ${warns} 行あります。` : "AI はこの地図を見て、検索するフォルダを選びます。")
      : "まだ行がありません。";
    $("#map-summary").title = map.path;
  };

  const applyMap = (data) => {
    map.loaded = true;
    map.version = data.version;
    map.path = data.path;
    savedById.clear();
    map.items = data.items.map((raw) => {
      const it = { id: nextId++, cells: raw.cells ? { ...raw.cells } : null, comment: raw.comment, status: raw.status || null };
      savedById.set(it.id, contentOf(it));
      return it;
    });
    map.saved = JSON.stringify(payload());
    const kinds = new Set(["仕様書", "設計書", "マニュアル"]);
    map.items.forEach((it) => it.cells && it.cells.kind && kinds.add(it.cells.kind.trim()));
    put($("#map-kinds"), [...kinds].filter(Boolean).map((k) => h("option", { value: k })));
    put($("#map-paths"), [...data.roots, ...data.wikis].map((p) => h("option", { value: p })));
    renderMap();
    updateDirty();
  };
  const loadMap = (force) => {
    if (!force && mapDirty()) return Promise.resolve();
    return api.notes().then(applyMap).catch(failed("地図を読み込めませんでした"));
  };
  const saveMap = () => {
    if (!mapDirty()) return;
    api.save_notes(payload(), map.version).then((data) => {
      applyMap(data);
      toast("地図を保存しました");
    }).catch((e) => {
      const message = reason(e);
      if (/ほかで変更/.test(message)) toast(message, { label: "読み込み直す", run: () => loadMap(true) });
      else toast(`保存できませんでした: ${message}`);
    });
  };
  const addMapRow = () => {
    if (mapFilter.value) mapFilter.value = "";
    const it = { id: nextId++, cells: { path: "", section: "", kind: "", note: "" }, status: null };
    map.items.push(it);
    renderMap();
    updateDirty();
    const inputs = $("#map-rows").querySelectorAll("tr:last-child input");
    if (inputs.length) { inputs[0].focus(); inputs[0].scrollIntoView({ block: "nearest" }); }
  };
  // from a folder's row: a map row for it, its kind to be typed
  const addToMap = (root) => {
    showTab("map");
    loadMap(false).then(() => {
      if (mapFilter.value) mapFilter.value = "";
      const it = { id: nextId++, cells: { path: root.path, section: "", kind: "", note: "" }, status: { kind: "folder", indexed: true } };
      map.items.push(it);
      renderMap();
      updateDirty();
      const kind = $("#map-rows").querySelector("tr:last-child input[list=map-kinds]");
      if (kind) { kind.focus(); kind.scrollIntoView({ block: "nearest" }); }
      toast(`「${label(root)}」の行を足しました。種類を書いて保存してください`);
    });
  };
  mapFilter.addEventListener("input", renderMap);
  mapFilter.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { if (mapFilter.value) { mapFilter.value = ""; renderMap(); } else mapFilter.blur(); e.preventDefault(); }
  });

  // ---- search: the index the agent searches, for a person
  const q = $("#q");
  const scopeSel = $("#scope");
  const switches = { all: $("#opt-all"), name: $("#opt-name"), regex: $("#opt-regex"), variants: $("#opt-variants") };
  let searchSeq = 0;
  let results = null;  // the last answer: {hits, terms, facets, ...} or {error}
  let activeHit = 0;
  let searchTimer = 0;
  let facet = { kind: "", ext: "", folder: "" };  // narrowing chosen among the hits
  let previewSeq = 0;
  let previewTimer = 0;
  const fillScopes = () => api.search_scopes().then((s) => {
    const keep = scopeSel.value;
    put(scopeSel, h("option", { value: "", text: "すべてのフォルダ" }),
      s.kinds.length ? h("optgroup", { label: "地図の種類" },
        s.kinds.map((k) => h("option", { value: `kind:${k.kind}`, text: `${k.kind}（地図の ${k.folders} フォルダ）` }))) : null,
      h("optgroup", { label: "フォルダ" }, s.roots.map((r) => h("option", { value: r.path, text: r.name, title: r.path }))));
    if ([...scopeSel.options].some((o) => o.value === keep)) scopeSel.value = keep;
  }).catch(() => {});
  // text with every term marked, case ignored
  const markTerms = (text, terms) => {
    const low = String(text).toLowerCase();
    const on = new Array(low.length).fill(false);
    for (const t of terms) {
      const f = String(t).toLowerCase();
      for (let i = f ? low.indexOf(f) : -1; i !== -1; i = low.indexOf(f, i + 1)) on.fill(true, i, i + f.length);
    }
    return marked(String(text), on.flatMap((x, i) => (x ? [i] : [])));
  };
  // shown only past a moment, so a quick search does not flash it
  let busyTimer = 0;
  const busy = (on) => {
    clearTimeout(busyTimer);
    const mark = (x) => {
      $("#search-bar-box").classList.toggle("busy", x);
      for (const id of ["#search-body", "#facets", "#search-note"]) $(id).classList.toggle("stale", x);
      $("#results").setAttribute("aria-busy", String(x));
    };
    if (on) busyTimer = setTimeout(() => mark(true), 300);
    else mark(false);
  };
  const allTerms = () => (results ? [...(results.terms || []), ...(results.variants || [])] : []);
  const runSearch = (keepFacet) => {
    clearTimeout(searchTimer);
    if (!keepFacet) facet = { kind: "", ext: "", folder: "" };
    const text = q.value.trim();
    const mine = ++searchSeq;  // an answer to an older query is dropped
    if (!text) { busy(false); results = null; renderResults(); return; }
    $("#search-meta").textContent = "探しています…";
    busy(true);
    api.search({ text, scope: scopeSel.value, all: switches.all.checked, name: switches.name.checked,
      regex: switches.regex.checked, variants: switches.variants.checked, facet })
      .then((r) => { if (mine === searchSeq) { busy(false); results = r; activeHit = 0; renderResults(); } })
      .catch((e) => { if (mine === searchSeq) { busy(false); results = { error: reason(e), hits: [] }; renderResults(); } });
  };
  const viewHits = (numbers) => api.open_hits(numbers, q.value.trim())
    .then(() => toast("読み取り画面で開きました"))
    .catch(failed("読み取り画面で開けませんでした"));
  const openFile = (n) => api.open_file(n)
    .then(() => toast("ファイルを開きました"))
    .catch(failed("ファイルを開けませんでした"));
  const citation = (hit) => {
    const name = base(hit.path);
    const section = hit.crumb || (hit.title !== name ? hit.title : "");
    return `${name}${section ? `「${section}」` : ""}${hit.location ? ` ${hit.location}` : ""}`;
  };
  const copyCitation = (hit) => api.copy(citation(hit))
    .then(() => toast(`出典をコピーしました: ${citation(hit)}`))
    .catch(failed("コピーできませんでした"));
  const pendingIn = (roots) => {
    // changes the folder tab found in the searched folders, not read yet: the answer may lie there
    let n = 0;
    for (const root of data ? data.roots.filter((r) => !r.parent) : []) {
      const inScope = roots.some((p) => { const k = keyOf(p), r = keyOf(root.path); return k === r || within(k, r) || within(r, k); });
      const c = checks.get(keyOf(root.path));
      if (inScope && c && c.state === "done" && c.result.exists) n += c.result.new + c.result.changed;
    }
    return n;
  };

  // narrowing buttons: the hits by kind in the map, by format and by folder, with their counts
  const FACETS = [["kind", "種類"], ["ext", "形式"], ["folder", "フォルダ"]];
  const renderFacets = () => {
    const box = $("#facets");
    const counts = (results && results.facets) || {};
    // a group is shown when it can narrow: two values or more, or one value that some hits lack
    const narrows = (k) => (counts[k] || []).length > 1 || ((counts[k] || []).length === 1 && counts[k][0].count < results.total);
    const groups = FACETS.filter(([k]) => narrows(k) || facet[k]).map(([k, label]) =>
      h("div", { class: "facet", role: "group", "aria-label": label }, h("span", { class: "facet-label", text: label }),
        (counts[k] || []).slice(0, 8).map((x) => h("button", { type: "button", class: "chip-btn", "aria-pressed": String(facet[k] === x.value),
          onclick: () => { facet = { ...facet, [k]: facet[k] === x.value ? "" : x.value }; runSearch(true); } },
          x.value, h("small", { text: ` ${fmt(x.count)}` })))));
    if (Object.values(facet).some(Boolean)) {
      groups.push(h("button", { type: "button", class: "link", text: "絞り込みをやめる",
        onclick: () => { facet = { kind: "", ext: "", folder: "" }; runSearch(true); } }));
    }
    put(box, groups);
    box.hidden = !groups.length;
  };

  // the pane shows the hit's text, or its page as a picture; the choice is kept for next time
  let pvMode = "text";
  ready.then(() => api.preview_mode()).then((m) => { pvMode = m; }).catch(() => {});
  const setMode = (m) => {
    pvMode = m;
    api.preview_mode(m).catch(() => {});  // kept in the settings file for the next window
    renderPreview();
  };

  // the page as a picture: a PDF's own page, or a Word or PowerPoint file's once exported to
  // PDF. Exporting starts Office and takes seconds, so it waits for a click; an export made
  // before is shown at once
  const pageView = (hit, mine, pane, head) => {
    const box = h("div", { class: "pv-page" }, h("p", { class: "pv-loading", text: "ページを描いています…" }));
    put(pane, head, box);
    const draw = (convert) => api.page_image(hit.n, convert).then((r) => {
      if (mine !== previewSeq || r.stale) return;
      if (r.src) {
        put(box, h("img", { class: "pv-img", src: r.src, alt: `${base(hit.path)} の ${r.page} ページ目`,
          title: "クリックで根拠を見る", onclick: () => viewHits([hit.n]) }),
          h("p", { class: "pv-cap", text: `${r.page} / ${r.pages} ページ${r.exported ? "（PDF に書き出したもの）" : ""}` }));
      } else if (r.convert) {
        const go = h("button", { type: "button", class: "btn", text: "ページの画像を作る" });
        go.addEventListener("click", () => {
          go.disabled = true;
          go.textContent = "Office で PDF にしています…";
          draw(true);
        });
        put(box, h("p", { class: "pv-loading", text: `${r.app} で PDF に書き出して描きます。初めてのファイルは数秒かかります。` }), go);
      } else {
        put(box, h("p", { class: "pv-loading", text: r.note || "このファイルのページは描けませんでした。本文に切り替えて見てください。" }));
      }
    }).catch((e) => { if (mine === previewSeq) put(box, h("p", { class: "pv-loading", text: `描けませんでした: ${reason(e)}` })); });
    draw(false);
  };

  // the pane beside the results: the chosen hit's section with those around it, or its page
  const renderPreview = () => {
    const pane = $("#preview");
    const hit = results && results.hits ? results.hits[activeHit] : null;
    if (!hit) { pane.replaceChildren(); return; }
    clearTimeout(previewTimer);
    const mine = ++previewSeq;
    const modes = h("div", { class: "pv-mode", role: "group", "aria-label": "右の表示" });
    const head = h("header", { class: "pv-head" },
      h("h2", { class: "pv-name", text: base(hit.path), title: hit.path }),
      h("p", { class: "pv-where", text: [hit.crumb || "", hit.location].filter(Boolean).join("　") }),
      h("p", { class: "pv-path", text: `${shortPath(hit.path)}　${hit.modified}`, title: hit.path }),
      hit.newer_version ? h("p", { class: "r-flag", text: `新しい版があります: ${base(hit.newer_version)}` }) : null,
      h("div", { class: "pv-ops" },
        hit.chunk_id ? h("button", { type: "button", class: "btn primary", text: "根拠を見る", title: "Enter",
          onclick: () => viewHits([hit.n]) }) : null,
        h("button", { type: "button", class: "btn", text: "ファイルを開く", onclick: () => openFile(hit.n) }),
        h("button", { type: "button", class: "btn", text: "出典をコピー", title: citation(hit), onclick: () => copyCitation(hit) }),
        modes));
    put(pane, head, h("p", { class: "pv-loading", text: "読み込んでいます…" }));
    // moving through the list quickly asks only for the hit it stops on
    previewTimer = setTimeout(() => api.preview(hit.n).then((p) => {
      if (mine !== previewSeq) return;
      const mode = p.pages ? pvMode : "text";
      put(modes, [["text", "本文"], ["page", "ページ"]].map(([m, label]) => h("button", {
        type: "button", class: "seg", text: label, "aria-pressed": String(mode === m),
        disabled: m === "page" && !p.pages ? "" : null,
        title: m === "page" ? (p.pages ? "ページを画像で見ます。検索語に印が付きます" : "この形式にはページがありません") : "節の文字を見ます",
        onclick: () => { if (m !== mode) setMode(m); } })));
      if (mode === "page") { pageView(hit, mine, pane, head); return; }
      const terms = allTerms().concat(hit.terms);
      // a heading only where the file has one: a file's name as its only "title" says nothing new
      const heading = (sec) => sec.crumb || (sec.title !== base(hit.path) ? sec.title : "");
      // a section's text starts with its own heading: shown once, above it
      const lines = (sec) => {
        const all = sec.body.split("\n");
        const own = heading(sec).split(" > ").pop().trim();
        return own && all[0].trim() === own ? all.slice(1) : all;
      };
      const body = p.sections.length ? p.sections.map((sec) => h("section", { class: `pv-sec${sec.current ? " current" : ""}` },
        heading(sec) ? h("h3", { text: heading(sec) }) : null,
        h("div", { class: "pv-text" }, lines(sec).map((line) => h("p", {}, markTerms(line.split("\t").join("　｜　"), terms))))))
        : [h("p", { class: "pv-loading", text: p.note || "この結果には本文がありません（ファイル名で見つけたもの）。ファイルを開いて見てください。" })];
      put(pane, head, body);
      const current = pane.querySelector(".pv-sec.current");
      if (current) pane.scrollTop = current.offsetTop - head.offsetHeight - 12;
    }).catch((e) => { if (mine === previewSeq) put(pane, head, h("p", { class: "pv-loading", text: `表示できませんでした: ${reason(e)}` })); }), 120);
  };

  const selectHit = (i, scroll) => {
    activeHit = i;
    [...$("#results").children].forEach((el, j) => { el.classList.toggle("active", j === i); el.setAttribute("aria-selected", String(j === i)); });
    const el = $("#results").children[i];
    if (el && scroll) el.scrollIntoView({ block: "nearest" });
    renderPreview();
  };

  const renderResults = () => {
    const list = $("#results");
    const note = $("#search-note");
    $("#search-tips").hidden = Boolean(results);
    $("#search-body").hidden = !results || Boolean(results.error) || !results.hits.length;
    $("#search-more").hidden = true;
    note.hidden = true;
    note.className = "search-note";
    if (!results) { list.replaceChildren(); $("#search-meta").textContent = ""; renderFacets(); return; }
    if (results.error) {
      list.replaceChildren();
      $("#search-meta").textContent = "";
      note.hidden = false;
      note.classList.add("error");
      note.textContent = results.error;
      renderFacets();
      return;
    }
    const hits = results.hits;
    const shown = hits.length < results.total ? `（上位 ${fmt(hits.length)} 件を表示）` : "";
    $("#search-meta").textContent = results.total
      ? `${fmt(results.total)}${results.pooled ? " 件以上" : " 件"}${shown}　${fmt(results.ms)} ミリ秒` : "見つかりません";
    const notes = [];
    if (results.variants && results.variants.length) notes.push(`言い換えも探しました: ${results.variants.join("、")}`);
    const pending = pendingIn(results.roots || []);
    if (pending) notes.push(`探した範囲に、まだ読み込んでいない変更が ${fmt(pending)} 件あります。フォルダのタブで更新すると、それも探せます。`);
    if (readerOf()) notes.push("裏で読み込み中です。読み込めた分から探しています。");
    if (results.missing) notes.push(`地図のこの種類のうち ${results.missing} フォルダはインデックスにないため、探していません。`);
    if (results.exclude && results.exclude.length) notes.push(`除外: ${results.exclude.join("、")}`);
    notes.push(...(results.hints || []));
    if (!hits.length && !results.roots.length) notes.push("探すフォルダがありません。フォルダのタブで追加してください。");
    if (notes.length) { note.hidden = false; note.replaceChildren(...notes.map((t) => h("span", { text: t }))); }
    renderFacets();
    const terms = allTerms();
    put(list, hits.map((hit, i) => {
      const name = base(hit.path);
      const heading = hit.crumb || (hit.title !== name ? hit.title : "");
      const item = h("li", { class: `result${i === activeHit ? " active" : ""}`, role: "option", "aria-selected": String(i === activeHit) },
        h("div", { class: "r-head" }, h("span", { class: "r-name", text: name, title: hit.path }),
          h("span", { class: "r-where", text: [heading, hit.location].filter(Boolean).join("　") })),
        h("p", { class: "r-snippet" }, markTerms(hit.snippet, terms.concat(hit.terms))),
        h("span", { class: "r-path", text: `${shortPath(hit.path)}　${hit.modified}` }),
        hit.newer_version ? h("span", { class: "r-flag", text: `新しい版があります: ${base(hit.newer_version)}` }) : null);
      item.addEventListener("click", () => selectHit(i, false));
      item.addEventListener("dblclick", () => (hit.chunk_id ? viewHits([hit.n]) : openFile(hit.n)));
      return item;
    }));
    $("#search-more").hidden = hits.filter((x) => x.chunk_id).length < 2;
    renderPreview();
  };
  q.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => runSearch(false), 250); });
  scopeSel.addEventListener("change", () => runSearch(false));
  Object.values(switches).forEach((box) => box.addEventListener("change", () => runSearch(false)));
  q.addEventListener("keydown", (e) => {
    const hits = results && results.hits ? results.hits : [];
    if (e.key === "Enter") {
      e.preventDefault();
      if (!results || results.error || !hits.length) { runSearch(false); return; }
      const hit = hits[activeHit];
      if (hit) (hit.chunk_id ? viewHits([hit.n]) : openFile(hit.n));
    } else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && hits.length) {
      e.preventDefault();
      selectHit(Math.max(0, Math.min(hits.length - 1, activeHit + (e.key === "ArrowDown" ? 1 : -1))), true);
    } else if (e.key === "c" && e.ctrlKey && e.shiftKey && hits[activeHit]) {
      e.preventDefault();
      copyCitation(hits[activeHit]);
    } else if (e.key === "Escape" && q.value) {
      e.preventDefault();
      q.value = "";
      runSearch(false);
    }
  });

  // ---- tabs
  const tabButtons = { folders: $("#tab-folders"), map: $("#tab-map"), search: $("#tab-search") };
  let tab = "folders";
  const showTab = (name) => {
    if (!tabButtons[name]) return;
    tab = name;
    for (const [k, b] of Object.entries(tabButtons)) {
      const on = k === name;
      b.setAttribute("aria-selected", String(on));
      b.tabIndex = on ? 0 : -1;
      $(`#panel-${k}`).hidden = !on;
    }
    if (name === "map") loadMap(false);
    if (name === "search") { fillScopes(); q.focus(); }
    try { history.replaceState(null, "", `#${name}`); } catch (e) { /* the hash only remembers the tab */ }
  };
  window.__docwhereTab = showTab;
  $("#tabs").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) showTab(b.dataset.tab); });
  $("#tabs").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const order = Object.keys(tabButtons);
    const next = order[(order.indexOf(tab) + (e.key === "ArrowRight" ? 1 : order.length - 1)) % order.length];
    showTab(next);
    tabButtons[next].focus();
    e.preventDefault();
  });

  // ---- polling: every second while reading, every few seconds otherwise
  let timer = 0;
  const refresh = async () => {
    clearTimeout(timer);
    try {
      const next = await api.overview();
      const before = data;
      data = next;
      if (!before) recheckAll();
      // a folder the reader just finished (or a reader that just ended): look at it again
      const current = next.reader && next.reader.current;
      const finished = [];
      if (lastCurrent && keyOf(lastCurrent) !== keyOf(current || "")) { queueCheck(lastCurrent); finished.push(keyOf(lastCurrent)); }
      lastCurrent = current || null;
      // tell when each folder asked for here is done, however quickly it went
      let told = false;
      for (const [key, req] of requested) {
        if (handedOver(key)) { req.seen = true; continue; }
        if (req.seen || Date.now() - req.at > 1500) {
          requested.delete(key);
          checks.delete(key);
          const root = next.roots.find((r) => keyOf(r.path) === key);
          if (root) queueCheck(root.path);
          finished.push(key);
          toast(`「${req.name}」の更新が終わりました`);
          told = true;
        }
      }
      if (before && before.reader && !next.reader && !told) toast("更新が終わりました");
      if (!before || finished.length || (before.reader && !next.reader)) loadTotals();
      for (const key of finished) {
        costs.delete(key);
        const parts = rows.get(key);
        if (parts && expanded.has(key)) loadCost(parts.root, parts);
      }
      for (const root of next.roots) if (!checks.has(keyOf(root.path)) && root.exists && !statusOf(root).busy) queueCheck(root.path);
      render();
    } catch (e) {
      toast(`インデックスを読み込めませんでした: ${reason(e)}`);
    } finally {
      timer = setTimeout(refresh, requested.size ? 500 : data && data.reader ? 1000 : 4000);
    }
  };

  document.addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]");
    if (!act) return;
    const what = act.dataset.act;
    if (what === "add") addFolder();
    else if (what === "recheck") { recheckAll(); loadTotals(); toast("変更を確認しています"); }
    else if (what === "synonyms") {
      api.open_synonyms().then(() => toast("言い換えのファイルを開きました。保存すると、次の検索から使います")).catch(failed("開けませんでした"));
    }
    else if (what === "view-all") viewHits(results.hits.filter((x) => x.chunk_id).slice(0, 10).map((x) => x.n));
    else if (what === "map-add") addMapRow();
    else if (what === "map-save") saveMap();
    else if (what === "map-revert") loadMap(true).then(() => toast("変更を取り消しました"));
    else if (what === "map-excel") {
      api.open_notes().then(() => toast("Excel で開きました。Excel で開いている間は、ここから保存できません。Excel で保存したら、この窓に戻ると読み込み直します"))
        .catch(failed("開けませんでした"));
    }
    else if (what === "update-all") update(data.roots.filter((r) => !r.parent && statusOf(r).changes).map((r) => r.path));
  });
  filterInput.addEventListener("input", () => render());
  filterInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      if (filterInput.value) { filterInput.value = ""; render(); } else filterInput.blur();
      e.preventDefault();
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!shown.length) return;
      e.preventDefault();
      const i = shown.findIndex((p) => keyOf(p.root.path) === active);
      const next = shown[Math.max(0, Math.min(shown.length - 1, (i === -1 ? -1 : i) + (e.key === "ArrowDown" ? 1 : -1)))];
      active = keyOf(next.root.path);
      for (const parts of shown) parts.el.classList.toggle("active", keyOf(parts.root.path) === active);
      next.el.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      const target = shown.find((p) => keyOf(p.root.path) === active) || shown[0];
      if (target) { e.preventDefault(); toggle(target.root, target); }
    }
  });
  document.addEventListener("keydown", (e) => {
    const typing = e.target.closest("input, textarea, [contenteditable]") || $("#confirm").open;
    if (e.key === "s" && e.ctrlKey && tab === "map") { e.preventDefault(); saveMap(); return; }
    if ((e.key === "/" && !typing) || (e.key === "f" && e.ctrlKey)) {
      const box = tab === "map" ? $("#map-filter-box") : tab === "search" ? $("#search-bar-box") : $("#filter-box");
      const input = tab === "map" ? mapFilter : tab === "search" ? q : filterInput;
      if (box.hidden) return;
      e.preventDefault();
      input.focus();
      input.select();
    }
  });

  // coming back to the window: look again at folders checked over a minute ago
  window.addEventListener("focus", () => {
    if (tab === "map") loadMap(false);  // edited in Excel meanwhile: shown unless edits here are unsaved
    if (!data) return;
    const stale = Date.now() - 60000;
    data.roots.forEach((r) => { const c = checks.get(keyOf(r.path)); if (c && c.state !== "checking" && c.at < stale) queueCheck(r.path); });
  });
  // the page may only show itself: no link leads anywhere
  document.addEventListener("click", (e) => { if (e.target.closest("a[href]")) e.preventDefault(); }, true);

  $("#tabs").hidden = false;
  ready.then(() => { showTab(["#map", "#search"].includes(location.hash) ? location.hash.slice(1) : "folders"); return refresh(); }).catch((e) => { $("#summary").textContent = `この窓を開き直してください（${reason(e)}）`; });
})();
"""
