"""PowerPoint 97-2003 presentations (.ppt) read without PowerPoint, from the file format
Microsoft publishes: [MS-CFB] for the compound file, [MS-PPT] for the presentation, [MS-ODRAW]
for the shapes on its slides.

What is read is what the .pptx reader takes from the same presentation saved by PowerPoint as
.pptx: a chunk a slide, titled by its title placeholder, holding the text of its shapes in the
order they are drawn (the shapes of a group in the group's order), each table a line per row,
and the slide's notes. As PowerPoint writes the .pptx, the date, footer and slide number a
slide shows are put on it from its master, fields show their text, and a shape PowerPoint 2007
or later kept in DrawingML beside the old drawing is read from that.

A presentation PowerPoint would read otherwise is not guessed at: ``Unsupported`` says why, and
the caller has PowerPoint read it. That is an encrypted one, one of PowerPoint 95 or older, and
anything in the file that does not hold together as the format says.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from docwhere.extractors.msdoc import MAX_DOCUMENT_CHARS, Unsupported, _Compound
from docwhere.model import Chunk, norm, table_lines

_u16 = struct.Struct("<H").unpack_from
_i32 = struct.Struct("<i").unpack_from
_u32 = struct.Struct("<I").unpack_from
_HEADER = struct.Struct("<HHI").unpack_from

# record types ([MS-PPT] 2.13.24 RecordType, [MS-ODRAW] 2.1)
_DOCUMENT, _SLIDE, _SLIDE_ATOM, _NOTES = 0x03E8, 0x03EE, 0x03EF, 0x03F0
_SLIDE_PERSIST, _SLIDE_LIST, _USER_EDIT, _PERSIST_DIRECTORY = 0x03F3, 0x0FF0, 0x0FF5, 0x1772
_DRAWING, _TEXT_HEADER, _TEXT_CHARS, _TEXT_BYTES, _OUTLINE_REF = 0x040C, 0x0F9F, 0x0FA0, 0x0FA8, 0x0F9E
_PLACEHOLDER = 0x0BC3
_MAIN_MASTER, _COMPOSITE_MASTER, _CONTENT_MASTER = 0x03F8, 0x041D, 0x0422
_MASTER_DATE, _MASTER_NUMBER, _MASTER_FOOTER = 0x07, 0x08, 0x09  # a master's placeholders for them
_DOCUMENT_ATOM, _HEADERS_FOOTERS, _HEADERS_FOOTERS_ATOM, _CSTRING = 0x03E9, 0x0FD9, 0x0FDA, 0x0FBA
_FLAGS = 0  # where _footers keeps the flags
_DG, _SPGR, _SP, _FSP, _FOPT, _TERTIARY_FOPT = 0xF002, 0xF003, 0xF004, 0xF00A, 0xF00B, 0xF122
_CLIENT_ANCHOR, _CHILD_ANCHOR, _CLIENT_DATA, _CLIENT_TEXTBOX = 0xF010, 0xF00F, 0xF011, 0xF00D
_TITLES = {0x0D, 0x0F, 0x11}  # title, centered title, vertical title: what the .pptx reader calls the title
_NOTES_BODY = 0x0C
_RECTANGLE = 1
_TABLE_PROPERTY = 0x039F  # tableProperties: a group of shapes that is a table
_WORDART_TEXT = 0x00C0  # gtextUNICODE: text kept in the shape's properties, not in a text box
_METRO_BLOB = 0x03A9  # the shape as PowerPoint 2007 and later drew it, in DrawingML (a zip)
_SLIDE_NUMBER, _DATE_FIELD, _GENERIC_DATE, _HEADER_FIELD, _FOOTER_FIELD, _RTF_DATE = (
    0x0FD8, 0x0FF7, 0x0FF8, 0x0FF9, 0x0FFA, 0x1015)
_FIELDS = {_SLIDE_NUMBER, _DATE_FIELD, _GENERIC_DATE, _HEADER_FIELD, _FOOTER_FIELD, _RTF_DATE}
MAX_DEPTH = 64  # records inside records, groups inside groups


def _records(data: bytes, start: int = 0, end: int | None = None):
    """(type, instance, body start, body end, is a container) of each record in data[start:end]."""
    end = len(data) if end is None else end
    at = start
    while at + 8 <= end:
        ver_instance, kind, size = _HEADER(data, at)
        body = at + 8
        if body + size > end:
            raise Unsupported("a record runs past what holds it")
        yield kind, ver_instance >> 4, body, body + size, ver_instance & 0xF == 0xF
        at = body + size


# control characters PowerPoint leaves out of a .pptx (a tab, a line break, a paragraph end stay)
_CONTROLS = dict.fromkeys(c for c in range(0x20) if c not in (0x09, 0x0B, 0x0D))


def _text(data: bytes, kind: int, start: int, end: int) -> str:
    raw = data[start:end]
    if kind == _TEXT_CHARS:
        text = raw.decode("utf-16-le", "ignore")  # a surrogate without its pair is dropped
    else:
        text = raw.decode("latin-1")  # a TextBytesAtom: the low byte of each UTF-16 code unit
    return text.translate(_CONTROLS)


@dataclass
class _Shape:
    texts: list[str] = field(default_factory=list)  # paragraphs, in order
    placeholder: int = 0
    kind: int = 0  # the shape type (FSP instance)
    anchor: tuple = ()  # left, top, right, bottom
    children: list[_Shape] = field(default_factory=list)  # a group's shapes
    group: bool = False
    table: bool = False
    xml: list[str] | None = None  # the lines of the shape PowerPoint 2007 and later kept beside it
    has_text: bool = False  # the shape has text of its own (then its DrawingML's text is masked)
    raw: tuple | None = None  # its text before its fields are filled in, and the fields


class _Presentation:
    def __init__(self, data: bytes) -> None:
        cfb = _Compound(data)
        user = cfb.stream("Current User")
        doc = cfb.stream("PowerPoint Document")
        if user is None or doc is None:
            raise Unsupported("not a PowerPoint 97-2003 presentation")
        if cfb.stream("EncryptedSummary") is not None:
            raise Unsupported("password protected")
        if len(user) < 20:
            raise Unsupported("a short Current User stream")
        token = _u32(user, 12)[0]
        if token == 0xF3D1C4DF:
            raise Unsupported("password protected")
        if token != 0xE391C05F:
            raise Unsupported("not a PowerPoint 97-2003 presentation (PowerPoint 95 or older?)")
        self.doc = doc
        self.persist = self._persist(_u32(user, 16)[0])
        self.budget = MAX_DOCUMENT_CHARS  # characters of text yet to be made (see _charge)

    # -- where each persistent object is

    def _persist(self, offset: int) -> dict[int, int]:
        """persist id -> offset of the object, as the latest save left it: every save appends a
        UserEditAtom and a persist directory naming what it changed."""
        edits, seen = [], set()
        while offset:
            if offset in seen or offset + 32 > len(self.doc):
                raise Unsupported("the list of saves is broken")
            seen.add(offset)
            _, kind, _ = _HEADER(self.doc, offset)
            if kind != _USER_EDIT:
                raise Unsupported("a save's record is not where it is said to be")
            edits.append(offset)
            offset = _u32(self.doc, offset + 16)[0]
        self.document_id = _u32(self.doc, edits[0] + 24)[0]
        found: dict[int, int] = {}
        for edit in reversed(edits):  # the oldest first: a later save's entry replaces an earlier one
            at = _u32(self.doc, edit + 20)[0]
            if at + 8 > len(self.doc):
                raise Unsupported("a persist directory lies outside the file")
            _, kind, size = _HEADER(self.doc, at)
            if kind != _PERSIST_DIRECTORY or at + 8 + size > len(self.doc):
                raise Unsupported("a persist directory is not where it is said to be")
            pos, end = at + 8, at + 8 + size
            while pos + 4 <= end:
                word = _u32(self.doc, pos)[0]
                first, count = word & 0xFFFFF, word >> 20
                if pos + 4 + 4 * count > end:
                    raise Unsupported("a persist directory entry runs past its end")
                for k in range(count):
                    found[first + k] = _u32(self.doc, pos + 4 + 4 * k)[0]
                pos += 4 + 4 * count
        return found

    def _object(self, persist_id: int, *kinds: int) -> tuple[int, int]:
        offset = self.persist.get(persist_id)
        if offset is None or offset + 8 > len(self.doc):
            raise Unsupported(f"object {persist_id} is not in the file")
        _, found, size = _HEADER(self.doc, offset)
        if found not in kinds or offset + 8 + size > len(self.doc):
            raise Unsupported(f"object {persist_id} is not what it is said to be")
        return offset + 8, offset + 8 + size

    # -- the slides

    def slides(self) -> list[tuple[int, list[tuple[str, list]]]]:
        """(persist id of the slide, the texts its list keeps for its placeholders) of each
        slide, in order. Also notes where each notes page and each master is, by its id."""
        start, end = self._object(self.document_id, _DOCUMENT)
        out: list[tuple[int, list[tuple[str, list]]]] = []
        self.notes_ids: dict[int, int] = {}
        self.masters: dict[int, tuple[int, list]] = {}
        self.master_footers: dict[int, dict[int, _Shape]] = {}
        self.slide_footers: dict[int, str | int] = {}
        self.first_number, self.omit_title_slide = 1, False
        for kind, instance, s, e, _ in _records(self.doc, start, end):
            if kind == _DOCUMENT_ATOM and e - s >= 38:
                self.first_number = struct.unpack_from("<h", self.doc, s + 32)[0]
                self.omit_title_slide = self.doc[s + 37] != 0
            elif kind == _HEADERS_FOOTERS and instance == 3:
                self.slide_footers = self._footers(s, e)
            elif kind == _SLIDE_LIST:
                listed = self._listed(s, e)
                if instance == 0:
                    out = [(persist, outline) for persist, _, outline in listed]
                elif instance == 1:
                    self.masters = {slide_id: (persist, outline) for persist, slide_id, outline in listed}
                elif instance == 2:
                    self.notes_ids = {slide_id: persist for persist, slide_id, _ in listed}
        return out

    def _listed(self, start: int, end: int) -> list[tuple[int, int, list[tuple[str, list]]]]:
        """(persist id, slide id, placeholder texts with their fields) of each entry of a
        SlideListWithText."""
        out: list[tuple[int, int, list[tuple[str, list]]]] = []
        for kind, _, s, e, _ in _records(self.doc, start, end):
            if kind == _SLIDE_PERSIST and e - s >= 16:
                out.append((_u32(self.doc, s)[0], _u32(self.doc, s + 12)[0], []))
            elif kind == _TEXT_HEADER and out:
                out[-1][2].append(("", []))
            elif out and out[-1][2]:
                if kind in (_TEXT_CHARS, _TEXT_BYTES):
                    out[-1][2][-1] = (_text(self.doc, kind, s, e), out[-1][2][-1][1])
                elif kind in _FIELDS and e - s >= 4:
                    out[-1][2][-1][1].append((_i32(self.doc, s)[0], kind))
        return out

    def slide(self, persist_id: int, outline: list[tuple[str, list]]) -> tuple[list[_Shape], int]:
        """The slide's shapes, and the id of its notes page (0: none). The date, footer and
        slide number the slide shows come first, as PowerPoint puts them in the .pptx: its
        master's placeholders for them, their fields filled in."""
        start, end = self._object(persist_id, _SLIDE)
        notes, shapes, title_slide, master, master_shapes = 0, [], False, 0, True
        records = list(_records(self.doc, start, end))
        own = next(((s, e) for kind, _, s, e, _ in records if kind == _HEADERS_FOOTERS), None)
        self.footers = self._footers(*own) if own else self.slide_footers
        # A slide of the blank layout saved by PowerPoint 2003 or older has no title in the
        # .pptx: a title placeholder on it is linked to none of the layout's (the layout of one
        # saved by PowerPoint 2007 or later is kept beside it, under a master id of its own).
        self.titled = any(kind in (_COMPOSITE_MASTER, _CONTENT_MASTER) for kind, *_ in records)
        for kind, _, s, e, _ in records:
            if kind == _SLIDE_ATOM and e - s >= 22:
                title_slide = _u32(self.doc, s)[0] == 0  # SL_TitleSlide
                self.titled = self.titled or _u32(self.doc, s)[0] != 0x10  # SL_Blank
                master, notes = _u32(self.doc, s + 12)[0], _u32(self.doc, s + 16)[0]
                master_shapes = bool(_u16(self.doc, s + 20)[0] & 0x01)  # fMasterObjects
            elif kind == _DRAWING:
                shapes = self._drawing(s, e, outline)
        if title_slide and self.omit_title_slide or not master_shapes:
            return shapes, notes
        flags = self.footers.get(_FLAGS, 0)
        placed = {shape.placeholder for shape in _flat(shapes)}
        footers = self._master_footers(master)
        shown = []
        for kind, flag in ((_MASTER_DATE, 0x01), (_MASTER_FOOTER, 0x20), (_MASTER_NUMBER, 0x08)):
            if flags & flag and kind in footers and kind not in placed:
                text, fields = footers[kind].raw
                shown.append(_Shape(texts=self._with_fields(text, fields).split("\r")))
        return shown + shapes, notes

    def _master_footers(self, master_id: int) -> dict[int, _Shape]:
        """The date, footer and slide number placeholders of a master, by their kind."""
        if master_id not in self.master_footers:
            found: dict[int, _Shape] = {}
            if master_id in self.masters:
                persist, outline = self.masters[master_id]
                start, end = self._object(persist, _MAIN_MASTER, _SLIDE)
                for kind, _, s, e, _ in _records(self.doc, start, end):
                    if kind == _DRAWING:
                        for shape in _flat(self._drawing(s, e, outline)):
                            if shape.placeholder in (_MASTER_DATE, _MASTER_FOOTER, _MASTER_NUMBER) and shape.raw:
                                found.setdefault(shape.placeholder, shape)
            self.master_footers[master_id] = found
        return self.master_footers[master_id]

    def _footers(self, start: int, end: int) -> dict[int, str | int]:
        """What a HeadersFootersContainer keeps: which of date, footer and number are shown
        (under _FLAGS), and the fixed date, header and footer text, by the field showing each."""
        found: dict[int, str | int] = {}
        for kind, instance, s, e, _ in _records(self.doc, start, end):
            if kind == _HEADERS_FOOTERS_ATOM and e - s >= 4:
                found[_FLAGS] = _u16(self.doc, s + 2)[0]
            elif kind == _CSTRING and instance in (0, 1, 2):
                found[(_DATE_FIELD, _HEADER_FIELD, _FOOTER_FIELD)[instance]] = _text(self.doc, _TEXT_CHARS, s, e)
        return found

    def notes(self, notes_id: int) -> list[_Shape]:
        persist_id = self.notes_ids.get(notes_id)
        if persist_id is None:
            raise Unsupported("a slide's notes are not in the list of notes")
        start, end = self._object(persist_id, _NOTES)
        for kind, _, s, e, _ in _records(self.doc, start, end):
            if kind == _DRAWING:
                return self._drawing(s, e, [])
        return []

    def _drawing(self, start: int, end: int, outline: list[tuple[str, list]]) -> list[_Shape]:
        for kind, _, s, e, _ in _records(self.doc, start, end):
            if kind == _DG:
                for k, _, ks, ke, _ in _records(self.doc, s, e):
                    if k == _SPGR:
                        return self._group(ks, ke, outline, 0)
        return []

    def _group(self, start: int, end: int, outline: list[tuple[str, list]], depth: int) -> list[_Shape]:
        """The shapes of a group, in order; the first shape of a group is the group's own."""
        if depth > MAX_DEPTH:
            raise Unsupported("groups of shapes nested too deep")
        shapes: list[_Shape] = []
        first = True
        for kind, _, s, e, _ in _records(self.doc, start, end):
            if kind == _SP:
                shape = self._shape(s, e, outline)
                if not first:
                    shapes.append(shape)
                first = False
            elif kind == _SPGR:
                own = next(((ks, ke) for k, _, ks, ke, _ in _records(self.doc, s, e) if k == _SP), None)
                group = self._shape(*own, outline) if own else _Shape()
                group.group = True
                group.children = self._group(s, e, outline, depth + 1)
                shapes.append(group)
                first = False
        return shapes

    def _shape(self, start: int, end: int, outline: list[tuple[str, list]]) -> _Shape:
        shape, wordart = _Shape(), None
        for kind, instance, s, e, _ in _records(self.doc, start, end):
            if kind == _FSP and e - s >= 8:
                shape.kind = instance
            elif kind in (_FOPT, _TERTIARY_FOPT):
                props = self._properties(s, e, instance)
                if _TABLE_PROPERTY in props:
                    shape.table = True
                if isinstance(props.get(_WORDART_TEXT), bytes):
                    wordart = props[_WORDART_TEXT].decode("utf-16-le", "ignore").split("\0")[0]
                if isinstance(props.get(_METRO_BLOB), bytes):
                    shape.xml = _drawingml(props[_METRO_BLOB])
                    self._charge(sum(map(len, shape.xml or ())))
            elif kind in (_CLIENT_ANCHOR, _CHILD_ANCHOR):
                if e - s == 8:
                    top, left, right, bottom = struct.unpack_from("<4h", self.doc, s)
                    shape.anchor = (left, top, right, bottom)
                elif e - s >= 16:
                    if kind == _CHILD_ANCHOR:
                        shape.anchor = struct.unpack_from("<4i", self.doc, s)  # left, top, right, bottom
                    else:
                        top, left, right, bottom = struct.unpack_from("<4i", self.doc, s)
                        shape.anchor = (left, top, right, bottom)
            elif kind == _CLIENT_DATA:
                for k, _, ks, ke, _ in _records(self.doc, s, e):
                    if k == _PLACEHOLDER and ke - ks >= 5:
                        shape.placeholder = self.doc[ks + 4]
                    elif k == _OUTLINE_REF and ke - ks >= 4:
                        self._outline_text(shape, _i32(self.doc, ks)[0], outline)
            elif kind == _CLIENT_TEXTBOX:
                text, fields = None, []
                for k, _, ks, ke, _ in _records(self.doc, s, e):
                    if k in (_TEXT_CHARS, _TEXT_BYTES):
                        text = _text(self.doc, k, ks, ke)
                    elif k in _FIELDS and ke - ks >= 4:
                        fields.append((_i32(self.doc, ks)[0], k))
                    elif k == _OUTLINE_REF and ke - ks >= 4:
                        self._outline_text(shape, _i32(self.doc, ks)[0], outline)
                if text is not None:
                    self._set_text(shape, text, fields)
        if wordart and not shape.has_text:  # WordArt of PowerPoint 2003 and older: a line a paragraph
            self._charge(len(wordart))
            shape.texts = wordart.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            shape.has_text = True
        return shape

    def _outline_text(self, shape: _Shape, index: int, outline: list[tuple[str, list]]) -> None:
        """A placeholder's text kept with the slide's entry in the list of slides."""
        if not 0 <= index < len(outline):
            raise Unsupported("a shape points at text its slide does not keep")
        self._set_text(shape, *outline[index])

    def _charge(self, chars: int) -> None:
        """Count text about to be made against one budget for the whole presentation: shapes
        that point at the same text, and fields that repeat a footer, make more than the file
        holds, so the limit is kept as it is made, not only on the lines at the end."""
        self.budget -= chars
        if self.budget < 0:
            raise ValueError(f"presentation text exceeds the {MAX_DOCUMENT_CHARS:,}-character limit")

    def _set_text(self, shape: _Shape, text: str, fields: list[tuple[int, int]]) -> None:
        shape.raw = (text, fields)
        shape.texts = self._with_fields(text, fields).split("\r")
        shape.has_text = True

    def _with_fields(self, text: str, fields: list[tuple[int, int]]) -> str:
        """The text with each field's mark (an asterisk) replaced by what the field shows, as
        PowerPoint writes it into the .pptx: the slide number, the header or footer text, the
        fixed date. A date that is the day the file is read is left out."""
        longest = max((len(v) for k, v in self.footers.items() if k != _FLAGS), default=0) if fields else 0
        self._charge(len(text) + len(fields) * max(longest, 6))  # before a footer is repeated in every field
        if not fields:
            return text
        chars = list(text)
        # a field's place counts UTF-16 code units: a character beyond them takes two
        units = [i for i, c in enumerate(chars) for _ in range(2 if ord(c) > 0xFFFF else 1)]
        for at, kind in fields:
            at = units[at] if 0 <= at < len(units) else -1
            if at < 0 or chars[at] != "*":
                raise Unsupported("a field not where its mark is")
            if kind == _SLIDE_NUMBER:
                chars[at] = str(self.slide_number)
            elif kind in (_HEADER_FIELD, _FOOTER_FIELD):
                chars[at] = self.footers.get(kind, "")
            elif kind == _GENERIC_DATE and not self.footers.get(_FLAGS, 0) & 0x02:
                chars[at] = self.footers.get(_DATE_FIELD, "")
            else:
                chars[at] = ""
        return "".join(chars)

    def _properties(self, start: int, end: int, count: int) -> dict[int, bytes | int]:
        """The properties an OfficeArtFOPT holds (its instance is their count): a simple one's
        value, a complex one's data (which follows the table, in the table's order)."""
        props: dict[int, bytes | int] = {}
        data = start + 6 * count
        if data > end:
            raise Unsupported("a shape's properties run past their record")
        for i in range(count):
            at = start + 6 * i
            opid, value = _u16(self.doc, at)[0], _u32(self.doc, at + 2)[0]
            if opid & 0x8000:
                if data + value > end:
                    raise Unsupported("a shape's property runs past its record")
                props[opid & 0x3FFF] = self.doc[data : data + value]
                data += value
            else:
                props[opid & 0x3FFF] = value
        return props


