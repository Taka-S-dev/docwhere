"""The index manager: a window that shows every indexed folder, what changed in it since it was
read and the background reading as it goes, and lets the user read a folder again, stop the
reading, add a folder, change what is left out, rebuild a folder or take it out of the index.

It is a process and a window of its own (``docwhere manage``), so the viewer window stays as
it is while reading goes on. The safety rules are the viewer's: the page is a static file that
docwhere wrote and signed, its CSP forbids scripts, and the manager script is injected from
outside only while that page is showing. What the page may ask of Python is ``ManageApi``;
every method acts only on folders and files that are in the index, and the reading itself is
the background reader that searches use, so the window never reads files on its own.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from multiprocessing import AuthenticationError
from multiprocessing.connection import Listener
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from docwhere import background
from docwhere.index import Index, path_key, prefix_of
from docwhere.settings import load_settings, private, save_settings

KIND = "manage"  # the window's pipe and settings are kept apart from the viewer's
STORAGE = "webview-manage"
PAGE_NAME = "manage.html"
DEFAULT_SIZE = (1040, 800)
START_TIMEOUT = 15.0
MAX_UNREADABLE = 200  # unreadable files listed per folder; the count is always given
SLOWEST_SHOWN = 5  # files that took longest, listed per folder


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


def open_manager(db: Path) -> bool:
    """Show the manager window, starting it if none is running. False when the window cannot
    be used here (no pywebview, or an index folder that others can write to)."""
    from docwhere import window

    if not window.available() or not private(db.parent):
        return False
    write_page(db)
    window.allow_foreground()
    if window._send(db, {"show": True}, KIND):
        return True
    background.spawn_detached(["--db", str(db), "manage-window"])
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(0.3)
        if window._send(db, {"ping": True}, KIND):
            return True
    return False


# ---- what the page may ask -------------------------------------------------------------------


def _bridge(method):
    from docwhere.window import _logged

    return _logged(method)


class ManageApi:
    """Methods the manager script may call. Attributes are private so pywebview exposes only these.
    Each call opens its own index connection: the bridge runs every call on a thread of its own."""

    def __init__(self, db: Path) -> None:
        self._db = db
        self._window = None
        self._trusted = False  # set while the signed manager page is showing

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

    @_bridge
    def overview(self) -> dict:
        index = self._index()
        try:
            roots = index.roots()
            keys = {path_key(Path(r["path"])): r["path"] for r in roots}
            out = []
            for r in roots:
                key = path_key(Path(r["path"]))
                parent = next((p for k, p in keys.items() if k != key and key.startswith(prefix_of(k))), None)
                out.append({
                    **r,
                    "name": Path(r["path"]).name or r["path"],
                    "parent": parent,
                    "exists": Path(r["path"]).is_dir(),
                })
            queued = [q["path"] for q in index.db.execute("SELECT path FROM queue ORDER BY requested_at")]
            reader = background.state(index)
            state = None
            if reader is not None:
                state = {
                    "current": reader.current, "done": reader.done, "total": reader.total, "rate": reader.rate(),
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

    @_bridge
    def changes(self, path: str) -> dict:
        index = self._index()
        try:
            return {"path": str(self._root(index, path)), **index.changes(self._root(index, path))}
        finally:
            index.close()

    @_bridge
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

    @_bridge
    def update(self, paths: list) -> dict:
        """Hand the folders to the background reader (it reads only what changed)."""
        index = self._index()
        try:
            roots = [self._root(index, p) for p in paths[:100]]
            for root in roots:
                background.request(index, root)
            return {"queued": [str(r) for r in roots]}
        finally:
            index.close()

    @_bridge
    def stop(self) -> dict:
        index = self._index()
        try:
            return {"stopped": background.stop(index)}
        finally:
            index.close()

    @_bridge
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
                return {"added": None, "known": str(folder)}
            background.request(index, folder)
            return {"added": str(folder)}
        finally:
            index.close()

    @_bridge
    def add_exclude(self, path: str, pattern: str) -> dict:
        index = self._index()
        try:
            root = self._root(index, path)
            added, dropped = index.add_excludes(root, [str(pattern)[:200]])
            return {"added": added, "dropped": dropped}
        finally:
            index.close()

    @_bridge
    def remove_exclude(self, path: str, pattern: str) -> dict:
        index = self._index()
        try:
            removed, _ = index.remove_excludes(self._root(index, path), [str(pattern)])
            return {"removed": removed}
        finally:
            index.close()

    @_bridge
    def rebuild(self, path: str) -> dict:
        """Read the folder again from nothing, keeping what it leaves out."""
        index = self._index()
        try:
            root = self._root(index, path)
            index.forget(root, keep_excludes=True)
            background.request(index, root)
            return {"queued": str(root)}
        finally:
            index.close()

    @_bridge
    def forget(self, path: str) -> dict:
        index = self._index()
        try:
            root = self._root(index, path)
            index.forget(root)
            return {"forgotten": str(root)}
        finally:
            index.close()

    @_bridge
    def unreadable(self, path: str) -> dict:
        index = self._index()
        try:
            errors = index.errors([self._root(index, path)])
            return {"count": len(errors), "files": [{"path": p, "error": e} for p, e in errors[:MAX_UNREADABLE]]}
        finally:
            index.close()

    @_bridge
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


# ---- the window process ----------------------------------------------------------------------


def run(db: Path) -> None:
    """The manager window process: show the page and come to the front when asked again."""
    import pywintypes
    import webview

    from docwhere import window as viewer_window

    if not private(db.parent):
        return
    page = write_page(db).resolve()
    try:
        listener = Listener(viewer_window._address(db, KIND), family="AF_PIPE", authkey=viewer_window._key(db))
    except OSError:
        viewer_window._send(db, {"show": True}, KIND)  # another manager took the pipe first
        return
    viewer_window._setup_log(db)  # bridge errors and slow calls go to window.log, as the viewer's do
    log = viewer_window._log
    log.info("index window started")
    win32_errors = (OSError, pywintypes.error)

    geometry = viewer_window._usable_geometry(load_settings(db).get("manage_window") or {})
    api = ManageApi(db)
    win = webview.create_window(
        f"{viewer_window.title(db)} - インデックス", url=page.as_uri(), js_api=api,
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
            if (hwnd := viewer_window._own_window()) is not None:
                save_settings(db, manage_window=viewer_window._placement(hwnd))
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
            try:
                conn.recv_bytes(viewer_window.MAX_MESSAGE)
                conn.send_bytes(b"ok")
            except (OSError, EOFError):
                log.exception("answering a request for the index window failed")
            finally:
                conn.close()
            try:
                viewer_window._bring_to_front(False)
            except win32_errors:
                log.exception("raising the index window failed")

    win.events.before_load += distrust
    win.events.loaded += inject
    win.events.shown += lambda: viewer_window._bring_to_front(False)
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
<dialog id="confirm"><form method="dialog" class="confirm">
  <h2 id="confirm-title"></h2><p id="confirm-text"></p>
  <div class="buttons"><button value="cancel" class="btn">やめる</button><button value="ok" class="btn" id="confirm-ok"></button></div>
</form></dialog>"""

