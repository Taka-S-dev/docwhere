"""PDF renditions of Word and PowerPoint files, so the viewer can show their pages.

Word files hold no pages: pagination exists only once Word lays the document out. The viewer
therefore has Word (or PowerPoint) export the file to PDF and shows that, so page numbers
match what Word would print. Exports are cached by path, size and modification time and
pruned like the other generated files. Office runs as a private instance with macros
force-disabled (see extractors.legacy); without Office there is no rendition and the viewer
falls back to text.
"""

from __future__ import annotations

import hashlib
import os
import unicodedata
from pathlib import Path

WORD_EXTS = {".docx", ".docm", ".doc"}
SLIDE_EXTS = {".pptx", ".pptm", ".ppt"}
# Exports take Word seconds to make, so they are kept long; excerpts are cut in half a second
# and kept in a pool of their own, so a flurry of them never pushes an export out. Ages count
# from the last use: a cached file is touched whenever it is reused.
RENDITION_RETENTION = {"min_keep": 5, "max_keep": 100, "max_age": 30 * 24 * 3600, "max_bytes": 500 * 1024 * 1024}
EXCERPT_RETENTION = {"min_keep": 10, "max_keep": 200, "max_age": 7 * 24 * 3600, "max_bytes": 300 * 1024 * 1024}
EXPORT_PATTERN = "[0-9a-f]*.pdf"  # exports are named by a hex digest; excerpts live in excerpts/
EXPORT_TIMEOUT = 120  # seconds Office may take over one export before it is stopped with its process


def _touch(path: Path) -> Path:
    try:
        os.utime(path)
    except OSError:
        pass
    return path


def as_pdf(path: str, cache: Path, export: bool = True) -> Path | None:
    """A PDF of the Word or PowerPoint file at ``path``, exported once and then reused.
    With ``export`` false only an export made before is returned (Office is not started)."""
    src = Path(path)
    ext = src.suffix.lower()
    if ext not in WORD_EXTS | SLIDE_EXTS:
        return None
    try:
        st = src.stat()
    except OSError:
        return None
    key = hashlib.sha1(f"{src.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:20]
    out = cache / f"{key}.pdf"
    if out.exists():
        return _touch(out)
    if not export:
        return None
    cache.mkdir(parents=True, exist_ok=True)
    if not _export_apart(src, out, ext) or not out.exists():
        out.unlink(missing_ok=True)
        return None  # no Office, the file would not open, or Office stopped answering
    from docwhere.viewer import prune

    prune(cache, EXPORT_PATTERN, **RENDITION_RETENTION)
    return out if out.exists() else None


def _export_apart(src: Path, out: Path, ext: str) -> bool:
    """Export in a worker process of its own, as files are read (see docwhere.workers): Office
    is stopped with the worker at the timeout or when this process ends, and a process that
    lives on, such as the index window, never keeps an Office instance between calls."""
    from docwhere import workers

    for outcome in workers.run([workers.Task(str(src), True, None, (str(out), ext))], _export_job, 1, EXPORT_TIMEOUT):
        return outcome.failure is None and bool(outcome.result)
    return False


def _export_job(path: str, out: str, ext: str) -> bool:
    """The worker's half of ``_export_apart``. False when Office could not export the file."""
    try:
        _export(Path(path), Path(out), ext)
    except Exception:
        Path(out).unlink(missing_ok=True)
        return False
    return True


def _export(src: Path, out: Path, ext: str) -> None:
    from docwhere.extractors.legacy import (
        _app,
        keep_personal_information,
        open_word_document,
    )

    if ext in WORD_EXTS:
        doc = open_word_document(src)
        try:
            doc.ExportAsFixedFormat(str(out), 17)  # wdExportFormatPDF
        finally:
            doc.Close(SaveChanges=0)
    else:
        app = _app("PowerPoint.Application")
        pres = app.Presentations.Open(str(src.resolve()), ReadOnly=True, Untitled=False, WithWindow=False)
        keep_personal_information(pres)
        try:
            pres.SaveAs(str(out), 32)  # ppSaveAsPDF
        finally:
            pres.Close()


EXCERPT_SPAN = 10  # pages kept either side of the linked page
EXCERPT_FORMAT = 2  # bump when excerpts are cut differently, so cached ones are cut again


def _relink(doc, part, pdf: str, first: int, last: int) -> None:
    """Recreate the excerpt's links, which copying pages does not keep.

    Manuals link their cross-references ("127ページ ...") through named destinations that
    live in the source file. A link to a page inside the excerpt is pointed at that page of
    the excerpt; one to a page outside opens the full PDF at that page, which is slow to load
    but never a dead link. Web links are kept as they are.
    """
    import pymupdf

    whole = Path(pdf).resolve().as_uri()
    for i, pno in enumerate(range(first - 1, last)):
        for link in doc[pno].get_links():
            target = link.get("page", -1)
            if link["kind"] in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED) and target is not None and target >= 0:
                if first - 1 <= target <= last - 1:
                    new = {"kind": pymupdf.LINK_GOTO, "from": link["from"], "page": target - (first - 1),
                           "to": link.get("to") or pymupdf.Point(0, 0), "zoom": 0}
                else:
                    new = {"kind": pymupdf.LINK_URI, "from": link["from"], "uri": f"{whole}#page={target + 1}"}
            elif link["kind"] == pymupdf.LINK_URI and link.get("uri"):
                new = {"kind": pymupdf.LINK_URI, "from": link["from"], "uri": link["uri"]}
            else:
                continue
            part[i].insert_link(new)


