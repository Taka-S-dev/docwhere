import json
import threading
from multiprocessing.connection import Listener

import pytest

from docwhere import window
from docwhere.cli import main


@pytest.fixture
def api(docs, tmp_path, capsys, monkeypatch):
    db = tmp_path / "cli" / "index.db"
    monkeypatch.setenv("DOCWHERE_DB", str(db))
    assert main(["search", "config", "--in", str(docs), "--json"]) == 0
    capsys.readouterr()
    return _trusted(window.Api(db)), docs, db


def _trusted(api):
    """As when a signed docwhere page is showing; see test_bridge_refuses_unsigned_pages."""
    api._trusted = True
    return api


def test_pages_and_links_only_for_indexed_pdfs(api, tmp_path):
    api, docs, _ = api
    pdf = str(docs / "sub" / "設計書.pdf")
    assert api.page(pdf, 2, ["config"], []).startswith("data:image/jpeg;base64,")
    assert api.links(pdf, 1)["pages"] == 3
    stranger = tmp_path / "elsewhere.pdf"
    stranger.write_bytes((docs / "sub" / "設計書.pdf").read_bytes())
    with pytest.raises(PermissionError):
        api.page(str(stranger), 1, [], [])
    with pytest.raises(PermissionError):
        api.links(str(docs / "運用マニュアル.docx"), 1)  # indexed, but not a PDF


def test_open_runs_only_for_indexed_files(api, monkeypatch, tmp_path):
    api, docs, _ = api
    from docwhere import opener

    opened = []
    monkeypatch.setattr(opener, "open_at", lambda path, anchor, pdf_app=None: opened.append((path, anchor)) or "opened")
    target = str(docs / "運用マニュアル.docx")
    assert api.open(json.dumps({"path": target, "anchor": {"term": "config"}})).endswith("opened")
    with pytest.raises(PermissionError):
        api.open(json.dumps({"path": str(tmp_path / "evil.bat"), "anchor": {}}))
    assert opened == [(target, {"term": "config"})]


def test_later_pages_are_handed_to_the_running_window_with_the_key(tmp_path):
    db = tmp_path / "index.db"
    received = []
    listener = Listener(window.pipe_address(db), family="AF_PIPE", authkey=window.pipe_key(db))

    def accept_one():
        conn = listener.accept()
        received.append(window.unpack(conn.recv_bytes(window.MAX_MESSAGE)))
        conn.send_bytes(b"ok")
        conn.close()

    thread = threading.Thread(target=accept_one, daemon=True)
    thread.start()
    assert window.send(db, {"page": "x.html"})
    thread.join(5)
    listener.close()
    assert received == [{"page": "x.html"}]
    assert not window.send(db, {"ping": True})  # nobody listening any more


def test_page_script_uses_the_bridge_not_the_network():
    from docwhere.viewer import APP_JS, STATIC_CSP

    assert "fetch(" not in APP_JS and "XMLHttpRequest" not in APP_JS and "pywebview.api" in APP_JS
    assert "script-src" not in STATIC_CSP  # default-src 'none': the page can run no script of its own


def test_viewer_mode_can_be_switched(tmp_path):
    db = tmp_path / "index.db"
    assert window.mode(db) == "window"
    window.set_mode(db, "browser")
    assert window.mode(db) == "browser"