STYLE = """
* { box-sizing: border-box; }
[hidden] { display: none !important; }  /* the flex and grid boxes below would otherwise show */
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.6 "BIZ UDPGothic", "Yu Gothic UI", "Meiryo", sans-serif; }
.wrap { max-width: 1000px; margin: 0 auto; padding: 24px 24px 80px; }
button { font: inherit; }
.btn { padding: 5px 14px; color: var(--ink); background: var(--surface); border: 1px solid var(--rule);
  border-radius: 6px; cursor: pointer; white-space: nowrap; }
.btn:hover:not(:disabled) { border-color: var(--muted); }
.btn:disabled { opacity: .45; cursor: default; }
.btn.primary { color: var(--surface); background: var(--blue); border-color: var(--blue); }
.btn.danger { color: var(--warn); }
.btn.quiet { border-color: transparent; background: transparent; color: var(--blue); padding: 3px 6px; }
:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; border-radius: 4px; }

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
.folder.child .row { padding-left: 32px; }
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
.folder.child .details { padding-left: 68px; }
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
    if (reader && reader.current && keyOf(reader.current) === key) {
      const pct = reader.total ? Math.floor((reader.done / reader.total) * 100) : 0;
      return { cls: "reading", text: reader.total ? `更新中 ${pct}%` : "変更を確認中", busy: true };
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
    const key = keyOf(path);
    if (waiting.some((p) => keyOf(p) === key)) return;
    checks.set(key, { state: "checking", at: Date.now() });
    waiting.push(path);
    pump();
  };
  const pump = () => {
    while (checking < 2 && waiting.length) {
      const path = waiting.shift();
      checking++;
      api.changes(path)
        .then((result) => checks.set(keyOf(path), { state: "done", result, at: Date.now() }))
        .catch((e) => checks.set(keyOf(path), { state: "error", result: reason(e), at: Date.now() }))
        .finally(() => { checking--; render(); pump(); });
    }
    render();
  };
  const recheckAll = () => { if (data) data.roots.filter((r) => r.exists).forEach((r) => queueCheck(r.path)); };

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
    const ok = await confirmAction(`「${root.name}」をインデックスから外しますか？`,
      "このフォルダは検索の対象から外れます。ファイル自体は削除されません。\nこのフォルダに設定した除外も消えます。", "外す", true);
    if (!ok) return;
    api.forget(root.path).then(() => {
      expanded.delete(keyOf(root.path));
      toast(`「${root.name}」をインデックスから外しました`);
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
    const pace = reader.rate ? `残り約 ${duration(left / reader.rate)}（毎秒 ${reader.rate >= 10 ? fmt(Math.round(reader.rate)) : reader.rate.toFixed(1)} 件）` : (scanning ? "対象のファイルを数えています" : "残り時間を計算しています");
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
    parts.el.classList.toggle("child", Boolean(root.parent));
    const match = parts.match;
    const nameAt = root.path.replace(/[\\/]+$/, "").length - root.name.length;
    if (match && match.at.length) put(parts.name, marked(root.name, match.at, nameAt));
    else parts.name.textContent = root.name;
    parts.row.title = root.path;
    put(parts.facts,
      h("span", { text: `${fmt(root.files)} ファイル` }),
      root.errors ? h("span", { class: "bad", text: `読めない ${fmt(root.errors)} 件` }) : null,
      h("span", { text: `最終更新 ${ago(root.synced_at, data.now)}` }),
      root.excludes.length ? h("span", { text: `除外 ${root.excludes.length} 件` }) : null,
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

    const files = data.roots.reduce((n, r) => n + r.files, 0);
    const errors = data.roots.reduce((n, r) => n + r.errors, 0);
    $("#summary").textContent = data.roots.length
      ? `${fmt(files)} ファイルを検索できます（${data.roots.length} フォルダ）。` + (errors ? `読めないファイルが ${fmt(errors)} 件あります。` : "")
      : "検索できるフォルダはまだありません。";
    const done = [...checks.values()].filter((c) => c.state !== "checking");
    const busy = [...checks.values()].some((c) => c.state === "checking");
    $("#checked").hidden = !data.roots.length;
    $("#checked-text").textContent = busy ? "変更を確認しています…"
      : done.length ? checkedWhen(Math.min(...done.map((c) => c.at)) / 1000) : "";
    $("#empty").hidden = data.roots.length > 0 || data.pending.length > 0;
    $("#head-actions").hidden = false;
    const changed = data.roots.filter((r) => statusOf(r).changes);
    const all = $("[data-act=update-all]");
    all.hidden = changed.length === 0;
    all.textContent = changed.length > 1 ? `変更のあるフォルダを更新（${changed.length}）` : "変更のあるフォルダを更新";
    renderBand();
    renderCosts();
  };

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
    else if (what === "update-all") update(data.roots.filter((r) => statusOf(r).changes).map((r) => r.path));
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
    if ((e.key === "/" && !typing) || (e.key === "f" && e.ctrlKey)) {
      if ($("#filter-box").hidden) return;
      e.preventDefault();
      filterInput.focus();
      filterInput.select();
    }
  });

  // coming back to the window: look again at folders checked over a minute ago
  window.addEventListener("focus", () => {
    if (!data) return;
    const stale = Date.now() - 60000;
    data.roots.forEach((r) => { const c = checks.get(keyOf(r.path)); if (c && c.state !== "checking" && c.at < stale) queueCheck(r.path); });
  });
  // the page may only show itself: no link leads anywhere
  document.addEventListener("click", (e) => { if (e.target.closest("a[href]")) e.preventDefault(); }, true);

  ready.then(refresh).catch((e) => { $("#summary").textContent = `この窓を開き直してください（${reason(e)}）`; });
})();
"""