_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
MAX_XML_BYTES = 16 << 20


def _refuse_declarations(xml: bytes) -> None:
    """Refuse a document type or entity declaration, which DrawingML never has and whose
    entities could expand without end. Asked of the parser, not of the bytes: a part in
    UTF-16 or another encoding spells ``<!DOCTYPE`` in bytes no search for it would find."""
    from xml.parsers import expat

    def refuse(*_):
        raise Unsupported("a shape's DrawingML declares a document type or entities")

    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    parser.Parse(xml, True)


def _drawingml(blob: bytes) -> list[str] | None:
    """The lines of a shape kept in DrawingML, read as the .pptx reader reads the shape; None
    when only the note of how it was drawn is kept (the shape itself is the drawing)."""
    import io
    import zipfile
    import zlib
    from xml.etree import ElementTree
    from xml.parsers import expat

    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = [n for n in z.namelist() if n.count("/") == 1 and n.startswith("drs/")
                     and n.endswith(".xml") and n != "drs/downrev.xml"]
            if not names:
                return None
            if len(names) > 1:
                raise Unsupported("a shape's DrawingML is in more than one part")
            info = z.getinfo(names[0])
            if info.file_size > MAX_XML_BYTES:
                raise Unsupported("a shape's DrawingML is too large")
            with z.open(info) as f:
                xml = f.read(MAX_XML_BYTES + 1)
            if len(xml) > MAX_XML_BYTES:  # the size the zip gives was not the size it holds
                raise Unsupported("a shape's DrawingML is too large")
        _refuse_declarations(xml)
        root = ElementTree.fromstring(xml)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, expat.ExpatError, EOFError, ValueError,
            NotImplementedError, OSError, zlib.error) as e:
        raise Unsupported(f"a shape's DrawingML does not read ({type(e).__name__})") from e
    return list(_xml_lines([root], 0))