def test_key_file_is_private_and_reused(tmp_path):
    import win32security

    db = tmp_path / "index.db"
    (tmp_path / "window.key").write_bytes(b"k" * window.KEY_BYTES)  # left by an older version, inherited ACL
    key = window.pipe_key(db)
    assert key == b"k" * window.KEY_BYTES and window.pipe_key(db) == key
    sd = win32security.GetFileSecurity(str(tmp_path / "window.key"), win32security.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    owner = win32security.GetTokenInformation(
        win32security.OpenProcessToken(__import__("win32api").GetCurrentProcess(), win32security.TOKEN_QUERY),
        win32security.TokenUser,
    )[0]
    assert dacl.GetAceCount() == 1 and dacl.GetAce(0)[2] == owner  # nobody but the owner


def test_pipe_messages_are_json_not_pickle():
    import pickle

    assert window.unpack(window._pack({"page": "x.html"})) == {"page": "x.html"}
    with pytest.raises(ValueError):
        window.unpack(pickle.dumps({"page": "x.html"}))


def test_window_title_names_no_tool_and_can_be_changed(tmp_path):
    db = tmp_path / "index.db"
    assert window.title(db) == "Reference" and "docwhere" not in window.title(db)
    window.set_title(db, "手順書ビューア")
    assert window.title(db) == "手順書ビューア"
    window.set_title(db, "  ")
    assert window.title(db) == "Reference"


def test_settings_round_trip_and_bad_entries_change_nothing(api, tmp_path, monkeypatch):
    api, docs, db = api
    monkeypatch.setattr(window, "_set_window_title", lambda text: None)
    exe = tmp_path / "Viewers" / "SumatraPDF.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    assert api.preset(str(exe)) == "-reuse-instance -page {page} {file}"
    assert api.preset(r"C:\x\Other.exe") == "{file}"
    assert api.preset(r"C:\tool\SumatraPDF-3.5.2-64.exe") == "-reuse-instance -page {page} {file}"  # portable build

    saved = api.save_settings({"pdf_program": str(exe), "pdf_args": api.preset(str(exe)), "title": "手順書", "viewer": "window"})
    assert saved == {"title": "手順書", "viewer": "window", "pdf_program": str(exe.resolve()),
                     "pdf_args": "-reuse-instance -page {page} {file}", "copy_caption": True,
                     "cite_path": "root", "count_pictures": True, "doc_reader": "word"}
    with pytest.raises(FileNotFoundError):
        api.save_settings({"pdf_program": str(tmp_path / "gone.exe"), "pdf_args": "{file}", "title": "x", "viewer": "browser"})
    assert api.settings() == saved  # nothing was half-saved

    api.save_settings({"pdf_program": "", "pdf_args": "", "title": "", "viewer": "browser"})
    assert api.settings() == {"title": "Reference", "viewer": "browser", "pdf_program": "", "pdf_args": "",
                              "copy_caption": True, "cite_path": "root", "count_pictures": True, "doc_reader": "word"}


def test_args_keep_windows_paths_and_quoted_parts():
    assert window._split_args(r'-page {page} -o "C:\My Docs\x.txt" {file}') == ["-page", "{page}", "-o", r"C:\My Docs\x.txt", "{file}"]


def test_try_pdf_uses_the_unsaved_program_on_an_indexed_pdf_only(api, tmp_path, monkeypatch):
    api, docs, db = api
    from docwhere import opener

    exe = tmp_path / "SumatraPDF.exe"
    exe.write_bytes(b"")
    launched = []
    monkeypatch.setattr(opener.subprocess, "Popen", lambda args: launched.append(args))
    pdf = str(docs / "sub" / "設計書.pdf")
    api.try_pdf(str(exe), "-page {page} {file}", pdf, 2)
    assert launched == [[str(exe.resolve()), "-page", "2", pdf]]
    assert window.pdf_app(db) == {}  # trying does not save
    with pytest.raises(PermissionError):
        api.try_pdf(str(exe), "{file}", str(tmp_path / "other.pdf"), 1)


def test_settings_button_is_live_only():
    from docwhere.viewer import RESULT_BODY

    assert 'data-settings data-live hidden' in RESULT_BODY


def test_saved_position_off_every_monitor_is_dropped():
    on_screen = {"width": 900, "height": 700, "x": 100, "y": 100, "maximized": True}
    assert window.usable_geometry(on_screen) == on_screen
    unplugged = {"width": 900, "height": 700, "x": 50000, "y": 50000, "maximized": False}
    assert window.usable_geometry(unplugged) == {"width": 900, "height": 700, "maximized": False}


def test_zoom_is_remembered_and_kept_in_range(api):
    api, docs, db = api
    assert api.zoom() == {"mode": "width", "scale": 1.0}
    assert api.save_zoom("fit", 1.5) == {"mode": "fit", "scale": 1.5}
    assert _trusted(window.Api(db)).zoom() == {"mode": "fit", "scale": 1.5}  # a new window gets it back
    assert api.save_zoom("bogus", 99)["scale"] == window.MAX_ZOOM


def test_page_resolution_follows_the_zoom(api):
    import base64

    import pymupdf

    api, docs, _ = api
    pdf = str(docs / "sub" / "設計書.pdf")
    widths = []
    for dpi in (0, 300, 10000):
        data = base64.b64decode(api.page(pdf, 1, [], [], dpi).split(",", 1)[1])
        widths.append(pymupdf.Pixmap(data).width)
    assert widths[0] < widths[1] == widths[2]  # sharper when zoomed, capped at MAX_DPI


def test_bridge_refuses_unsigned_pages(api):
    api, docs, db = api
    stranger = window.Api(db)  # no signed page showing
    for call in (lambda: stranger.settings(), lambda: stranger.links(str(docs / "sub" / "設計書.pdf"), 1),
                 lambda: stranger.save_settings({"pdf_program": "", "pdf_args": "", "title": "", "viewer": "window"})):
        with pytest.raises(PermissionError):
            call()


def test_pages_are_signed_and_tampering_shows(tmp_path):
    db = tmp_path / "index.db"
    page = tmp_path / "views" / "20990101-000000.html"
    page.parent.mkdir()
    page.write_text("<p>answer</p>", encoding="utf-8")
    assert not window.page_signed(db, page)  # never signed
    window.sign_page(db, page)
    assert window.page_signed(db, page)
    page.write_text("<p>answer</p><script>evil()</script>", encoding="utf-8")
    assert not window.page_signed(db, page)  # changed after signing


def test_saved_views_are_signed_and_signatures_pruned_with_them(tmp_path):
    from docwhere.viewer import save_view

    db = tmp_path / "index.db"
    folder = tmp_path / "views"
    out = save_view(folder, "<p>x</p>", "q", "a", sign=lambda p: window.sign_page(db, p))
    assert window.page_signed(db, out) and window.page_signed(db, folder / "index.html")
    orphan = folder / "20000101-000000.html.sig"  # its page was pruned earlier
    orphan.write_text("0" * 64, encoding="ascii")
    save_view(folder, "<p>y</p>", "q2", "a2", sign=lambda p: window.sign_page(db, p))
    assert not orphan.exists() and (folder / (out.name + ".sig")).exists()


@pytest.mark.parametrize("name", ["cmd.exe", "PowerShell.exe", "pwsh.exe", "mshta.exe", "python3.exe", "wscript.exe"])
def test_shells_and_script_hosts_cannot_be_the_pdf_app(tmp_path, name):
    exe = tmp_path / name
    exe.write_bytes(b"")
    with pytest.raises(PermissionError):
        window.checked_command([str(exe), "{file}"])


def test_shared_index_folder_gets_no_window(tmp_path, monkeypatch):
    db = tmp_path / "index.db"
    assert window.private(tmp_path)
    monkeypatch.setattr(window, "private", lambda folder: False)
    assert window.show(db, tmp_path / "views" / "x.html") is False
    with pytest.raises(PermissionError):
        window.pipe_key(db)
    assert not (tmp_path / "window.key").exists()


def test_folder_others_can_write_is_not_private(tmp_path, monkeypatch):
    import ntsecuritycon as nc
    import win32api
    import win32security

    monkeypatch.setenv("USERPROFILE", str(tmp_path / "someone-else"))  # judge by the ACL alone
    me = win32security.GetTokenInformation(
        win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY),
        win32security.TokenUser,
    )[0]
    shared = tmp_path / "shared"
    shared.mkdir()

    def grant(*sids):
        dacl = win32security.ACL()
        for sid, mask in sids:
            dacl.AddAccessAllowedAce(win32security.ACL_REVISION, mask, sid)
        sd = win32security.SECURITY_DESCRIPTOR()
        sd.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(
            str(shared), win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION, sd)

    everyone = win32security.ConvertStringSidToSid("S-1-1-0")
    grant((me, nc.FILE_ALL_ACCESS))
    assert window.private(shared)
    grant((me, nc.FILE_ALL_ACCESS), (everyone, nc.FILE_GENERIC_READ | nc.FILE_GENERIC_EXECUTE))
    assert window.private(shared)  # others may read
    grant((me, nc.FILE_ALL_ACCESS), (everyone, nc.FILE_ADD_FILE))
    assert not window.private(shared)  # but not put files there


