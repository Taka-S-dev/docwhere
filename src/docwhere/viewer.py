"""Hits as one local HTML page, and PDF pages as images with the terms and the answer marked.

The page is a single self-contained file: document text is escaped, images are inlined as
data: URIs and a Content-Security-Policy forbids scripts and every network request, so
nothing from the documents can run or leave the machine when the page is opened.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import time
import unicodedata
from pathlib import Path

from docwhere.index import Index
from docwhere.model import norm
from docwhere.search import CELL_SEP, location_label

STATIC_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'"  # no page script ever runs; see window.py
VIEW_DPI = 140  # the image fills about half the window; lower blurs 8pt table text on 150% displays
SNAP_DPI = 150  # sharp enough for small table text when the image is read back
JPEG_QUALITY = 75
MAX_PAGE_IMAGES = 20  # each page is ~150-300 KB inlined
DAY = 24 * 3600
MB = 1024 * 1024
# Saved pages and snapshots are pruned whenever a new one is written. The newest few always
# stay, so a page open in a browser tab is not deleted from under it; beyond those a file
# goes once there are too many, it is too old, or the folder has grown too heavy.
VIEW_RETENTION = {"min_keep": 10, "max_keep": 50, "max_age": 14 * DAY, "max_bytes": 200 * MB}
SNAP_RETENTION = {"min_keep": 5, "max_keep": 30, "max_age": 1 * DAY, "max_bytes": 100 * MB}
VIEW_PATTERN = "[0-9]*.html"  # result pages; index.html is the history page
TERM_COLOR = (0.74, 0.85, 1.0)  # pale blue wash when no answer is marked
TERM_UNDERLINE = (0.12, 0.31, 0.55)  # manual blue, once the answer is marked
QUOTE_COLOR = (1.0, 0.76, 0.1)  # highlighter
QUOTE_BORDER = (0.75, 0.42, 0.0)
MIN_QUOTE_LINE = 8  # a line this long that lies inside a multi-line quote is part of it
CLOSEUP_DPI = 220  # the close-up is a narrow strip, so it can be sharp and still small
CLOSEUP_MARGIN = 26  # points shown above and below the answer: a strip of a few lines, not a page


def render_page(
    path: str,
    page_no: int,
    terms: list[str],
    dpi: int,
    fmt: str = "png",
    quotes: list[str] = (),
    closeup: bool = False,
    source=None,
) -> bytes:
    """Page ``page_no`` (1-based) of a PDF with ``terms`` marked and each ``quotes`` highlighted.

    A quote is the sentence that answers the question, chosen by whoever read the hit; the
    terms only show why the page matched. With ``closeup`` only a strip of the page around
    the quotes is rendered (LookupError when none is on the page). The marks are added to
    an in-memory copy only; the file is never written. ``source`` is the PDF already open,
    for callers that keep it open: the page is copied out of it so it stays unmarked.
    """
    import pymupdf

    if source is not None:
        if not 1 <= page_no <= source.page_count:
            raise LookupError(f"{Path(path).name} has no page {page_no}")
        doc = pymupdf.open()
        doc.insert_pdf(source, from_page=page_no - 1, to_page=page_no - 1, links=False)
        page_no = 1
    else:
        doc = pymupdf.open(path)
    with doc:
        if not 1 <= page_no <= doc.page_count:
            raise LookupError(f"{Path(path).name} has no page {page_no}")
        page = doc[page_no - 1]
        quote_rects = _find(page, quotes)
        if closeup and not quote_rects:
            raise LookupError(f"the quote is not on p.{page_no}")
        for rects in _find(page, terms):
            for rect in rects:
                # once the answer is marked, terms are only underlined so the answer stands out
                mark = page.add_underline_annot(rect) if quotes else page.add_highlight_annot(rect)
                mark.set_colors(stroke=TERM_UNDERLINE if quotes else TERM_COLOR)
                mark.update()
        for rects in quote_rects:
            for rect in rects:
                mark = page.add_highlight_annot(rect)
                mark.set_colors(stroke=QUOTE_COLOR)
                mark.update()
            frame = page.add_rect_annot(_union(rects) + (-3, -3, 3, 3))
            frame.set_colors(stroke=QUOTE_BORDER)
            frame.set_border(width=1.2)
            frame.update()
        clip = None
        if closeup:
            around = _union([r for rects in quote_rects for r in rects])
            text = [pymupdf.Rect(b[:4]) for b in page.get_text("blocks")]
            left = min((r.x0 for r in text), default=page.rect.x0)
            right = max((r.x1 for r in text), default=page.rect.x1)
            top, bottom = around.y0 - CLOSEUP_MARGIN, around.y1 + CLOSEUP_MARGIN
            # move each edge out to the line it would cut through, so no line is sliced in half
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    y0, y1 = line["bbox"][1], line["bbox"][3]
                    if y0 < top < y1:
                        top = y0 - 4
                    if y0 < bottom < y1:
                        bottom = y1 + 4
            clip = pymupdf.Rect(left - 12, top, right + 12, bottom) & page.rect
        pix = page.get_pixmap(dpi=dpi, annots=True, clip=clip)
        return pix.tobytes("jpeg", jpg_quality=JPEG_QUALITY) if fmt == "jpeg" else pix.tobytes("png")


def _find(page, needles: list[str]) -> list[list]:
    """Every occurrence of each needle on the page, as one rectangle per text line it covers.

    Matching runs on NFKC-folded, lower-cased text without whitespace, the same folding the
    index applies, so a needle copied from a snippet is found even where the PDF itself
    holds full-width characters or breaks the sentence across lines.
    """
    import pymupdf

    chars, boxes, lines = [], [], []
    for block in page.get_text("rawdict")["blocks"]:
        for li, line in enumerate(block.get("lines", [])):
            for span in line["spans"]:
                for ch in span["chars"]:
                    for c in unicodedata.normalize("NFKC", ch["c"]).lower():
                        if not c.isspace():
                            chars.append(c)
                            boxes.append(ch["bbox"])
                            lines.append((block["number"], li))
    hay = "".join(chars)
    found = []
    for needle in needles:
        needle = "".join(unicodedata.normalize("NFKC", needle).lower().split())
        start = 0
        while needle and (i := hay.find(needle, start)) >= 0:
            per_line: dict[tuple, pymupdf.Rect] = {}
            for j in range(i, i + len(needle)):
                per_line[lines[j]] = per_line.get(lines[j], pymupdf.Rect(boxes[j])) | pymupdf.Rect(boxes[j])
            found.append(list(per_line.values()))
            start = i + len(needle)
    return found


def _union(rects: list):
    out = rects[0]
    for r in rects[1:]:
        out = out | r
    return out


def page_range(page: int, pages: int) -> range:
    """The hit page with ``pages`` pages either side (pages past either end are skipped later)."""
    return range(max(1, page - pages), page + pages + 1)


def build_html(
    index: Index,
    terms: list[str],
    hits: list[dict],
    context: int = 1,
    pages: int = 0,
    quotes: list[str] = (),
    answer: str = "",
    title: str = "",
    cut_excerpts: bool = True,
) -> str:
    """With ``cut_excerpts`` off (the page will be read in the docwhere window, which turns pages
    itself) PDF links point at the whole file and no excerpt is cut.

    ``quotes`` are the sentences that answer the question and ``answer`` a one-line summary,
    both from whoever read the hits. The page opens with the answer and, for each quote, where
    it is written with a close-up of it; the hits follow, each with its pages in page order.
    Every page that holds a quote is shown, so each piece of evidence links to its page."""
    quotes = [q for q in (norm(q) for q in quotes) if q]
    images_left = MAX_PAGE_IMAGES
    rendered: dict[tuple[str, int], str] = {}

    def page_image(path: str, p: int, hit_terms: list[str]) -> str:
        nonlocal images_left
        key = (path, p)
        if key not in rendered and images_left > 0:
            images_left -= 1
            try:
                rendered[key] = _data_uri(render_page(path, p, hit_terms, VIEW_DPI, "jpeg", quotes))
            except Exception:
                rendered[key] = ""  # past the last page, or the file changed since indexing
        return rendered.get(key, "")

    from docwhere.rendition import Excerpts

    renditions = index.path.with_name("renditions")
    excerpts = Excerpts(renditions) if cut_excerpts else None
    hits = [_paged(hit, renditions) for hit in hits]
    cards_by_path: dict[str, dict] = {}  # each file is shown once, in the card of its first hit
    for hit in hits:
        cards_by_path.setdefault(hit["path"], hit)
    evidence = [_locate(index, q, hits, cards_by_path) for q in quotes]
    evidence_pages: dict[int, set[int]] = {}
    evidence_rows: dict[int, set[tuple[str, int]]] = {}
    for ev in evidence:
        if ev["hit"] is not None and ev["page"]:
            evidence_pages.setdefault(ev["hit"]["n"], set()).add(ev["page"])
        if ev["hit"] is not None and ev["row"]:
            evidence_rows.setdefault(ev["hit"]["n"], set()).add(ev["row"])

    targets: dict[int, str] = {}  # hit number -> id of its hit page (or row) in this document
    shown: dict[int, set] = {}
    cards = []
    for hit in hits:
        page, n = hit["anchor"].get("page"), hit["n"]
        wanted = set()
        if page and hit.get("pdf"):
            wanted = set(page_range(page, pages)) | evidence_pages.get(n, set())
        page_images = [(p, src) for p in sorted(wanted) if (src := page_image(hit["pdf"], p, hit["terms"]))]
        shown[n] = {p for p, _ in page_images}
        targets[n] = _page_id(hit, page) if page in shown[n] else f"hit{n}"
        sheet = ""
        if _is_sheet(hit) and "row" in hit["anchor"]:
            rows = {(hit["anchor"]["sheet"], hit["anchor"]["row"])} | evidence_rows.get(n, set())
            sheet = _sheet_excerpt(index, hit, rows, quotes)
            shown[n] |= rows
            targets[n] = _row_id(hit, hit["anchor"]["sheet"], hit["anchor"]["row"])
        cards.append(_card(index, hit, page_images, context, quotes, sheet, evidence_pages.get(n, set()), excerpts))
    for ev in evidence:
        if ev["hit"] is not None:
            n = ev["hit"]["n"]
            if ev["row"] and ev["row"] in shown[n]:
                ev["target"] = _row_id(ev["hit"], *ev["row"])
            elif ev["page"] in shown[n]:
                ev["target"] = _page_id(ev["hit"], ev["page"])
            else:
                ev["target"] = f"hit{n}"

    answer_html = _answer_html(answer, evidence, excerpts)
    rail = _rail_html(bool(answer_html), evidence, hits, targets)
    shown_title = title or " ".join(terms)
    body = _RESULT_BODY.format(
        layout="layout has-rail" if rail else "layout",
        rail=rail,
        title=_e(shown_title),
        query=f'<span>検索語 {_e(" ".join(terms))}</span><span>{_e(time.strftime("%Y-%m-%d %H:%M"))}</span>',
        answer=answer_html,
        cards=(f'<h2 class="region">資料 {len(hits)} 件</h2>' + "".join(cards)) if cards
        else '<p class="empty">一致する箇所はありませんでした。言い方を変えて検索し直してください。</p>',
    )
    if excerpts is not None:
        excerpts.close()
    return _PAGE.format(csp=STATIC_CSP, title=_e(shown_title), style=_STYLE, body=body)


def save_view(folder: Path, page: str, title: str, answer: str, sign=None) -> Path:
    """Write ``page`` as a new file in ``folder`` and add it to the history page there.

    ``sign(path)``, when given, signs each page written (see window.sign_page) so the
    docwhere window will run its reader on it; signatures go with their pages when pruned.

    Every view gets its own file, so an earlier question's page stays open in its browser tab
    while the next one opens in a new tab. Older pages are pruned per VIEW_RETENTION.
    """
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out, i = folder / f"{stamp}.html", 2
    while out.exists():
        out, i = folder / f"{stamp}-{i}.html", i + 1
    out.write_text(page, encoding="utf-8")
    prune(folder, VIEW_PATTERN, **VIEW_RETENTION)

    log = folder / "history.json"
    try:
        history = json.loads(log.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        history = []
    history.insert(0, {"file": out.name, "title": title, "answer": answer, "when": time.strftime("%Y-%m-%d %H:%M")})
    history = [h for h in history if (folder / Path(h["file"]).name).exists()]
    log.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")

    items = "".join(
        f'<li><a href="{_e(Path(h["file"]).name)}"><span class="q">{_e(h["title"])}</span>'
        + (f'<span class="a"><span>{_e(h["answer"])}</span></span>' if h["answer"] else "")
        + f'<small>{_e(h["when"])}</small></a></li>'
        for h in history
    )
    body = _HISTORY_BODY.format(keep=VIEW_RETENTION["max_keep"], days=VIEW_RETENTION["max_age"] // DAY, items=items)
    (folder / "index.html").write_text(_PAGE.format(csp=STATIC_CSP, title="質問の履歴", style=_STYLE, body=body), encoding="utf-8")
    if sign is not None:
        sign(out)
        sign(folder / "index.html")
    for signature in folder.glob("*.sig"):
        if not signature.with_suffix("").exists():
            signature.unlink(missing_ok=True)
    return out


def prune(folder: Path, pattern: str, min_keep: int, max_keep: int, max_age: float, max_bytes: int) -> int:
    """Delete files matching ``pattern`` in ``folder``, newest first kept; return bytes freed.

    The newest ``min_keep`` always stay. After those, a file goes when it is past the
    ``max_keep``-th, older than ``max_age`` seconds, or would push the running total of the
    newer files past ``max_bytes``.
    """
    files = []
    for f in folder.glob(pattern):
        try:
            files.append((f, f.stat()))
        except OSError:
            continue
    files.sort(key=lambda fs: fs[1].st_mtime, reverse=True)
    now, total, freed = time.time(), 0, 0
    for i, (f, st) in enumerate(files):
        total += st.st_size
        if i < min_keep:
            continue
        if i >= max_keep or now - st.st_mtime > max_age or total > max_bytes:
            try:
                f.unlink()
                freed += st.st_size
                total -= st.st_size
            except OSError:
                pass  # open in another program; try again next time
    return freed


_OFFSETS: dict[tuple[str, int], int | None] = {}


def _page_text(hit: dict, page: int) -> str:
    """"p.120（印刷 118）" when the PDF's printed numbers differ from its page index."""
    from docwhere.folio import printed, printed_offset

    pdf = hit.get("pdf")
    if not pdf or hit.get("rendered_from"):
        return f"p.{page}"
    try:
        key = (pdf, Path(pdf).stat().st_mtime_ns)
        if key not in _OFFSETS:
            import pymupdf

            with pymupdf.open(pdf) as doc:
                _OFFSETS[key] = printed_offset(doc)
        number = printed(_OFFSETS[key], page)
    except Exception:
        number = None
    return f"p.{page}（印刷 {number}）" if number else f"p.{page}"


