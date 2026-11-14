import os
import time

from docwhere import rendition
from docwhere.cli import main


def test_reused_export_counts_as_recently_used(tmp_path, monkeypatch):
    doc = tmp_path / "spec.docx"
    doc.write_bytes(b"x")
    made = []
    monkeypatch.setattr(rendition, "_export_apart", lambda src, out, ext: made.append(out) or bool(out.write_bytes(b"%PDF")))
    cache = tmp_path / "renditions"
    first = rendition.as_pdf(str(doc), cache)
    old = time.time() - 20 * 86400
    os.utime(first, (old, old))
    again = rendition.as_pdf(str(doc), cache)
    assert again == first and len(made) == 1  # served from the cache, not exported again
    assert time.time() - again.stat().st_mtime < 60  # and its age restarts from this use


def test_excerpts_have_their_own_pool(tmp_path):
    import pymupdf

    pdf = tmp_path / "m.pdf"
    doc = pymupdf.open()
    for i in range(30):
        doc.new_page().insert_text((72, 72), f"page {i}")
    doc.save(pdf)
    renditions = tmp_path / "renditions"
    renditions.mkdir()
    export = renditions / "0123456789abcdef0123.pdf"
    export.write_bytes(b"%PDF")
    legacy = renditions / "x-legacy.pdf"
    legacy.write_bytes(b"%PDF")
    excerpts = rendition.Excerpts(renditions)
    path, _ = excerpts.link(str(pdf), 15)
    excerpts.close()
    assert path.parent == renditions / "excerpts"
    assert export.exists() and not legacy.exists()  # exports untouched; old-style excerpts cleared


def test_status_flags_gone_folders_and_forget_frees_them(docs, tmp_path, capsys, monkeypatch):
    import shutil

    monkeypatch.setenv("DOCWHERE_DB", str(tmp_path / "idx" / "index.db"))
    gone = tmp_path / "gone"
    shutil.copytree(docs, gone)
    assert main(["index", str(docs), str(gone)]) == 0
    shutil.rmtree(gone)
    capsys.readouterr()
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "folder no longer exists" in out and "docwhere forget" in out and str(gone) in out
    assert main(["forget", str(gone)]) == 0
    assert "index removed" in capsys.readouterr().out
    main(["status"])
    out = capsys.readouterr().out
    assert str(gone) not in out and str(docs.resolve()) in out