def test_settings_in_a_shared_index_folder_are_not_used(tmp_path, monkeypatch):
    db = tmp_path / "index.db"
    (tmp_path / "settings.json").write_text('{"pdf_app": {"command": ["C:/x/viewer.exe", "{file}"]}}', encoding="utf-8")
    assert window.pdf_app(db)["command"][0] == "C:/x/viewer.exe"
    monkeypatch.setattr("docwhere.settings.private", lambda folder: False)
    assert window.pdf_app(db) == {} and window.mode(db) == "window"


def test_webview_files_go_to_one_folder_emptied_at_each_start(tmp_path):
    db = tmp_path / "index.db"
    folder = window.fresh_storage(db)
    (folder / "EBWebView").mkdir()
    (folder / "EBWebView" / "cache").write_bytes(b"x" * 100)
    assert window.fresh_storage(db) == folder and list(folder.iterdir()) == []


def test_a_page_or_a_region_of_it_is_copied_as_a_picture_with_its_source(api, tmp_path, monkeypatch):
    import io

    from PIL import Image

    api, docs, db = api
    pdf = str(docs / "sub" / "設計書.pdf")
    put = []
    monkeypatch.setattr(window, "_put_picture", lambda png, dib: put.append((png, dib)))  # not the user's clipboard
    whole = api.copy_page(pdf, 2, ["config"], [], None, pdf, ["CLI"], None)
    part = api.copy_page(pdf, 2, ["config"], [], [0.5, 0.5, 0.0, 0.0], pdf, ["CLI"], None)  # dragged up and left
    w, h = (int(x) for x in whole.split(" x "))
    pw, ph = (int(x) for x in part.split(" x "))
    assert abs(pw - w / 2) <= 1 and h > ph > (h - 40) / 2  # half the page, and the source under each
    png, dib = put[1]
    assert Image.open(io.BytesIO(png)).size == (pw, ph)
    assert int.from_bytes(dib[4:8], "little") == pw  # the bitmap's own header: its width
    # without the source when the settings say so
    window.save_settings_values(db, {"title": "", "viewer": "window", "copy_caption": False})
    assert window.settings_values(db)["copy_caption"] is False
    api.copy_page(pdf, 2, [], [], [0.0, 0.0, 0.5, 0.5], pdf, ["x"], None)
    plain = Image.open(io.BytesIO(put[2][0])).size
    assert plain[0] == pw and plain[1] < ph  # the same region, with no band under it
    with pytest.raises(ValueError, match="小さすぎ"):
        api.copy_page(pdf, 2, [], [], [0.2, 0.2, 0.2005, 0.5], pdf, [], None)
    with pytest.raises(PermissionError):
        api.copy_page(str(tmp_path / "elsewhere.pdf"), 1, [], [], None, "", [], None)