def _page_id(hit: dict, page: int) -> str:
    return f"hit{hit['n']}-p{page}"


def _row_id(hit: dict, sheet: str, row: int) -> str:
    digest = hashlib.sha1(sheet.encode()).hexdigest()[:6]  # sheet names may hold any character
    return f"hit{hit['n']}-s{digest}-r{row}"


def _data_uri(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


def _is_sheet(hit: dict) -> bool:
    return Path(hit["path"]).suffix.lower() in (".xlsx", ".xlsm")


def _paged(hit: dict, renditions: Path) -> dict:
    """``hit`` with ``pdf`` set to the PDF its pages are drawn from, and a page in its anchor.

    A PDF is its own; Word and PowerPoint files get an exported PDF (see rendition), in which
    the hit's page is where its text appears, or its slide number. Others are returned as is.
    """
    from docwhere.rendition import SLIDE_EXTS, WORD_EXTS, as_pdf, page_with

    ext = Path(hit["path"]).suffix.lower()
    if ext == ".pdf":
        return {**hit, "pdf": hit["path"]} if hit["anchor"].get("page") else hit
    if ext not in WORD_EXTS | SLIDE_EXTS or (pdf := as_pdf(hit["path"], renditions)) is None:
        return hit
    if ext in SLIDE_EXTS:
        page = hit["anchor"].get("slide") or 1
    else:
        page = page_with(pdf, _plain(hit["anchor"].get("text", ""))) or page_with(pdf, hit["title"]) or 1
    return {**hit, "pdf": str(pdf), "rendered_from": ext, "anchor": {**hit["anchor"], "page": page},
            "location": hit["location"] or f"p.{page}"}


def _plain(line: str) -> str:
    """A table row as indexed ("項目: 接続タイムアウト / 既定値: 30秒") back to its cell text, which
    is what the page itself shows; other lines are returned unchanged."""
    if " / " not in line and ": " not in line:
        return line
    return "".join(part.split(": ", 1)[-1] for part in line.split(" / "))


def _open_link(hit: dict, excerpts) -> str:
    """Links that open the page in the browser's PDF viewer, when there is a PDF to open.

    The main link opens an excerpt of the pages around it (see rendition.Excerpts), which
    loads at once; the whole file stays one click away for reading further. Word and Excel
    files get no link: a browser cannot hand a file:// link to Office, so it downloads a copy
    on every click. Their path is shown for copying instead (see _card).
    """
    page = hit["anchor"].get("page")
    if not (hit.get("pdf") and page):
        return ""
    label = "PDF にしたものをこのページで開く" if hit.get("rendered_from") else "PDF をこのページで開く"
    whole = f'{_e(Path(hit["pdf"]).as_uri())}#page={page}'
    part = excerpts.link(hit["pdf"], page) if excerpts is not None else None
    # static-only: a served page turns pages and opens the PDF application itself
    if part is None:
        return f'<a class="static-only" href="{whole}">{label}</a>'
    path, at = part
    return (
        f'<a class="static-only" href="{_e(path.as_uri())}#page={at}" title="前後のページだけを抜き出した PDF">{label}</a>'
        f'<a class="whole static-only" href="{whole}">全体を開く（重い）</a>'
    )


def _locate(index: Index, quote: str, hits: list[dict], cards_by_path: dict[str, dict]) -> dict:
    """Where ``quote`` is written: the card that shows it, its page (or sheet row) and section,
    and a close-up.

    Pages of the hits and the pages either side are tried first, since the answer usually
    sits there; then every section of the hit files, so evidence on a distant page, in a
    spreadsheet row or in a file without a rendition is found too. ``hit`` is None when the
    quote is in none of them.
    """
    for hit in hits:
        page = hit["anchor"].get("page")
        if not (page and hit.get("pdf")):
            continue
        for p in (page, page + 1, page - 1):
            if image := _closeup(hit, p, quote):
                return _evidence(quote, cards_by_path[hit["path"]], p, hit["crumb"] or hit["title"], image)
    for row in _sections_containing(index, quote, list(cards_by_path)):
        card = cards_by_path[row["path"]]
        crumb = row["crumb"] or row["title"]
        anchor = _anchor_of_line(row, quote)
        if _is_sheet(card) and "row" in anchor:
            where = (anchor["sheet"], anchor["row"])
            ev = _evidence(quote, card, None, crumb, "", where)
            ev["table"] = _sheet_table(card, {where}, _header_rows(json.loads(row["anchors"])), [quote], context=0, ids=False)
            return ev
        page = anchor.get("page")
        if page is None and card.get("rendered_from"):
            from docwhere.rendition import page_with

            page = page_with(Path(card["pdf"]), _plain(quote))
        image = _closeup(card, page, quote) if page and card.get("pdf") else ""
        return _evidence(quote, card, page, crumb, image)
    return _evidence(quote, None, None, "", "")


def _evidence(quote: str, hit: dict | None, page: int | None, crumb: str, image: str, row: tuple | None = None) -> dict:
    # the section's own heading is enough here; the card below carries the full path
    return {"quote": quote, "hit": hit, "page": page, "row": row, "section": crumb.split(" > ")[-1],
            "image": image, "table": "", "target": ""}


SHEET_CONTEXT_ROWS = 3  # rows shown above and below a hit row
SHEET_MAX_COLS = 12


def _header_rows(anchors: list[dict]) -> dict[str, int]:
    """Sheet name -> row number of its header row, from a sheet chunk's anchors."""
    return {a["sheet"]: a["row"] for a in anchors if a.get("header")}


def _sheet_excerpt(index: Index, hit: dict, rows: set[tuple[str, int]], quotes: list[str]) -> str:
    own = [r for r in index.chunk_with_neighbors(hit["chunk_id"]) if r["id"] == hit["chunk_id"]]
    headers = _header_rows(json.loads(own[0]["anchors"])) if own else {}
    return _sheet_table(hit, rows, headers, quotes, SHEET_CONTEXT_ROWS)


def _sheet_table(
    hit: dict, rows: set[tuple[str, int]], headers: dict[str, int], quotes: list[str], context: int, ids: bool = True
) -> str:
    """The rows of a workbook around ``rows``, read from the file, as HTML tables (one per
    sheet) with the header row on top; each of ``rows`` is marked, and with ``ids`` it is also
    a jump target (the copy shown as evidence must not repeat the card's ids)."""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(hit["path"], read_only=True, data_only=True)
    except Exception:
        return ""  # moved or changed since indexing; the extracted text still shows
    tables = []
    try:
        for sheet in sorted({s for s, _ in rows}):
            if sheet not in wb.sheetnames:
                continue
            ws = wb[sheet]
            marked = sorted(r for s, r in rows if s == sheet)
            wanted = sorted({n for r in marked for n in range(max(1, r - context), r + context + 1)})
            header = headers.get(sheet)
            if header is not None and header not in wanted:
                wanted = [header, *wanted]
            values: dict[int, dict[int, str]] = {}
            for lo, hi in _runs(wanted):
                for r, cells in enumerate(ws.iter_rows(min_row=lo, max_row=hi, values_only=True), lo):
                    values[r] = {c: norm(str(v)) for c, v in enumerate(cells, 1) if v is not None and norm(str(v))}
            cols = sorted({c for cells in values.values() for c in cells})[:SHEET_MAX_COLS]
            if not cols:
                continue
            body, previous = [], None
            for r in wanted:
                if previous is not None and r != previous + 1:
                    body.append(f'<tr class="gap"><td colspan="{len(cols) + 1}">…</td></tr>')
                previous = r
                cells = values.get(r, {})
                attrs = ' class="header-row"' if r == header else ""
                if r in marked:
                    attrs = f' class="hit-row" id="{_row_id(hit, sheet, r)}"' if ids else ' class="hit-row"'
                tds = "".join(f"<td>{_mark(cells.get(c, ''), hit['terms'], quotes)[0]}</td>" for c in cols)
                body.append(f'<tr{attrs}><th scope="row">{r}</th>{tds}</tr>')
            letters = "".join(f"<th>{_column_letter(c)}</th>" for c in cols)
            tables.append(
                f'<figure class="sheet"><figcaption>{_e(sheet)}</figcaption><div class="sheet-scroll">'
                f'<table><thead><tr><th></th>{letters}</tr></thead><tbody>{"".join(body)}</tbody></table></div></figure>'
            )
    finally:
        wb.close()
    return "".join(tables)


def _runs(numbers: list[int]) -> list[tuple[int, int]]:
    """Consecutive runs in sorted ``numbers`` as (first, last) pairs."""
    runs: list[tuple[int, int]] = []
    for n in sorted(numbers):
        if runs and n == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], n)
        else:
            runs.append((n, n))
    return runs