def _xml_lines(elements, depth: int):
    if depth > MAX_DEPTH:
        raise Unsupported("groups of shapes nested too deep")
    for el in elements:
        if el.tag == _P + "grpSp":
            yield from _xml_lines(list(el), depth + 1)
        elif el.tag == _P + "sp":
            body = el.find(_P + "txBody")
            if body is not None:
                yield from map(_xml_paragraph, body.iter(_A + "p"))
        elif el.tag == _P + "graphicFrame":
            table = el.find(f"{_A}graphic/{_A}graphicData/{_A}tbl")
            if table is not None:
                rows = [
                    ["\n".join(map(_xml_paragraph, tc.iter(_A + "p"))) for tc in tr.findall(_A + "tc")
                     if tc.get("hMerge") not in ("1", "true") and tc.get("vMerge") not in ("1", "true")]
                    for tr in table.findall(_A + "tr")
                ]
                yield from table_lines(rows)


def _xml_paragraph(p) -> str:
    """A paragraph's text as python-pptx gives it: runs and fields, a line break as a vertical tab."""
    out = []
    for child in p:
        if child.tag in (_A + "r", _A + "fld"):
            t = child.find(_A + "t")
            out.append((t.text or "") if t is not None else "")
        elif child.tag == _A + "br":
            out.append("\v")
    return "".join(out)