def test_a_source_names_its_file_as_the_settings_say(api, monkeypatch):
    from docwhere.settings import cite_name

    roots = [r"C:\data\手順書", r"C:\data\手順書\第2版"]
    path = r"C:\data\手順書\第2版\設計書.pdf"
    assert cite_name(path, "name", roots) == "設計書.pdf"
    assert cite_name(path, "root", roots) == r"手順書\第2版\設計書.pdf"  # from the outer folder, the inner one named
    assert cite_name(r"c:\DATA\手順書\a.pdf", "root", roots) == r"手順書\a.pdf"  # letter case aside
    assert cite_name(r"E:\x\a.pdf", "root", roots) == "a.pdf"  # in no folder of the index
    assert cite_name(path, "full", roots) == path
    # a copied page names the file it is from, from its indexed folder, as set
    api, docs, db = api
    pdf = str(docs / "sub" / "設計書.pdf")
    captions = []
    monkeypatch.setattr(window, "page_picture", lambda png, region, caption: captions.append(caption) or (b"", b"", (1, 1)))
    monkeypatch.setattr(window, "_put_picture", lambda png, dib: None)
    api.copy_page(pdf, 1, [], [], None, pdf, ["CLI"], None)
    window.save_settings_values(db, {"title": "", "viewer": "window", "cite_path": "name"})
    api.copy_page(pdf, 1, [], [], None, pdf, ["CLI"], None)
    assert captions == [rf"{docs.name}\sub\設計書.pdf「CLI」 p.1", "設計書.pdf「CLI」 p.1"]


def test_the_source_under_a_picture_takes_the_headings_and_page_as_chosen(api):
    trail = ["第1部 準備", "3 保存", "3.2 名前を付けて保存"]
    pick = lambda heading, page: window.picture_caption("a.pdf", trail, 104, 102, {"cite_heading": heading, "cite_page": page})
    assert pick("all", "both") == "a.pdf「第1部 準備 > 3 保存 > 3.2 名前を付けて保存」 p.104（印刷 102）"
    assert pick("last", "print") == "a.pdf「3.2 名前を付けて保存」 p.102"
    assert pick("none", "pdf") == "a.pdf p.104"
    assert window.picture_caption("a.pdf", [], 3, None, {"cite_heading": "last", "cite_page": "print"}) == "a.pdf p.3"
    # chosen beside the copy buttons: only these are saved, the PDF app and the title stay
    api, _, db = api
    window.save_settings(db, title="手順書", pdf_app={"command": ["x.exe", "{file}"]})
    saved = api.save_copy_options({"copy_caption": False, "cite_heading": "last", "cite_page": "print", "cite_path": "bogus"})
    assert saved == {"copy_caption": False, "cite_path": "root", "cite_heading": "last", "cite_page": "print"}
    assert window.title(db) == "手順書" and window.load_settings(db)["pdf_app"] == {"command": ["x.exe", "{file}"]}


def test_a_minimized_window_is_restored_before_it_is_raised(monkeypatch):
    import ctypes

    import win32con
    import win32gui

    from docwhere import window

    shown = []
    monkeypatch.setattr(window, "own_window", lambda: 4242)
    monkeypatch.setattr(window, "_set_topmost", lambda on: None)
    monkeypatch.setattr(win32gui, "IsIconic", lambda hwnd: True)
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 4242)  # already in front: nothing more to do
    monkeypatch.setattr(ctypes.windll.user32, "ShowWindowAsync", lambda hwnd, how: shown.append((hwnd, how)))
    window.bring_to_front(False)
    assert shown == [(4242, win32con.SW_RESTORE)]