def _column_letter(index: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(index)


def _closeup(hit: dict, page: int, quote: str) -> str:
    # a quoted table row reads "項目: 値 / ..." in the index but only as its cells on the page
    for needle in dict.fromkeys((quote, _plain(quote))):
        try:
            return _data_uri(render_page(hit["pdf"], page, hit["terms"], CLOSEUP_DPI, "jpeg", [needle], closeup=True))
        except Exception:
            continue  # not on this page, past either end, or the file changed
    return ""


QUOTE_PROBE_CHARS = 24  # enough of a quote to find its section; the rest may wrap onto the next line


def _probe(quote: str) -> str:
    """What to look for in the index: the start of the quote, or for a table row quoted as
    "コード: E2041 / 対処: ..." its first cell, since a sheet stores the cells tab-separated."""
    if " / " in quote and ": " in quote.split(" / ", 1)[0]:
        return quote.split(" / ", 1)[0].split(": ", 1)[1][:QUOTE_PROBE_CHARS]
    return quote[:QUOTE_PROBE_CHARS]


def _sections_containing(index: Index, quote: str, paths: list[str]) -> list:
    probe = _probe(quote)
    like = "%" + probe.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    marks = ",".join("?" * len(paths))
    return index.db.execute(
        "SELECT c.crumb, c.title, c.body, c.anchors, f.path FROM chunks c JOIN files f ON f.id = c.file_id"
        f" WHERE f.path IN ({marks}) AND c.body LIKE ? ESCAPE '\\' ORDER BY c.id LIMIT 5",
        [*paths, like],
    ).fetchall()


def _anchor_of_line(row, quote: str) -> dict:
    """The anchor (page, or sheet and row) of the first line in ``row`` holding the quote."""
    probe = _probe(quote).lower()
    anchors = json.loads(row["anchors"])
    for i, line in enumerate(row["body"].split("\n")):
        if probe in line.lower():
            return anchors[i] if len(anchors) > 1 and i < len(anchors) else (anchors[0] if anchors else {})
    return {}


def _answer_html(answer: str, evidence: list[dict], excerpts=None) -> str:
    """The answer, then each piece of evidence: where it is written and a close-up, both
    jumping to that page further down."""
    if not (answer or evidence):
        return ""
    items = []
    for ev in evidence:
        hit = ev["hit"]
        if hit is None:
            items.append(
                '<div class="evidence"><p class="missing">資料の中で位置を特定できませんでした。</p>'
                f'<blockquote>{_e(ev["quote"])}</blockquote></div>'
            )
            continue
        at_page = {**hit, "anchor": {**hit["anchor"], "page": ev["page"]}} if ev["page"] else hit
        parts = [f'<span class="file">{_e(Path(hit["path"]).name)}</span>']
        if ev["section"]:
            parts.append(f"<span>{_e(ev['section'])}</span>")
        if ev["page"]:
            parts.append(f'<a href="#{ev["target"]}">{_e(_page_text(hit, ev["page"]))}</a>')
        if ev["row"]:
            parts.append(f'<a href="#{ev["target"]}">{_e(ev["row"][0])} {ev["row"][1]}行目</a>')
        source = f'<p class="source">{"".join(parts)}{_open_link(at_page, excerpts)}</p>'
        if ev["image"]:
            shown = (
                f'<a class="closeup" href="#{ev["target"]}" title="このページへ移動">'
                f'<img src="{ev["image"]}" alt="根拠の箇所の拡大。クリックするとページ全体へ移動します"></a>'
            )
        elif ev["table"]:
            shown = ev["table"]
        else:
            shown = f'<a class="quote" href="#{ev["target"]}"><blockquote>{_e(ev["quote"])}</blockquote></a>'
        cite = _citation(hit, ev)
        button = f'<button type="button" class="cite" data-live hidden data-cite="{_e(cite)}">引用をコピー</button>'
        source = source.replace("</p>", f"{button}</p>", 1)
        items.append(f'<div class="evidence">{source}{shown}</div>')
    head = f'<h2 class="evidence-head">根拠 {len(evidence)} 件</h2>' if len(evidence) > 1 else ""
    text = answer or evidence[0]["quote"]
    return (
        f'<section class="answer" id="answer" aria-label="回答"><p class="answer-text"><span>{_e(text)}</span></p>'
        f'{head}{"".join(items)}</section>'
    )


RAIL_QUOTE_CHARS = 36


def _rail_html(has_answer: bool, evidence: list[dict], hits: list[dict], targets: dict[int, str]) -> str:
    """A list that stays in view while scrolling: each piece of evidence and each candidate
    jumps to its page, so the reader can go back and forth without returning to the top.
    Wide screens get it as a column on the left, narrow ones as a bar across the top."""
    if not evidence and len(hits) < 2:
        return ""
    parts = ['<a class="to-answer" href="#answer">回答へ戻る</a>'] if has_answer else []
    if evidence:
        items = []
        for ev in evidence:
            quote = ev["quote"] if len(ev["quote"]) <= RAIL_QUOTE_CHARS else ev["quote"][:RAIL_QUOTE_CHARS] + "…"
            if ev["hit"] is None:
                items.append(f'<li><span class="item unplaced"><span class="pg">位置不明</span><span class="q">{_e(quote)}</span></span></li>')
                continue
            if ev["page"]:
                where = _page_text(ev["hit"], ev["page"])
            elif ev["row"]:
                where = f"{ev['row'][0]} {ev['row'][1]}行目"
            else:
                where = Path(ev["hit"]["path"]).name
            items.append(
                f'<li><a class="item" href="#{ev["target"]}"><span class="pg">{_e(where)}</span>'
                f'<span class="q">{_e(quote)}</span><span class="sec">{_e(ev["section"])}</span></a></li>'
            )
        parts.append(f'<h2>根拠</h2><ol class="evidence-list">{"".join(items)}</ol>')
    if len(hits) > 1:
        items = "".join(
            f'<li><a class="item" href="#{targets[h["n"]]}"><span class="pg">{_e(h["location"] or "")}</span>'
            f'<span class="q">{_e(Path(h["path"]).name)}</span><span class="sec">{_e((h["crumb"] or h["title"]).split(" > ")[-1])}</span></a></li>'
            for h in hits
        )
        parts.append(f'<h2>資料 {len(hits)} 件</h2><ol class="candidate-list">{items}</ol>')
    return f'<nav class="rail" aria-label="根拠と候補">{"".join(parts)}</nav>'


def _card(
    index: Index,
    hit: dict,
    page_images: list[tuple[int, str]],
    context: int,
    quotes: list[str],
    sheet: str = "",
    evidence_pages: set[int] = frozenset(),
    excerpts=None,
) -> str:
    rows = index.chunk_with_neighbors(hit["chunk_id"], context)
    sections = []
    for row in rows:
        own = row["id"] == hit["chunk_id"]
        body = _section_html(row, hit["terms"], hit["anchor"].get("text") if own else None, quotes)
        heading = _e(row["crumb"] or row["title"] or "見出しなし")
        if own:
            sections.append(f'<section class="own"><h3>{heading}</h3>{body}</section>')
        else:
            if row["crumb"] and row["crumb"] == hit["crumb"]:  # a long section split into parts
                heading = "同じ節（前の部分）" if row["id"] < hit["chunk_id"] else "同じ節（続き）"
            sections.append(f'<details class="section"><summary>{heading}</summary>{body}</details>')
    if not rows:
        sections.append('<p class="empty">この検索のあとでインデックスが更新されました。検索し直してください。</p>')

    path = Path(hit["path"])
    facts = [f"<span>{_e(hit['location'])}</span>"] if hit["location"] else []
    facts.append(f"<span>更新 {_e(hit['modified'])}</span>")
    newer = f'<p class="warn">新しい版があります: {_e(Path(hit["newer_version"]).name)}</p>' if hit.get("newer_version") else ""
    if hit.get("rendered_from") in (".docx", ".docm", ".doc"):
        newer += '<p class="note">ページは Word で PDF にしたときの番号です。印刷設定によってずれることがあります。</p>'

    if sheet:
        content = f'{sheet}<details class="extract"><summary>抽出したテキスト</summary>{"".join(sections)}</details>'
    elif page_images:
        # pages in page order, each with its number; the hit page is marked
        page = hit["anchor"]["page"]
        reader = {"data-pdf": hit["pdf"], "data-terms": json.dumps(hit["terms"], ensure_ascii=False),
                  "data-quotes": json.dumps(quotes, ensure_ascii=False)}
        attrs = "".join(f' {k}="{_e(v)}"' for k, v in reader.items())
        figures = "".join(
            f'<figure class="{_page_classes(p == page, p in evidence_pages)}" id="{_page_id(hit, p)}"{attrs} data-page="{p}">'
            f'<figcaption>{_e(_page_text(hit, p))}{_page_tags(p == page, p in evidence_pages)}</figcaption>'
            f'{_pager(hit, p)}<div class="page-scroll"><div class="page-img"><img src="{src}" alt="{_e(path.name)} p.{p}"></div></div></figure>'
            for p, src in page_images
        )
        content = f'{figures}<details class="extract"><summary>抽出したテキスト</summary>{"".join(sections)}</details>'
    else:
        content = f'<div class="extract">{"".join(sections)}</div>'
    return f"""
<article class="hit" id="hit{hit['n']}">
  <header>
    <h2>{_e(path.name)}</h2>
    <p class="crumb">{_e(hit['crumb'] or hit['title'])}</p>
    <p class="facts">{''.join(facts)}{_open_link(hit, excerpts)}</p>
    {_open_button(hit, "actions")}
    <p class="path"><span class="copy" title="クリックで全体を選択">{_e(str(path))}</span><span class="hint">クリックで選択して Ctrl+C でコピー</span></p>
    {newer}
  </header>
  {content}
</article>"""


OPEN_LABELS = {".pdf": "PDF アプリで開く", ".docx": "Word で開く", ".docm": "Word で開く", ".doc": "Word で開く",
               ".xlsx": "Excel で開く", ".xlsm": "Excel で開く", ".xls": "Excel で開く",
               ".pptx": "PowerPoint で開く", ".pptm": "PowerPoint で開く", ".ppt": "PowerPoint で開く"}


def _open_spec(path: str, anchor: dict) -> str:
    return json.dumps({"path": path, "anchor": anchor}, ensure_ascii=False)


def _open_button(hit: dict, cls: str) -> str:
    """A button (live pages only) that opens the file in its application at the hit."""
    label = OPEN_LABELS.get(Path(hit["path"]).suffix.lower(), "エディタで開く")
    anchor = {k: v for k, v in hit["anchor"].items() if k != "page" or not hit.get("rendered_from")}
    return (f'<p class="{cls}" data-live hidden><button type="button" data-open="{_e(_open_spec(hit["path"], anchor))}">'
            f"{label}</button></p>")


def _pager(hit: dict, page: int) -> str:
    """Page controls for the live reader: turn pages, go back after following a reference, open."""
    if hit.get("rendered_from"):
        label = OPEN_LABELS[hit["rendered_from"]]
        spec = _open_spec(hit["path"], {k: v for k, v in hit["anchor"].items() if k != "page"})
        opener = f'<button type="button" data-open="{_e(spec)}">{label}</button>'
    else:
        spec = _open_spec(hit["pdf"], {"page": page})
        opener = f'<button type="button" data-open-page data-open="{_e(spec)}" title="このページを PDF アプリで開く">PDF アプリ</button>'
    return (
        '<div class="pager" data-live hidden>'
        '<button type="button" data-go="-1" aria-label="前のページ">‹ 前</button>'
        f'<span class="pg-now">p.{page}</span>'
        '<button type="button" data-go="1" aria-label="次のページ">次 ›</button>'
        '<button type="button" data-back hidden>参照元に戻る</button>'
        f'<button type="button" data-home hidden>p.{page} に戻る</button>'
        '<button type="button" data-toc>目次</button>'
        '<span class="zoom" role="group" aria-label="表示の大きさ">'
        '<button type="button" data-zoom="fit" aria-pressed="false" title="ページ全体を画面に収める">全体</button>'
        '<button type="button" data-zoom="-1" aria-label="縮小">−</button>'
        '<span class="zoom-now">100%</span>'
        '<button type="button" data-zoom="1" aria-label="拡大">＋</button></span>'
        '<form class="jump" data-jump><input type="text" inputmode="numeric" placeholder="印刷ページ" aria-label="印刷されたページ番号で移動" size="6">'
        '<button type="submit">移動</button></form>'
        f'<span class="spacer"></span>{opener}</div>'
        '<nav class="trail" data-live hidden aria-label="この位置の見出し"></nav>'
    )


def _citation(hit: dict, ev: dict) -> str:
    where = [Path(hit["path"]).name]
    if ev["section"]:
        where.append(ev["section"])
    if ev["page"]:
        where.append(f"p.{ev['page']}")
    if ev["row"]:
        where.append(f"{ev['row'][0]} {ev['row'][1]}行目")
    return f"「{ev['quote']}」（{'、'.join(where)}）"


def _page_classes(is_hit: bool, has_evidence: bool) -> str:
    return " ".join(["page", *(["is-hit"] if is_hit else []), *(["has-evidence"] if has_evidence else [])])


def _page_tags(is_hit: bool, has_evidence: bool) -> str:
    # yellow is the answer and its evidence, blue the search hit; the tags carry both
    tags = ['<span class="tag ev-tag">根拠</span>'] if has_evidence else []
    if is_hit:
        tags.append('<span class="tag hit-tag">ヒットしたページ</span>')
    return "".join(tags)


def _section_html(row, terms: list[str], hit_line: str | None, quotes: list[str]) -> str:
    anchors = json.loads(row["anchors"])
    out, previous = [], None
    for i, line in enumerate(row["body"].split("\n")):
        anchor = anchors[i] if len(anchors) > 1 and i < len(anchors) else (anchors[0] if anchors else {})
        label = location_label(anchor) if len(anchors) > 1 else ""
        prefix = f'<span class="loc">{_e(label)}</span>' if label and label != previous else ""
        previous = label
        text = line.replace(CELL_SEP, " | ")
        marked, has_answer = _mark(text, terms, quotes)
        classes = [c for c, on in (("hit-line", hit_line is not None and line == hit_line), ("answer-line", has_answer)) if on]
        cls = f' class="{" ".join(classes)}"' if classes else ""
        out.append(f"<p{cls}>{prefix}{marked}</p>")
    return "".join(out)


def _mark(text: str, terms: list[str], quotes: list[str]) -> tuple[str, bool]:
    """``text`` escaped, terms in <mark> and quoted answer text in <mark class=ans>."""
    low = text.lower()
    if len(low) != len(text):  # a few characters change length when lower-cased
        return _e(text), False
    level = [0] * len(text)  # 1 = term, 2 = answer
    for t in (t.lower() for t in terms):
        start = 0
        while t and (i := low.find(t, start)) >= 0:
            level[i : i + len(t)] = [max(x, 1) for x in level[i : i + len(t)]]
            start = i + 1
    for q in (q.lower() for q in quotes):
        if len(low.strip()) >= MIN_QUOTE_LINE and low.strip() in q:
            level = [2] * len(text)  # one line of a quote that spans several
            continue
        start = 0
        while q and (i := low.find(q, start)) >= 0:
            level[i : i + len(q)] = [2] * len(q)
            start = i + len(q)
    tags = {0: ("", ""), 1: ("<mark>", "</mark>"), 2: ('<mark class="ans">', "</mark>")}
    out, run, current = [], [], 0
    for ch, lv in zip(text, level):
        if lv != current:
            out.append(tags[current][0] + _e("".join(run)) + tags[current][1])
            run, current = [], lv
        run.append(ch)
    out.append(tags[current][0] + _e("".join(run)) + tags[current][1])
    return "".join(out), 2 in level


def _e(text: str) -> str:
    return html.escape(text or "", quote=True)


_STYLE = """
:root {
  --bg: #eef1f4; --surface: #ffffff; --ink: #1b2430; --muted: #5b6675; --rule: #d5dbe3;
  --blue: #1f4e8c; --marker: #ffb000; --marker-soft: #ffe39a; --term: #dbe6f5; --warn: #a8431a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #12161c; --surface: #1a2028; --ink: #e4e8ee; --muted: #98a3b3; --rule: #2c3542;
    --blue: #8db4ea; --marker: #ffb000; --marker-soft: #5a4512; --term: #23364f; --warn: #f0a07a;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.75 "BIZ UDPGothic", "Yu Gothic UI", "Meiryo", sans-serif; }
.wrap { max-width: 1040px; margin: 0 auto; padding: 28px 24px 96px; }
a { color: var(--blue); }
a:focus-visible, summary:focus-visible { outline: 2px solid var(--blue); outline-offset: 3px; border-radius: 2px; }

.top-bar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 0; }
.top .back { font-size: 13px; text-decoration: none; }
[data-pin][aria-pressed="true"] { border-color: var(--marker); background: var(--marker-soft); }
.top-actions { display: flex; gap: 6px; }
dialog#settings { width: min(600px, 92vw); padding: 0; color: var(--ink); background: var(--surface);
  border: 1px solid var(--rule); border-radius: 8px; }
dialog#settings::backdrop { background: rgba(0, 0, 0, .45); }
.settings { padding: 20px 24px; }
.settings h2 { margin: 0 0 12px; font-size: 18px; }
.settings fieldset { margin: 0 0 16px; padding: 10px 14px 12px; border: 1px solid var(--rule); border-radius: 6px; }
.settings legend { padding: 0 6px; font-weight: 700; font-size: 14px; }
.settings label { display: block; margin: 6px 0; font-size: 14px; }
.settings input[type=text] { width: 100%; margin-top: 2px; padding: 5px 8px; font: inherit; font-size: 13px;
  color: var(--ink); background: var(--bg); border: 1px solid var(--rule); border-radius: 4px; }
.settings input[type=text]:focus-visible { outline: 2px solid var(--blue); outline-offset: 1px; }
.settings .row { display: flex; gap: 6px; }
.settings .row button { flex: none; white-space: nowrap; }
.settings .custom[aria-disabled="true"] { opacity: .45; pointer-events: none; }
.settings .hint { margin: 4px 0 8px; color: var(--muted); font-size: 12px; }
.settings .error { margin: 0 0 10px; color: var(--warn); font-size: 13px; }
.settings .buttons { display: flex; justify-content: flex-end; gap: 8px; }
.settings .primary { border-color: var(--blue); background: var(--blue); color: var(--surface); }
.top .back::before { content: "‹ "; }
.top h1 { margin: 6px 0 2px; font-size: 26px; line-height: 1.4; font-weight: 700; }
.top .query { margin: 0; display: flex; flex-wrap: wrap; gap: 4px 18px; color: var(--muted); font-size: 13px; }

.answer { margin: 24px 0 0; padding: 22px 26px 26px; background: var(--surface);
  border-left: 6px solid var(--marker); border-radius: 0 6px 6px 0; }
.answer-text { margin: 0; font-size: 28px; line-height: 1.5; font-weight: 700; }
.answer-text span { background: linear-gradient(transparent 60%, var(--marker-soft) 60%);
  -webkit-box-decoration-break: clone; box-decoration-break: clone; }
.source, .facts { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; margin: 10px 0 0;
  color: var(--muted); font-size: 13px; }
.source .file { color: var(--ink); font-weight: 700; }
a.whole { color: var(--muted); font-size: 12px; }
.closeup img { display: block; width: 100%; background: #fff; border-left: 5px solid var(--marker); border-radius: 0 3px 3px 0; }
blockquote { margin: 14px 0 0; padding: 2px 0 2px 14px; border-left: 3px solid var(--marker); color: var(--muted); }

.layout.has-rail { display: grid; grid-template-columns: 250px minmax(0, 1fr); max-width: 1340px; margin: 0 auto; }
.layout.has-rail .wrap { margin: 0; }
.rail { position: sticky; top: 0; align-self: start; max-height: 100vh; overflow-y: auto; padding: 28px 8px 28px 20px; }
.rail .to-answer { display: inline-block; font-size: 13px; text-decoration: none; }
.rail .to-answer::before { content: "↑ "; }
.rail h2 { margin: 20px 0 6px; font-size: 13px; color: var(--muted); font-weight: 700; }
.rail ol { list-style: none; margin: 0; padding: 0; }
.rail .item { display: block; padding: 8px 10px; margin: 0 0 4px; border-left: 3px solid var(--rule);
  border-radius: 0 4px 4px 0; color: inherit; text-decoration: none; }
.rail .evidence-list .item { border-left-color: var(--marker); }
.rail a.item:hover, .rail a.item:focus-visible { background: var(--surface); }
.rail .pg { display: block; color: var(--blue); font-weight: 700; font-size: 13px; }
.rail .q { display: block; font-size: 13px; line-height: 1.55; }
.rail .sec { display: block; color: var(--muted); font-size: 12px; line-height: 1.5; }
.rail .unplaced { border-left-color: var(--warn); }
.rail .unplaced .pg { color: var(--warn); }

.region { margin: 56px 0 0; font-size: 20px; }
.hit { margin: 28px 0 0; padding: 18px 0 8px; border-top: 3px solid var(--blue); scroll-margin-top: 16px; }
.region + .hit { margin-top: 12px; }
.hit h2 { margin: 0; font-size: 18px; line-height: 1.5; word-break: break-all; }
.hit .crumb { margin: 2px 0 0; font-weight: 700; color: var(--blue); }
.hit .path { margin: 4px 0 0; display: flex; flex-wrap: wrap; align-items: baseline; gap: 2px 10px; font-size: 12px; }
.hit .path .copy { -webkit-user-select: all; user-select: all; cursor: text; padding: 1px 6px; color: var(--ink);
  background: var(--bg); border: 1px solid var(--rule); border-radius: 3px; word-break: break-all; }
.hit .path .copy:hover { border-color: var(--blue); }
.hit .path .hint { color: var(--muted); }
.warn { margin: 8px 0 0; color: var(--warn); font-size: 14px; }
.hit .note { margin: 6px 0 0; color: var(--muted); font-size: 12px; }
.sheet { margin: 18px 0 0; scroll-margin-top: 12px; }
.sheet figcaption { margin: 0 0 6px; color: var(--muted); font-size: 13px; }
.sheet-scroll { overflow-x: auto; border: 1px solid var(--rule); border-radius: 3px; }
.sheet table { border-collapse: collapse; font-size: 13px; line-height: 1.55; min-width: 100%; }
.sheet th, .sheet td { padding: 5px 10px; border-bottom: 1px solid var(--rule); text-align: left; vertical-align: top; }
.sheet thead th, .sheet tbody th { color: var(--muted); font-weight: 400; background: var(--bg); }
.sheet tbody th { text-align: right; width: 1%; white-space: nowrap; }
.sheet td { min-width: 6em; max-width: 28em; white-space: pre-wrap; word-break: break-word; }
.sheet tr.header-row td { font-weight: 700; background: var(--term); }
.sheet tr.hit-row td { background: var(--marker-soft); }
.sheet tr.hit-row th { border-left: 4px solid var(--marker); color: var(--ink); font-weight: 700; scroll-margin-top: 12px; }
.sheet tr.gap td { color: var(--muted); text-align: center; padding: 0; }
.evidence .sheet { margin-top: 12px; }
.page { margin: 22px 0 0; max-width: 880px; scroll-margin-top: 12px; }
.page figcaption { margin: 0 0 6px; color: var(--muted); font-size: 13px; }
.page.is-hit figcaption, .page.has-evidence figcaption { color: var(--ink); font-weight: 700; }
.tag { margin-left: 8px; padding: 0 8px; border-radius: 3px; font-weight: 400; font-size: 12px; }
.hit-tag { background: var(--term); }
.ev-tag { background: var(--marker-soft); }
.page img { display: block; width: 100%; background: #fff; border: 1px solid var(--rule); border-radius: 3px; }
.page.is-hit img { border: 2px solid var(--blue); }
.page.has-evidence img { border: 3px solid var(--marker); }
.page:target figcaption { color: var(--blue); }
details { margin: 12px 0 0; }
summary { cursor: pointer; color: var(--blue); font-size: 14px; }
a.closeup { display: block; }
a.closeup:hover img { border-left-color: var(--blue); }
.evidence-head { margin: 22px 0 0; font-size: 15px; }
/* each piece of evidence: where it is on the left, the excerpt strip on the right */
.evidence { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 18px; align-items: start; margin: 16px 0 0; }
.evidence + .evidence { padding-top: 16px; border-top: 1px solid var(--rule); }
.evidence .source { margin: 0; flex-direction: column; align-items: flex-start; gap: 2px; }
.evidence .source .file { word-break: break-all; }
.evidence .sheet, .evidence blockquote, .evidence .missing { margin-top: 0; }
.evidence .sheet figcaption { display: none; }  /* the sheet is already named in the source */
a.quote { color: inherit; text-decoration: none; }
a.quote:hover blockquote { border-left-color: var(--blue); }
.missing { margin: 6px 0 0; color: var(--warn); font-size: 13px; }

.extract { margin-top: 14px; }
.extract .own { margin-top: 10px; }
.extract h3 { margin: 0 0 6px; font-size: 14px; color: var(--muted); }
.extract p { margin: 0; padding: 2px 10px; white-space: pre-wrap; word-break: break-word; max-width: 80em; }
.extract p.hit-line { border-left: 3px solid var(--blue); }
.extract p.answer-line { border-left: 3px solid var(--marker); }
.extract .section { color: var(--muted); }
.extract .section[open] { color: var(--ink); }
mark { background: var(--term); color: inherit; border-radius: 2px; }
mark.ans { background: var(--marker-soft); font-weight: 700; }
.loc { display: inline-block; margin-right: 8px; color: var(--muted); font-size: 12px; }
.empty { color: var(--muted); }

.history h1 { margin: 0; font-size: 24px; }
.history .note { margin: 2px 0 0; color: var(--muted); font-size: 13px; }
.history ol { list-style: none; margin: 20px 0 0; padding: 0; }
.history li a { display: block; padding: 14px 18px; margin: 0 0 10px; background: var(--surface);
  border-left: 4px solid var(--rule); border-radius: 0 6px 6px 0; color: inherit; text-decoration: none; }
.history li a:hover, .history li a:focus-visible { border-left-color: var(--marker); }
.history .q { display: block; font-weight: 700; }
.history .a { display: block; }
.history .a span { background: linear-gradient(transparent 60%, var(--marker-soft) 60%); }
.history small { color: var(--muted); font-size: 12px; }

/* live reader (served pages only) */
button { font: inherit; font-size: 13px; color: var(--ink); background: var(--surface); border: 1px solid var(--rule);
  border-radius: 4px; padding: 3px 10px; cursor: pointer; }
button:hover { border-color: var(--blue); }
button:focus-visible { outline: 2px solid var(--blue); outline-offset: 2px; }
.actions { margin: 8px 0 0; }
.cite { margin-top: 6px; }
/* one row that stays at the top of the window while its page is being read */
.pager { position: sticky; top: 0; z-index: 4; display: flex; flex-wrap: nowrap; align-items: center; gap: 6px;
  margin: 0 0 8px; padding: 6px 0; overflow-x: auto; background: var(--bg); scrollbar-width: thin; }
.pager > * { flex: none; }
.page-scroll { scroll-margin-top: var(--pager-h, 52px); }
.pager .pg-now { min-width: 7em; text-align: center; color: var(--muted); font-size: 13px; font-variant-numeric: tabular-nums; }
.pager .spacer { flex: 1; }
.page-scroll { overflow: auto; }
.page-img { position: relative; width: calc(var(--zoom, 1) * 100%); }
body.zoom-fit .page { max-width: none; }
body.zoom-fit .page-scroll { text-align: center; }
body.zoom-fit .page-img { display: inline-block; width: auto; height: calc(100vh - var(--pager-h, 52px) - 16px); }
body.zoom-fit .page-img img { width: auto; height: 100%; }
.zoom { display: inline-flex; align-items: center; gap: 4px; }
.zoom-now { min-width: 3.4em; text-align: center; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
[data-zoom="fit"][aria-pressed="true"] { border-color: var(--blue); background: var(--term); }
.page-img.loading img { opacity: .45; }
.xref { position: absolute; border-radius: 2px; }
.xref:hover, .xref:focus-visible { background: rgba(31, 78, 140, .14); outline: 2px solid var(--blue); }
.page.moved figcaption { opacity: .5; }
.trail { margin: 0 0 8px; font-size: 13px; color: var(--muted); line-height: 1.9; }
.trail .crumb-link { border: 0; background: none; padding: 0 2px; color: var(--blue); font-size: 13px; }
.trail .crumb-link:hover { text-decoration: underline; }
.jump { display: inline-flex; gap: 4px; margin: 0; }
.jump input { width: 5.5em; padding: 3px 6px; font: inherit; font-size: 13px; color: var(--ink); background: var(--bg);
  border: 1px solid var(--rule); border-radius: 4px; }
dialog#toc { width: min(720px, 94vw); height: min(80vh, 900px); padding: 0; color: var(--ink); background: var(--surface);
  border: 1px solid var(--rule); border-radius: 8px; }
dialog#toc[open] { display: flex; flex-direction: column; }
dialog#toc::backdrop { background: rgba(0, 0, 0, .45); }
.toc-head { display: flex; gap: 8px; padding: 12px 14px; border-bottom: 1px solid var(--rule); }
.toc-head input { flex: 1; padding: 5px 8px; font: inherit; font-size: 14px; color: var(--ink); background: var(--bg);
  border: 1px solid var(--rule); border-radius: 4px; }
.toc-list { list-style: none; margin: 0; padding: 6px 0; overflow-y: auto; flex: 1; }
.toc-list button { display: flex; justify-content: space-between; gap: 12px; width: 100%; text-align: left; border: 0;
  border-radius: 0; background: none; padding: 4px 12px; font-size: 13px; }
.toc-list button:hover { background: var(--bg); }
.toc-list .here > button { background: var(--marker-soft); font-weight: 700; }
.toc-page { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
#toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); max-width: min(560px, 90vw);
  padding: 10px 16px; border-radius: 6px; background: var(--ink); color: var(--bg); font-size: 14px; z-index: 10; }
@media (max-width: 1100px) {
  /* the list becomes a bar of page buttons pinned to the top */
  .layout.has-rail { display: block; }
  .rail { z-index: 5; max-height: none; display: flex; align-items: center; gap: 6px; overflow-x: auto;
    padding: 8px 16px; background: var(--bg); border-bottom: 1px solid var(--rule); }
  .rail h2, .rail .q, .rail .sec { display: none; }
  .rail ol { display: flex; gap: 6px; }
  .rail .item { margin: 0; padding: 4px 10px; white-space: nowrap; border-left-width: 0; border-bottom: 3px solid var(--rule);
    border-radius: 4px 4px 0 0; background: var(--surface); }
  .rail .evidence-list .item { border-bottom-color: var(--marker); }
  .rail .candidate-list .pg:empty::before { content: "候補"; }
  .rail .to-answer { white-space: nowrap; margin-right: 6px; }
  .layout.has-rail .page, .layout.has-rail .hit, .layout.has-rail .answer { scroll-margin-top: 56px; }
  .layout.has-rail .pager { top: var(--rail-h, 46px); }
}
@media (max-width: 640px) {
  .wrap { padding: 20px 16px 64px; }
  .top h1 { font-size: 21px; }
  .answer { padding: 18px 18px 20px; }
  .evidence { grid-template-columns: minmax(0, 1fr); gap: 8px; }
  .answer-text { font-size: 22px; }
  .hit { padding: 16px; }
}
"""

_PAGE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<title>{title}</title>
<style>
{style}</style>
</head>
<body>
{body}
<div id="toast" role="status" hidden></div>
</body>
</html>
"""

_RESULT_BODY = """<div class="{layout}">
{rail}
<div class="wrap">
<header class="top"><p class="top-bar"><a class="back" href="index.html">質問の履歴</a><span class="top-actions"><button type="button" data-pin data-live hidden aria-pressed="false">常に手前に表示</button><button type="button" data-settings data-live hidden>設定</button></span></p><h1>{title}</h1><p class="query">{query}</p></header>
{answer}
<main>{cards}</main>
</div>
</div>"""

_HISTORY_BODY = """<div class="wrap history">
<h1>質問の履歴</h1><p class="note">新しい順に最大 {keep} 件。{days} 日を過ぎたものは自動で削除されます。</p>
<ol>{items}</ol>
</div>"""


# Injected by the docwhere window (see window.py) after each page load; the page's own CSP
# forbids scripts, so nothing in a document can run. Plain DOM, no libraries; every call goes
# to Python through pywebview's bridge (window.pywebview.api), never over the network.
APP_JS = r"""(() => {
  "use strict";
  if (window.__docwhere) return;
  window.__docwhere = true;
  const all = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const toastBox = document.getElementById("toast");
  let toastTimer = 0;
  const toast = (message) => {
    if (!toastBox) return;
    toastBox.textContent = message;
    toastBox.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastBox.hidden = true; }, 3000);
  };
  const bridge = async () => {
    for (let i = 0; i < 100; i++) {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.page) return window.pywebview.api;
      await new Promise((r) => setTimeout(r, 50));
    }
    throw new Error("bridge unavailable");
  };
  const failed = (what) => (e) => toast(`${what}できませんでした: ${(e && e.message) || e}`);
  const PAGE_TIMEOUT = 20000;
  const withTimeout = (promise, ms) => Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(() => reject(new Error("応答がありません。もう一度押してください")), ms)),
  ]);

  all("[data-live]").forEach((el) => { el.hidden = false; });
  all(".static-only").forEach((el) => { el.hidden = true; });

  // stay inside the viewer: only other docwhere pages and in-page anchors navigate
  document.addEventListener("click", (event) => {
    const a = event.target.closest("a[href]");
    if (!a || a.classList.contains("xref")) return;
    const href = a.getAttribute("href");
    if (href.startsWith("#") || /^[0-9][0-9-]*\.html$|^index\.html$/.test(href)) return;
    event.preventDefault();
  }, true);

  document.addEventListener("click", async (event) => {
    const open = event.target.closest("[data-open]");
    if (open) {
      event.preventDefault();
      toast("開いています…");
      try { toast(await (await bridge()).open(open.dataset.open)); } catch (e) { failed("開けま")(e); }
      return;
    }
    const cite = event.target.closest("[data-cite]");
    if (cite) {
      event.preventDefault();
      try { await (await bridge()).copy(cite.dataset.cite); toast("引用をコピーしました"); } catch (e) { failed("コピー")(e); }
      return;
    }
    const pin = event.target.closest("[data-pin]");
    if (pin) {
      event.preventDefault();
      const api = await bridge();
      const on = await api.pin(pin.getAttribute("aria-pressed") !== "true");
      showPin(on);
    }
  });

  const pinButton = document.querySelector("[data-pin]");
  const showPin = (on) => {
    if (!pinButton) return;
    pinButton.setAttribute("aria-pressed", String(on));
    pinButton.textContent = on ? "手前に固定中" : "常に手前に表示";
  };
  bridge().then((api) => api.pinned()).then(showPin, () => {});

  const readers = [];
  let active = null;
  all("figure.page[data-pdf]").forEach((figure) => {
    const frame = figure.querySelector(".page-img");
    const img = frame.querySelector("img");
    const label = figure.querySelector(".pg-now");
    const back = figure.querySelector("[data-back]");
    const homeButton = figure.querySelector("[data-home]");
    const openPage = figure.querySelector("[data-open-page]");
    const pdf = figure.dataset.pdf;
    const terms = JSON.parse(figure.dataset.terms || "[]");
    const quotes = JSON.parse(figure.dataset.quotes || "[]");
    const home = Number(figure.dataset.page);
    const trail = [];
    const outline = figure.querySelector(".trail");
    let page = home;
    let total = 0;
    let offset = 0;  // PDF index minus printed page number, when the pages carry one
    const pageText = (p) => `p.${p}${offset && p - offset >= 1 ? `（印刷 ${p - offset}）` : ""}`;

    async function loadLinks() {
      all(".xref", frame).forEach((a) => a.remove());
      try {
        const shownPage = page;
        const data = await withTimeout((await bridge()).links(pdf, page), PAGE_TIMEOUT);
        if (shownPage !== page) return;
        total = data.pages;
        offset = data.offset || 0;
        label.textContent = `${pageText(page)} / ${total}`;
        outline.replaceChildren();
        data.trail.forEach(([title, start], i) => {
          if (i) outline.append(" › ");
          const b = document.createElement("button");
          b.type = "button";
          b.className = "crumb-link";
          b.textContent = title;
          b.title = `この節の先頭（${pageText(start)}）へ`;
          b.addEventListener("click", () => show(start, true));
          outline.append(b);
        });
        for (const link of data.links) {
          const a = document.createElement("a");
          a.className = "xref";
          a.href = "#";
          a.title = `p.${link.page} へ移動`;
          a.style.left = link.x * 100 + "%";
          a.style.top = link.y * 100 + "%";
          a.style.width = link.w * 100 + "%";
          a.style.height = link.h * 100 + "%";
          a.addEventListener("click", (event) => { event.preventDefault(); show(link.page, true); });
          frame.appendChild(a);
        }
      } catch (e) {
        label.textContent = `p.${page}`;
      }
    }

    // The newest request wins: an answer that comes back after a later page turn is dropped,
    // and one that never comes back gives up after PAGE_TIMEOUT instead of leaving the page dim.
    let request = 0;
    let wanted = page;  // where the page is heading; quick presses count from here
    async function show(target, remember) {
      if (target < 1 || (total && target > total) || target === wanted) return;
      const previous = page;
      const mine = ++request;
      wanted = target;
      active = reader;
      label.textContent = `${pageText(target)}${total ? " / " + total : ""}…`;
      frame.classList.add("loading");
      try {
        const src = await withTimeout((await bridge()).page(pdf, target, terms, quotes, zoomDpi()), PAGE_TIMEOUT);
        if (mine !== request) return;
        if (remember) trail.push(previous);
        page = target;
        img.src = src;
        figure.dataset.dpi = String(zoomDpi());
        img.alt = img.alt.replace(/p\.\d+$/, `p.${page}`);
        back.hidden = trail.length === 0;
        homeButton.hidden = page === home;
        figure.classList.toggle("moved", page !== home);
        figure.dataset.current = String(page);
        if (openPage) openPage.dataset.open = JSON.stringify({ path: pdf, anchor: { page } });
        loadLinks();
      } catch (e) {
        if (mine !== request) return;
        wanted = page;
        failed(`p.${target} を表示`)(e);
      } finally {
        if (mine === request) {
          frame.classList.remove("loading");
          label.textContent = `${pageText(page)}${total ? " / " + total : ""}`;
        }
      }
    }

    async function sharpen() {
      // re-draw the page shown at the resolution the current zoom needs
      if (Number(figure.dataset.dpi || BASE_DPI) === zoomDpi()) return;
      try {
        img.src = await withTimeout((await bridge()).page(pdf, page, terms, quotes, zoomDpi()), PAGE_TIMEOUT);
        figure.dataset.dpi = String(zoomDpi());
      } catch (e) { /* the lower resolution image stays */ }
    }

    const reader = {
      figure,
      pdf,
      sharpen,
      current: () => page,
      pageText: (p) => pageText(p),
      go: (p) => show(p, true),
      turn: (step) => show(wanted + step, false),
      back: () => { if (trail.length) show(trail.pop(), false); },
      home: () => { trail.length = 0; show(home, false); },
    };
    readers.push(reader);
    figure.addEventListener("pointerdown", () => { active = reader; });
    figure.querySelector("[data-go='-1']").addEventListener("click", () => reader.turn(-1));
    figure.querySelector("[data-go='1']").addEventListener("click", () => reader.turn(1));
    back.addEventListener("click", reader.back);
    homeButton.addEventListener("click", reader.home);
    figure.querySelector("[data-toc]").addEventListener("click", () => openToc(reader));
    figure.querySelector("[data-jump]").addEventListener("submit", (event) => {
      event.preventDefault();
      const input = event.target.querySelector("input");
      const number = parseInt(input.value.replace(/[^0-9]/g, ""), 10);
      if (!number) return;
      input.value = "";
      input.blur();
      show(number + offset, true);  // the number printed on the page, as the manual cites it
    });
    loadLinks();
  });

  // page size: "width" fills the column times a scale, "fit" shows the whole page in the window
  const BASE_DPI = 140;
  const STEPS = [0.6, 0.75, 0.9, 1, 1.25, 1.5, 1.75, 2, 2.5];
  const zoom = { mode: "width", scale: 1 };
  const zoomDpi = () => (zoom.mode === "width" && zoom.scale > 1.1 ? Math.min(300, Math.round(BASE_DPI * zoom.scale)) : BASE_DPI);
  let sharpenTimer = 0;
  function applyZoom(save) {
    document.body.classList.toggle("zoom-fit", zoom.mode === "fit");
    document.body.style.setProperty("--zoom", String(zoom.scale));
    all("[data-zoom='fit']").forEach((b) => b.setAttribute("aria-pressed", String(zoom.mode === "fit")));
    all(".zoom-now").forEach((s) => { s.textContent = zoom.mode === "fit" ? "全体" : `${Math.round(zoom.scale * 100)}%`; });
    clearTimeout(sharpenTimer);
    sharpenTimer = setTimeout(() => readers.forEach((r) => r.sharpen()), 400);
    if (save) bridge().then((api) => api.save_zoom(zoom.mode, zoom.scale)).catch(() => {});
  }
  function stepZoom(direction) {
    const i = STEPS.findIndex((s) => s >= zoom.scale - 0.001);
    const next = STEPS[Math.max(0, Math.min(STEPS.length - 1, (i < 0 ? STEPS.length - 1 : i) + direction))];
    zoom.mode = "width";
    zoom.scale = next;
    applyZoom(true);
  }
  document.addEventListener("click", (event) => {
    const b = event.target.closest("[data-zoom]");
    if (!b) return;
    if (b.dataset.zoom === "fit") {
      zoom.mode = zoom.mode === "fit" ? "width" : "fit";
      applyZoom(true);
      // line the page up just under its toolbar, so the whole of it is in sight
      const scroller = b.closest("figure.page") && b.closest("figure.page").querySelector(".page-scroll");
      if (zoom.mode === "fit" && scroller) {
        // after the page has shrunk; scrolling first would aim at where the large page was
        const align = () => scroller.scrollIntoView({ block: "start" });
        setTimeout(align, 80);
        setTimeout(align, 400);
      }
    } else {
      stepZoom(Number(b.dataset.zoom));
    }
  });
  bridge().then((api) => api.zoom()).then((saved) => { Object.assign(zoom, saved); applyZoom(false); }, () => {});
  const measureBars = () => {
    const pager = document.querySelector(".pager");
    const rail = document.querySelector(".rail");
    const railOnTop = rail && getComputedStyle(rail).position === "sticky" && rail.getBoundingClientRect().width > innerWidth * 0.6;
    const railH = railOnTop ? rail.offsetHeight : 0;
    document.body.style.setProperty("--rail-h", `${railH}px`);
    document.body.style.setProperty("--pager-h", `${(pager ? pager.offsetHeight + 8 : 52) + railH}px`);
  };
  measureBars();
  addEventListener("resize", measureBars);

  // contents: the PDF's outline, filterable; the section holding the current page is marked
  const outlines = new Map();
  async function openToc(reader) {
    let entries = outlines.get(reader.pdf);
    try {
      if (!entries) {
        entries = await withTimeout((await bridge()).toc(reader.pdf), PAGE_TIMEOUT);
        outlines.set(reader.pdf, entries);
      }
    } catch (e) { failed("目次を開け")(e); return; }
    if (!entries.length) { toast("この PDF には目次（しおり）がありません"); return; }
    let dialog = document.getElementById("toc");
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.id = "toc";
      dialog.setAttribute("aria-label", "目次");
      document.body.appendChild(dialog);
    }
    dialog.replaceChildren();
    const head = document.createElement("div");
    head.className = "toc-head";
    const filter = document.createElement("input");
    filter.type = "text";
    filter.placeholder = "見出しを絞り込む";
    filter.setAttribute("aria-label", "見出しを絞り込む");
    const close = document.createElement("button");
    close.type = "button";
    close.textContent = "閉じる";
    close.addEventListener("click", () => dialog.close());
    head.append(filter, close);
    const list = document.createElement("ol");
    list.className = "toc-list";
    const current = reader.current();
    let here = null;
    for (const [level, title, start] of entries) {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.style.paddingLeft = `${(level - 1) * 16 + 8}px`;
      b.textContent = title;
      const pg = document.createElement("span");
      pg.className = "toc-page";
      pg.textContent = reader.pageText(start);
      b.append(pg);
      b.addEventListener("click", () => { dialog.close(); reader.go(start); });
      li.append(b);
      li.dataset.text = title.toLowerCase();
      list.append(li);
      if (start >= 1 && start <= current) here = li;
    }
    if (here) here.classList.add("here");
    filter.addEventListener("input", () => {
      const q = filter.value.trim().toLowerCase();
      for (const li of list.children) li.hidden = q !== "" && !li.dataset.text.includes(q);
    });
    dialog.append(head, list);
    dialog.showModal();
    if (here) here.scrollIntoView({ block: "center" });
    filter.focus();
  }

  // settings: built here, filled through .value / .checked only, never from markup
  const SETTINGS_FORM = `
    <form method="dialog" class="settings">
      <h2>設定</h2>
      <fieldset>
        <legend>PDF を開くアプリ</legend>
        <label><input type="radio" name="pdfmode" value="auto"> 自動（Acrobat があれば Acrobat、なければ Windows の既定のアプリ）</label>
        <label><input type="radio" name="pdfmode" value="custom"> アプリを指定する</label>
        <div class="custom">
          <label>プログラム<span class="row"><input type="text" name="program" spellcheck="false"><button type="button" data-browse>参照…</button></span></label>
          <label>引数<input type="text" name="args" spellcheck="false"></label>
          <p class="hint">{page} にページ番号、{file} に PDF のパスが入ります。SumatraPDF と Acrobat は選ぶと自動で入ります。</p>
          <button type="button" data-try>今のページで試す</button>
        </div>
      </fieldset>
      <label>ウィンドウの名前<input type="text" name="title" spellcheck="false" maxlength="80"></label>
      <fieldset>
        <legend>回答ページの開き方</legend>
        <label><input type="radio" name="viewer" value="window"> このウィンドウ</label>
        <label><input type="radio" name="viewer" value="browser"> ブラウザ</label>
      </fieldset>
      <p class="error" role="alert" hidden></p>
      <div class="buttons"><button value="cancel">キャンセル</button><button type="button" class="primary" data-save>保存</button></div>
    </form>`;

  async function openSettings() {
    let api;
    try { api = await bridge(); } catch (e) { failed("設定を開け")(e); return; }
    const current = await api.settings();
    let dialog = document.getElementById("settings");
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.id = "settings";
      dialog.setAttribute("aria-label", "設定");
      document.body.appendChild(dialog);
    }
    dialog.innerHTML = SETTINGS_FORM;
    const form = dialog.querySelector("form");
    const field = (name) => form.elements.namedItem(name);
    const custom = form.querySelector(".custom");
    const error = form.querySelector(".error");
    const setMode = (mode) => {
      form.querySelector(`input[name=pdfmode][value=${mode}]`).checked = true;
      custom.setAttribute("aria-disabled", String(mode === "auto"));
    };
    field("program").value = current.pdf_program;
    field("args").value = current.pdf_args;
    field("title").value = current.title;
    form.querySelector(`input[name=viewer][value=${current.viewer}]`).checked = true;
    setMode(current.pdf_program ? "custom" : "auto");
    const showError = (e) => { error.textContent = (e && e.message) || String(e); error.hidden = false; };

    all("input[name=pdfmode]", form).forEach((r) => r.addEventListener("change", () => setMode(r.value)));
    field("program").addEventListener("change", async () => {
      if (!field("args").value.trim()) field("args").value = await api.preset(field("program").value);
    });
    form.querySelector("[data-browse]").addEventListener("click", async () => {
      const chosen = await api.choose_program();
      if (chosen) {
        field("program").value = chosen;
        field("args").value = await api.preset(chosen);
      }
    });
    form.querySelector("[data-try]").addEventListener("click", async () => {
      const figure = (inView() || active || readers[0] || {}).figure;
      if (!figure) { showError("このページには PDF のページがありません"); return; }
      error.hidden = true;
      try {
        toast(await api.try_pdf(field("program").value, field("args").value, figure.dataset.pdf,
                                Number(figure.dataset.current || figure.dataset.page)));
      } catch (e) { showError(e); }
    });
    form.querySelector("[data-save]").addEventListener("click", async () => {
      const auto = form.querySelector("input[name=pdfmode][value=auto]").checked;
      try {
        await api.save_settings({
          pdf_program: auto ? "" : field("program").value,
          pdf_args: auto ? "" : field("args").value,
          title: field("title").value,
          viewer: form.querySelector("input[name=viewer]:checked").value,
        });
        dialog.close();
        toast("設定を保存しました");
      } catch (e) { showError(e); }
    });
    dialog.showModal();
  }
  const settingsButton = document.querySelector("[data-settings]");
  if (settingsButton) settingsButton.addEventListener("click", openSettings);

  // keyboard: arrows turn the page in view (or the last one used), Backspace goes back
  const inView = () => readers.find((r) => {
    const box = r.figure.getBoundingClientRect();
    return box.top < innerHeight * 0.6 && box.bottom > innerHeight * 0.3;
  });
  document.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, [contenteditable]") || event.altKey || event.ctrlKey || event.metaKey) return;
    const reader = inView() || active;
    if (!reader) return;
    if (event.key === "ArrowRight") { event.preventDefault(); reader.turn(1); }
    else if (event.key === "ArrowLeft") { event.preventDefault(); reader.turn(-1); }
    else if (event.key === "Backspace") { event.preventDefault(); reader.back(); }
    else if (event.key === "Home") { event.preventDefault(); reader.home(); }
    else if (event.key === "+" || event.key === ";") { event.preventDefault(); stepZoom(1); }
    else if (event.key === "-") { event.preventDefault(); stepZoom(-1); }
    else if (event.key === "0") { event.preventDefault(); zoom.mode = "width"; zoom.scale = 1; applyZoom(true); }
  });
})();
"""