def _lines(shapes: list[_Shape]):
    """The text of the shapes as the .pptx reader yields it: each paragraph of a shape with text,
    a group's shapes in turn, a table's rows labelled by its header row."""
    for shape in shapes:
        if shape.xml is not None and not shape.has_text and not shape.group:
            yield from shape.xml
        elif shape.table:
            yield from _table(shape)
        elif shape.group:
            yield from _lines(shape.children)
        else:
            yield from shape.texts


def _table(group: _Shape):
    """A table is a group of cells (rectangles, in rows and columns) and of the lines between
    them: a cell merged over others is one rectangle, and the places it covers have none."""
    cells = [s for s in group.children if s.kind == _RECTANGLE and not s.group]
    if not cells or any(len(c.anchor) != 4 for c in cells):
        raise Unsupported("a table without its cells' places")
    tops = sorted({c.anchor[1] for c in cells})
    rows = []
    for top in tops:
        row = sorted((c for c in cells if c.anchor[1] == top), key=lambda c: c.anchor[0])
        rows.append(["\n".join(c.texts) for c in row])
    yield from table_lines(rows)


def extract(path: Path) -> list[Chunk]:
    """The slides of a .ppt, as the .pptx reader reads the same presentation saved as .pptx."""
    try:
        return _extract(path)
    except (IndexError, struct.error) as e:
        raise Unsupported(f"the file does not hold together as the format says ({type(e).__name__})") from e