class Excerpts:
    """Small PDFs holding a page and its neighbours, for links from the viewer.

    A browser's PDF viewer loads the whole file before showing any page, which takes seconds
    for a manual of thousands of pages. A link therefore opens an excerpt of the pages around
    its target, cut once and cached; page labels keep the original numbers. Each source PDF is
    opened at most once per page build.
    """

    def __init__(self, renditions: Path) -> None:
        self.cache = renditions / "excerpts"
        self._legacy = renditions  # excerpts made before they had a folder of their own
        self._docs: dict[str, object] = {}
        self._made: dict[tuple[str, int, int], Path | None] = {}

    def link(self, pdf: str, page: int) -> tuple[Path, int] | None:
        """The excerpt holding ``page`` of ``pdf`` and that page's number within it."""
        import pymupdf

        try:
            doc = self._docs.get(pdf) or self._docs.setdefault(pdf, pymupdf.open(pdf))
        except Exception:
            return None
        first = max(1, page - EXCERPT_SPAN)
        last = min(doc.page_count, page + EXCERPT_SPAN)
        if not first <= page <= last:
            return None
        key = (pdf, first, last)
        if key not in self._made:
            self._made[key] = self._cut(doc, pdf, first, last)
        out = self._made[key]
        return (out, page - first + 1) if out else None

    def _cut(self, doc, pdf: str, first: int, last: int) -> Path | None:
        import pymupdf

        try:
            st = Path(pdf).stat()
        except OSError:
            return None
        key = f"{EXCERPT_FORMAT}|{Path(pdf).resolve()}|{st.st_size}|{st.st_mtime_ns}|{first}-{last}"
        out = self.cache / f"x-{hashlib.sha1(key.encode()).hexdigest()[:20]}.pdf"
        if out.exists():
            return _touch(out)
        self.cache.mkdir(parents=True, exist_ok=True)
        try:
            part = pymupdf.open()
            part.insert_pdf(doc, from_page=first - 1, to_page=last - 1, links=False)
            _relink(doc, part, pdf, first, last)
            part.set_page_labels([{"startpage": 0, "prefix": "", "style": "D", "firstpagenum": first}])
            part.save(out, garbage=3, deflate=True)
            part.close()
        except Exception:
            out.unlink(missing_ok=True)
            return None
        from docwhere.viewer import prune

        prune(self.cache, "x-*.pdf", **EXCERPT_RETENTION)
        prune(self._legacy, "x-*.pdf", min_keep=0, max_keep=0, max_age=0, max_bytes=0)
        return out

    def close(self) -> None:
        for doc in self._docs.values():
            doc.close()
        self._docs.clear()


def page_with(pdf: Path, text: str, probe_chars: int = 24) -> int | None:
    """The first page of ``pdf`` whose text contains the start of ``text`` (1-based).

    Compared NFKC-folded, lower-cased and without whitespace, as the index stores text.
    """
    import pymupdf

    probe = _fold(text)[:probe_chars]
    if not probe:
        return None
    with pymupdf.open(pdf) as doc:
        for i, page in enumerate(doc, 1):
            if probe in _fold(page.get_text("text")):
                return i
    return None


def _fold(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).lower().split())