def _extract(path: Path) -> list[Chunk]:
    from docwhere.index import MAX_FILE_BYTES

    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"over the {MAX_FILE_BYTES >> 20} MB limit on files read")
    pres = _Presentation(path.read_bytes())
    chunks: list[Chunk] = []
    budget = MAX_DOCUMENT_CHARS
    for no, (persist_id, outline) in enumerate(pres.slides(), 1):
        pres.slide_number = pres.first_number + no - 1
        shapes, notes_id = pres.slide(persist_id, outline)
        # the .pptx reader's title is a placeholder of the slide itself, not one inside a group
        title_shape = next((s for s in shapes if s.placeholder in _TITLES), None) if pres.titled else None
        title = norm("\n".join(title_shape.texts)) if title_shape is not None else ""
        texts = list(_lines(shapes))
        if notes_id:
            body = next((s for s in _flat(pres.notes(notes_id)) if s.placeholder == _NOTES_BODY), None)
            if body is not None:
                texts += "\n".join(body.texts).splitlines()
        lines = [line for line in map(norm, texts) if line]
        budget -= sum(map(len, lines))
        if budget < 0:
            raise ValueError(f"presentation text exceeds the {MAX_DOCUMENT_CHARS:,}-character limit")
        if lines:
            chunks.append(Chunk(title, title, lines, [{"slide": no}]))
    return chunks


def _flat(shapes: list[_Shape]):
    for shape in shapes:
        yield shape
        if shape.group and not shape.table:
            yield from _flat(shape.children)
