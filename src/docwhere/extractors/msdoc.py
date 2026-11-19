"""Word 97-2003 documents (.doc) read without Word, from the file format Microsoft publishes:
[MS-CFB] for the compound file a document is stored in, [MS-DOC] for the document itself.

What is read is what the .docx reader takes from the same document saved by Word as .docx,
so that a search finds the same text in the same sections either way: the main text, a
paragraph at a time, each paragraph a heading or not (its outline level, else its style), its
heading number counted as the .docx reader counts it, each table a line per row (a vertically
merged cell's text on each row it spans, tables inside a cell as part of that cell, three
deep), and each text box as a line after the paragraph it is anchored to (a box in a group of
shapes, or a chain of linked boxes, as Word's .docx places it). Text deleted under tracked
changes is left out, as are fields' codes and symbol characters, none of which is text in a
.docx. Ruby (furigana), which a .doc keeps as an EQ field and a .docx as text, is read as the
.docx has it: the reading, then the base text. A fast-saved document is read as any other: its
pieces of text are put in order by the piece table.

A document whose text Word would put otherwise is not guessed at: ``Unsupported`` says why, and
the caller has Word read it. That is a document of Word 95 or older, an encrypted one, one with
cells merged the old way, a fast save that changed how a piece of text reads, and anything in
the file that does not hold together as the format says.
"""

from __future__ import annotations

import re
import struct
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from pathlib import Path

from docwhere.model import MAX_HEADER_CHARS, Chunk, SectionBuilder, table_lines


class Unsupported(Exception):
    """This document is not one this reader reads as Word would; the reason is the message."""


_u16 = struct.Struct("<H").unpack_from
_i16 = struct.Struct("<h").unpack_from
_u32 = struct.Struct("<I").unpack_from


def _at(fmt, data, offset: int, what: str):
    try:
        return fmt(data, offset)[0]
    except struct.error:
        raise Unsupported(f"{what} lies outside the file") from None


# ---- the compound file ([MS-CFB]) -------------------------------------------------------------

_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
_LAST_SECTOR = 0xFFFFFFFA  # sector numbers above this are markers (end of chain, free, ...)
_STREAM, _ROOT = 2, 5


class _Compound:
    """The streams of a compound file, by name, at its top level."""

    def __init__(self, data: bytes) -> None:
        if data[:8] != _SIGNATURE:
            raise Unsupported("not an Office 97-2003 file (no compound file signature)")
        self.data = data
        shift = _at(_u16, data, 0x1E, "the header")
        if shift not in (9, 12):
            raise Unsupported(f"unknown sector size 2^{shift}")
        self.sector = 1 << shift
        self.mini = 1 << _at(_u16, data, 0x20, "the header")
        self.cutoff = _at(_u32, data, 0x38, "the header")
        self.sectors = max(0, (len(data) - self.sector) // self.sector)
        fat_sectors = self._difat(_at(_u32, data, 0x2C, "the header"), _at(_u32, data, 0x44, "the header"))
        self.fat = self._numbers(b"".join(self._sector(s) for s in fat_sectors))
        entries = self._chain(_at(_u32, data, 0x30, "the header"), self.fat)
        self.entries = [entries[i:i + 128] for i in range(0, len(entries) - 127, 128)]
        if not self.entries or self.entries[0][0x42] != _ROOT:
            raise Unsupported("the compound file has no root entry")
        root = self.entries[0]
        self.ministream = self._chain(_u32(root, 0x74)[0], self.fat)[: self._size(root)]
        self.minifat = self._numbers(self._chain(_at(_u32, data, 0x3C, "the header"), self.fat))
        self.names = self._top_level()

    def _sector(self, number: int) -> bytes:
        if number > _LAST_SECTOR or number >= self.sectors:
            raise Unsupported(f"sector {number} lies outside the file")
        start = (number + 1) * self.sector
        return self.data[start:start + self.sector]

    @staticmethod
    def _numbers(data: bytes) -> list[int]:
        return list(struct.unpack_from(f"<{len(data) // 4}I", data))

    def _difat(self, count: int, next_sector: int) -> list[int]:
        found = self._numbers(self.data[0x4C:0x4C + 109 * 4])
        per = self.sector // 4 - 1
        seen = set()
        while next_sector <= _LAST_SECTOR and len(found) < count + per:
            if next_sector in seen:
                raise Unsupported("the compound file's sector list loops")
            seen.add(next_sector)
            numbers = self._numbers(self._sector(next_sector))
            found += numbers[:per]
            next_sector = numbers[per]
        return [s for s in found[:count] if s <= _LAST_SECTOR]

    def _chain(self, start: int, table: list[int], read=None) -> bytes:
        read = read or self._sector
        parts, seen = [], set()
        while start <= _LAST_SECTOR:
            if start in seen or start >= len(table):
                raise Unsupported("a stream's sector chain is broken")
            seen.add(start)
            parts.append(read(start))
            start = table[start]
        return b"".join(parts)

    def _mini_sector(self, number: int) -> bytes:
        start = number * self.mini
        if start + self.mini > len(self.ministream):
            raise Unsupported(f"mini sector {number} lies outside the mini stream")
        return self.ministream[start:start + self.mini]

    @staticmethod
    def _size(entry: bytes) -> int:
        return _u32(entry, 0x78)[0]  # the high half is 0 in files of 512-byte sectors, ignored in the others

    def _top_level(self) -> dict[str, bytes]:
        """name -> directory entry of each entry right under the root (a red-black tree of siblings)."""
        names, todo, seen = {}, [_u32(self.entries[0], 0x4C)[0]], set()
        while todo:
            i = todo.pop()
            if i > _LAST_SECTOR or i in seen:
                continue
            if i >= len(self.entries):
                raise Unsupported("the compound file's directory is broken")
            seen.add(i)
            entry = self.entries[i]
            length = min(_u16(entry, 0x40)[0], 64)
            names[entry[: max(0, length - 2)].decode("utf-16-le", "replace")] = entry
            todo += [_u32(entry, 0x44)[0], _u32(entry, 0x48)[0]]
        return names

    def stream(self, name: str) -> bytes | None:
        entry = self.names.get(name)
        if entry is None or entry[0x42] != _STREAM:
            return None
        size, start = self._size(entry), _u32(entry, 0x74)[0]
        if size < self.cutoff:
            data = self._chain(start, self.minifat, self._mini_sector)
        else:
            data = self._chain(start, self.fat)
        if len(data) < size:
            raise Unsupported(f"the {name} stream is shorter than it says")
        return data[:size]


# ---- properties ([MS-DOC] 2.2.5, 2.6) -----------------------------------------------------------

# sprms read here
_P_IN_TABLE, _P_TTP, _P_ITAP, _P_INNER_CELL, _P_INNER_TTP = 0x2416, 0x2417, 0x6649, 0x244B, 0x244C
_P_ILFO, _P_OUTLINE, _P_HUGE = 0x460B, 0x2640, 0x6646
_T_DEF_TABLE, _T_VERT_MERGE, _T_MERGE, _T_SPLIT = 0xD608, 0xD62B, 0x5624, 0x5625
_C_DELETED, _C_SYMBOL = 0x0800, 0x6A09
_P_CHANGE_TABS = 0xC615
_FIXED = {0: 1, 1: 1, 2: 2, 3: 4, 4: 2, 5: 2, 7: 3}  # operand size by the sprm's spra


def _sprm_list(grpprl: bytes) -> list[tuple[int, bytes]]:
    """(sprm, operand) of each property in a list of them, in order."""
    out, i, n = [], 0, len(grpprl)
    while i + 2 <= n:
        sprm = _u16(grpprl, i)[0]
        i += 2
        spra = sprm >> 13
        if spra in _FIXED:
            size = _FIXED[spra]
        elif sprm == _T_DEF_TABLE:
            size = _at(_u16, grpprl, i, "a table definition") + 1  # its count includes one byte too many
        elif sprm == _P_CHANGE_TABS and i < n and grpprl[i] == 255:
            deleted = grpprl[i + 1] if i + 1 < n else 0
            added_at = i + 2 + 4 * deleted
            added = grpprl[added_at] if added_at < n else 0
            size = 1 + 1 + 4 * deleted + 1 + 3 * added
        else:
            if i >= n:
                break
            size = grpprl[i] + 1
        if i + size > n:
            raise Unsupported("a property list runs past its end")
        out.append((sprm, grpprl[i:i + size]))
        i += size
    return out


def _sprms(grpprl: bytes) -> dict[int, bytes]:
    """sprm -> operand, the last one written when a sprm is given twice."""
    return dict(_sprm_list(grpprl))


# What a property attached to a piece of text (a fast save's change) may not change here: how
# paragraphs make tables, headings and lists, and which characters are text. Anything else
# (fonts, sizes, spacing, revision ids) leaves what is read as it is.
_STRUCTURAL = {_P_IN_TABLE, _P_TTP, _P_ITAP, _P_INNER_CELL, _P_INNER_TTP, _P_ILFO, _P_OUTLINE, _P_HUGE,
               0x4600, 0xC601, 0x2602, 0x2640, 0x664A,  # the paragraph's style, its outline, its depth
               _C_DELETED, _C_SYMBOL, 0x0855}  # deleted, a symbol, a special character
_TABLE_GROUP = 5  # sgc of the table properties: a piece may not change a table


def _check_piece_properties(prm: int, lists: list[bytes]) -> None:
    """Refuse a piece whose attached properties change what is read: Prm1 (bit 0 set) names a
    property list of the piece table; Prm0 holds one property by an index this reader does not
    map, and is refused unless empty."""
    if not prm:
        return
    if not prm & 1:
        if prm >> 1 & 0x7F:
            raise Unsupported("a single property attached to a piece of text")
        return
    index = prm >> 1
    if index >= len(lists):
        raise Unsupported("a piece names a property list that is not there")
    for sprm, _ in _sprm_list(lists[index]):
        if sprm in _STRUCTURAL or sprm >> 10 & 7 == _TABLE_GROUP:
            raise Unsupported(f"a piece of text changes how it reads (property {sprm:#06x})")


class _Pieces:
    """The pieces of the text ordered by where they lie in the file, for finding which of them
    a range of the file holds."""

    def __init__(self, pieces: list[tuple[int, int, int, int]]) -> None:
        self.by_offset = sorted(pieces, key=lambda p: p[2])
        self.offsets = [p[2] for p in self.by_offset]


@dataclass
class _Style:
    sti: int
    name: str
    base: int
    papx: dict[int, bytes] = field(default_factory=dict)


def _styles(table: bytes, fc: int, lcb: int) -> dict[int, _Style]:
    """istd -> paragraph style, from the style sheet (STSH)."""
    if not lcb:
        return {}
    stsh = table[fc:fc + lcb]
    cb_stshi = _at(_u16, stsh, 0, "the style sheet")
    count = _at(_u16, stsh, 2, "the style sheet")
    base_size = _at(_u16, stsh, 4, "the style sheet")
    styles, i = {}, 2 + cb_stshi
    for istd in range(count):
        cb_std = _at(_u16, stsh, i, "a style")
        std = stsh[i + 2:i + 2 + cb_std]
        i += 2 + cb_std
        if not cb_std:
            continue
        sti = _at(_u16, std, 0, "a style") & 0x0FFF
        word2 = _at(_u16, std, 2, "a style")
        kind, base = word2 & 0x000F, word2 >> 4
        if kind != 1:  # a character, table or list style: no paragraph takes it as its style
            continue
        at = base_size
        cch = _at(_u16, std, at, "a style's name")
        name = std[at + 2:at + 2 + 2 * cch].decode("utf-16-le", "replace")
        at += 2 + 2 * cch + 2
        papx: dict[int, bytes] = {}
        cb_upx = _at(_u16, std, at, "a style's paragraph properties") if at + 2 <= len(std) else 0
        if cb_upx >= 2:
            papx = _sprms(std[at + 4:at + 2 + cb_upx])  # the style's own istd, then its sprms
        styles[istd] = _Style(sti, name, base, papx)
    return styles


@dataclass
class _Paragraph:
    text: str  # without its mark; fields' codes and symbols taken out
    mark: str  # the character that ends it: \r, a cell's or a row's \x07, or a section's \x0c
    props: dict[int, bytes]
    grpprl: bytes  # the paragraph's own property list, read in order for a row's merges
    istd: int
    start: int  # cp of its first character
    end: int  # cp after its mark


# ---- the document ------------------------------------------------------------------------------

_CONTROL_OUT = {c: None for c in range(0x20) if c not in (0x09, 0x0A, 0x0B, 0x0C, 0x0E)}
_CONTROL_OUT.update({0x0B: "\n", 0x0C: "\n", 0x0E: "\n"})  # line, page and column breaks
_CONTROL_OUT[0x1E] = None  # non-breaking hyphen: Word writes it as a mark of its own, not text
# a text box's text is its runs' text alone: no tabs and no breaks, which are not text there
_BOX_OUT = {c: None for c in range(0x20) if c != 0x0D}
_FIELD = re.compile("[\x13\x14\x15]")
_CONTROLS = re.compile("[\x00-\x08\x0b-\x1f]")
# Ruby as Word keeps it: EQ \o(\s\up N(reading),base), with an alignment switch (\ad, \ac...)
# after \o and the font and size switches before it
_RUBY = re.compile(r"\s*EQ\b(?:\s*\\\*\s*(?:\w+|\"[^\"]*\")(?:\s+(?:\w+|\"[^\"]*\"))?)*\s*\\o(?:\\a[a-z])?\s*"
                   r"\(\s*\\s\\up\s*-?\d+\s*\(([^()]*)\)\s*,([^()]*)\)\s*", re.IGNORECASE)
# Word turns the field into ruby text only when it is its own ruby (ルビ): the field then names
# the alignment, the font and the size (\* jc, \* "Font:...", \* hps). An EQ written by hand
# stays a field in the .docx, without text, as do combined characters (組み文字: \s\up then \s\do).
_RUBY_SWITCHES = (re.compile(r"\\\*\s*jc\d", re.IGNORECASE), re.compile(r"\\\*\s*\"Font:", re.IGNORECASE),
                  re.compile(r"\\\*\s*hps\d", re.IGNORECASE))
_RUBY_LIKE = re.compile(r"\\o(?:\\a[a-z])?\s*\(\s*\\s\\up", re.IGNORECASE)
_COMBINED = re.compile(r",\s*\\s\\do", re.IGNORECASE)
_HEADING_STYLE = re.compile(r"(?:heading|見出し)\s*(\d)", re.IGNORECASE)
MAX_FIELD_DEPTH = 200  # fields inside fields; Word's own stay far below this
MAX_RUBY_CODE = 2000  # characters of a field's code looked at for ruby
MAX_SHAPE_DEPTH = 100  # groups of shapes inside groups
MAX_CELL_CHARS = 2000  # as the .docx reader caps a cell
MAX_DOCUMENT_CHARS = 50_000_000


_ASTRAL = re.compile("[\U00010000-\U0010FFFF]")
_SURROGATE = re.compile("[\ud800-\udfff]")


def _utf16_units(data: bytes) -> str:
    """UTF-16 text a code unit a character, as Word counts its cps: a character beyond the
    BMP (𠮷, 🍳) is two cps in a .doc, so it is kept here as its two surrogate halves, and
    every cp after it stays where the file's tables put it. ``_whole`` joins them again."""
    text = data.decode("utf-16-le", "surrogatepass")
    if not _ASTRAL.search(text):
        return text
    return _ASTRAL.sub(lambda m: _halves(m.group()), text)


def _halves(char: str) -> str:
    code = ord(char) - 0x10000
    return chr(0xD800 + (code >> 10)) + chr(0xDC00 + (code & 0x3FF))


def _whole(text: str) -> str:
    """Text with its surrogate halves joined into the characters they make (a half left alone,
    which a .docx could not hold either, is dropped)."""
    if not _SURROGATE.search(text):
        return text
    return text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "ignore")


def _cp1252(data: bytes) -> str:
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:  # the five bytes cp1252 leaves undefined stand for themselves
        return "".join(bytes([b]).decode("cp1252", "ignore") or chr(b) for b in data)


class _Document:
    def __init__(self, data: bytes) -> None:
        cfb = _Compound(data)
        word = cfb.stream("WordDocument")
        if word is None:
            raise Unsupported("no WordDocument stream (not a Word document)")
        if _at(_u16, word, 0, "the file information block") != 0xA5EC:
            raise Unsupported("not a Word document")
        if _at(_u16, word, 2, "the file information block") < 0xC1:
            raise Unsupported("a document of Word 95 or older")
        flags = _u16(word, 0x0A)[0]
        if flags & 0x0100 or flags & 0x8000:
            raise Unsupported("password protected or obfuscated")
        # A fast-saved file (flags & 0x0004) keeps its text in pieces out of order, which the
        # piece table puts in order, as for any file; what a fast save adds to a piece is
        # checked where the pieces are read
        table = cfb.stream("1Table" if flags & 0x0200 else "0Table")
        if table is None:
            raise Unsupported("no table stream")
        self.word, self.table, self.data_stream = word, table, cfb.stream("Data") or b""
        csw = _at(_u16, word, 0x20, "the file information block")
        lw = 0x22 + 2 * csw + 2
        cslw = _at(_u16, word, lw - 2, "the file information block")
        if cslw < 11:
            raise Unsupported("a short file information block")
        ccp = [_at(_u32, word, lw + 4 * i, "the file information block") for i in range(11)]
        self.ccp_text = ccp[3]
        # the text box story follows the main text, footnotes, headers, comments and endnotes
        self.box_start = ccp[3] + ccp[4] + ccp[5] + ccp[7] + ccp[8]
        self.box_end = self.box_start + ccp[9]
        fc_lcb = lw + 4 * cslw
        self.n_fc_lcb = _at(_u16, word, fc_lcb, "the file information block")
        self.fc_lcb_at = fc_lcb + 2
        self.styles = _styles(table, *self._fc_lcb(1))

    def _fc_lcb(self, i: int) -> tuple[int, int]:
        if i >= self.n_fc_lcb:
            return 0, 0
        at = self.fc_lcb_at + 8 * i
        fc, lcb = _at(_u32, self.word, at, "the file information block"), _at(_u32, self.word, at + 4, "the file information block")
        if fc + lcb > len(self.table):
            raise Unsupported("a table of the document lies outside its stream")
        return fc, lcb

    def _plc(self, index: int, size: int) -> tuple[list[int], list[bytes]]:
        """The cps and the data of a PLC of ``size``-byte entries in the table stream."""
        fc, lcb = self._fc_lcb(index)
        if not lcb:
            return [], []
        n = (lcb - 4) // (4 + size)
        if lcb != 4 * (n + 1) + size * n:
            raise Unsupported("a table of the document has an odd size")
        cps = list(struct.unpack_from(f"<{n + 1}I", self.table, fc))
        at = fc + 4 * (n + 1)
        return cps, [self.table[at + size * i:at + size * (i + 1)] for i in range(n)]

    # -- the text

    def pieces(self, upto: int) -> list[tuple[int, int, int, int]]:
        """(first cp, end cp, offset in the WordDocument stream, bytes a character) of each
        piece of the text up to cp ``upto``, from the piece table (Clx)."""
        if upto > MAX_DOCUMENT_CHARS:  # said by the file before any of it is read: no text is built
            raise ValueError(f"document text exceeds the {MAX_DOCUMENT_CHARS:,}-character limit")
        fc, lcb = self._fc_lcb(33)
        clx = self.table[fc:fc + lcb]
        i = 0
        lists: list[bytes] = []  # property lists for pieces (Prc), before the table of pieces
        while i < len(clx) and clx[i] == 1:
            size = _at(_i16, clx, i + 1, "the piece table")
            if size < 0 or i + 3 + size > len(clx):  # a negative size would step back and never end
                raise Unsupported("a property list lies outside the piece table")
            lists.append(clx[i + 3:i + 3 + size])
            i += 3 + size  # each step moves on by at least 3 bytes: the loop ends with the table
        if i >= len(clx) or clx[i] != 2:
            raise Unsupported("no piece table")
        size = _at(_u32, clx, i + 1, "the piece table")
        plc = clx[i + 5:i + 5 + size]
        n = (size - 4) // 12
        if n < 1 or len(plc) < size:
            raise Unsupported("an empty or cut piece table")
        cps = struct.unpack_from(f"<{n + 1}I", plc)
        out = []
        for k in range(n):
            first, last = cps[k], min(cps[k + 1], upto)
            if first >= last:
                continue
            raw = _u32(plc, 4 * (n + 1) + 8 * k + 2)[0]
            _check_piece_properties(_u16(plc, 4 * (n + 1) + 8 * k + 6)[0], lists)
            compressed = bool(raw & 0x40000000)
            offset = (raw & 0x3FFFFFFF) // 2 if compressed else raw & 0x3FFFFFFF
            out.append((first, last, offset, 1 if compressed else 2))
        if not out or out[0][0] != 0 or any(a[1] != b[0] for a, b in zip(out, out[1:])) or out[-1][1] != upto:
            raise Unsupported("the pieces of the text do not join up")
        return out

    def _fkp_runs(self, index: int) -> list[tuple[int, int, bytes]]:
        """(first offset, end offset, raw property bytes) of each run of an FKP-paged property
        table: 13 for paragraphs (PlcBtePapx), 12 for characters (PlcBteChpx)."""
        fc, lcb = self._fc_lcb(index)
        n = (lcb - 4) // 8
        if n > len(self.word) // 512:
            raise Unsupported("more property pages than the file holds")
        runs, seen = [], set()
        for k in range(n):
            page_no = _at(_u32, self.table, fc + 4 * (n + 1) + 4 * k, "a property page number") & 0x3FFFFF
            if page_no in seen:
                raise Unsupported("a property page listed twice")
            seen.add(page_no)
            page = self.word[page_no * 512:page_no * 512 + 512]
            if len(page) < 512:
                raise Unsupported("a property page lies outside the file")
            crun = page[511]
            bounds = struct.unpack_from(f"<{crun + 1}I", page)
            for i in range(crun):
                if index == 13:  # BxPap: a byte offset/2, then 12 bytes of height
                    at = page[4 * (crun + 1) + 13 * i] * 2
                    if not at:
                        props = b"\0\0"
                    else:
                        cb = page[at]
                        props = page[at + 2:at + 2 + 2 * page[at + 1]] if cb == 0 else page[at + 1:at + 2 * cb]
                else:
                    at = page[4 * (crun + 1) + i] * 2
                    props = page[at + 1:at + 1 + page[at]] if at else b""
                runs.append((bounds[i], bounds[i + 1], props))
        return runs

    def read(self) -> None:
        """Read the main text into ``paragraphs``, and the text boxes into ``boxes`` (anchor cp
        -> the box's text, in the order of their anchors)."""
        upto = self.box_end if self.box_end > self.box_start else self.ccp_text
        pieces = self.pieces(upto)
        placed = _Pieces(pieces)
        papx = {end: props for _, end, props in self._fkp_runs(13)}
        ends = sorted(papx)
        dropped: list[tuple[int, int]] = []  # cp ranges taken out: symbols
        for start, end, props in self._fkp_runs(12):
            if b"\x00\x08" not in props and b"\x09\x6a" not in props:
                continue  # neither sprm's bytes are there: most runs, passed over without reading them
            sprms = _sprms(props)
            # tracked deletions (on: 1, or 0x81, the style's opposite) and symbols: neither is text
            # in Word's .docx, so both are left out here
            if sprms.get(_C_DELETED, b"\0")[0] in (1, 0x81) or _C_SYMBOL in sprms:
                dropped += self._cps_of(placed, start, end)
        texts, marks = [], []  # all the text read, and (cp after a main paragraph's mark, its properties)
        for first, last, offset, width in pieces:
            raw = self.word[offset:offset + (last - first) * width]
            if len(raw) < (last - first) * width:
                raise Unsupported("the text lies outside the file")
            texts.append(_cp1252(raw) if width == 1 else _utf16_units(raw))
            if first >= self.ccp_text:
                continue
            top = offset + (min(last, self.ccp_text) - first) * width
            for e in ends[bisect_right(ends, offset):bisect_right(ends, top)]:
                if (e - offset) % width == 0:
                    marks.append((first + (e - offset) // width, papx[e]))
        text = "".join(texts)
        main = text[:self.ccp_text]
        marks.sort()
        at_marks = {cp for cp, _ in marks}
        for m in re.finditer("[\r\x07]", main):
            if m.end() not in at_marks:
                raise Unsupported("paragraph marks and paragraph properties do not line up")
        last_mark = marks[-1][0] if marks else 0
        if main[last_mark:].strip("\r\x07\x0c"):
            raise Unsupported("text after the last paragraph")
        clean = self._without_codes(text, dropped)
        self.paragraphs, start = [], 0
        for cp, props in marks:
            if cp <= start:
                continue
            istd = _at(_u16, props, 0, "a paragraph's properties")
            grpprl = props[2:]
            sprms = _sprms(grpprl)
            if _P_HUGE in sprms:
                grpprl = self._huge(sprms[_P_HUGE])
                sprms.update(_sprms(grpprl))
            text = clean[start:cp - 1]
            if _CONTROLS.search(text):  # most paragraphs have none: translated only when there is one
                text = text.translate(_CONTROL_OUT)
            self.paragraphs.append(_Paragraph(_whole(text), main[cp - 1], sprms, grpprl,
                                              istd, start, cp))
            start = cp
        self.boxes = self._boxes(clean) if self.box_end > self.box_start else {}

    def _boxes(self, clean: str) -> dict[int, list[str]]:
        """anchor cp -> the text of each text box anchored there, as the .docx reader joins a
        box's text: its paragraphs with a space, nothing but text."""
        cps, data = self._plc(56, 22)  # PlcftxbxTxt: each box's part of the text box story
        anchors, spas = self._plc(40, 26)  # PlcSpaMom: each shape's anchor in the main text
        by_shape = {_u32(spa, 0)[0]: cp for cp, spa in zip(anchors, spas)}
        shapes = self._shapes()
        found: dict[int, list[tuple[int, str]]] = {}
        for i, entry in enumerate(data[:-1]):  # the last entry ends the story
            reusable, shape = _u16(entry, 8)[0], _u32(entry, 14)[0]
            if reusable:
                continue  # a deleted text box's slot
            # a chain of linked boxes keeps all its text in its first box's entry, as a .docx
            # keeps it in the first box: one box here
            top, order = shapes.get(shape, (shape, 0))  # a box in a group is anchored by its group
            if top not in by_shape:
                raise Unsupported("a text box whose anchor is not found")
            a, b = self.box_start + cps[i], self.box_start + cps[i + 1]
            if not self.box_start <= a <= b <= self.box_end:
                raise Unsupported("a text box's text lies outside its story")
            paragraphs = re.split("[\r\x07]", clean[a:b].rstrip("\r\x07"))
            text = _whole(" ".join(p.translate(_BOX_OUT) for p in paragraphs).strip())
            found.setdefault(by_shape[top], []).append((order, text))
        # the boxes of one anchor in the order the drawing holds them, as a .docx writes a group
        return {cp: [text for _, text in sorted(boxes)] for cp, boxes in found.items()}

    def _shapes(self) -> dict[int, tuple[int, int]]:
        """shape id -> (the id of the shape anchored in the text that holds it: its outermost
        group, or itself; its place in the drawing), for the shapes of the main document, from
        the drawing data (OfficeArt) in the table stream."""
        fc, lcb = self._fc_lcb(50)  # DggInfo
        if not lcb:
            return {}
        data = self.table[fc:fc + lcb]

        def records(body: bytes):
            at = 0
            while at + 8 <= len(body):
                ver_instance, kind, size = struct.unpack_from("<HHI", body, at)
                if at + 8 + size > len(body):
                    raise Unsupported("the drawing data is cut")
                yield kind, ver_instance & 0xF == 0xF, body[at + 8:at + 8 + size]
                at += 8 + size

        def shape_id(container: bytes) -> int | None:
            for kind, _, body in records(container):
                if kind == 0xF00A and len(body) >= 4:  # OfficeArtFSP
                    return _u32(body, 0)[0]
            return None

        out: dict[int, tuple[int, int]] = {}
        order = 0

        def walk(group: bytes, top: int | None, depth: int = 0) -> None:
            nonlocal order
            if depth > MAX_SHAPE_DEPTH:
                raise Unsupported("groups of shapes nested deeper than Word makes them")
            first = True
            for kind, _, body in records(group):
                if kind == 0xF004:  # a shape; the first of a group is the group's own shape
                    spid = shape_id(body)
                    if first and top is not None:
                        first = False
                        continue
                    first = False
                    if spid is not None:
                        out[spid] = (top if top is not None else spid, order)
                        order += 1
                elif kind == 0xF003:  # a group inside this one
                    inner = shape_id(next((b for k, _, b in records(body) if k == 0xF004), b""))
                    walk(body, top if top is not None else inner, depth + 1)
                    first = False

        kind = struct.unpack_from("<H", data, 2)[0] if len(data) >= 8 else 0
        if kind != 0xF000:
            raise Unsupported("unknown drawing data")
        at = 8 + _u32(data, 4)[0]  # past the OfficeArtDggContainer
        while at + 9 <= len(data):
            label = data[at]  # 0: the main document's drawing, 1: the headers'
            size = _u32(data, at + 5)[0]
            body = data[at + 9:at + 9 + size]
            if label == 0:
                for k, _, b in records(body):
                    if k == 0xF003:  # the patriarch: its own shape first, then the top-level shapes
                        walk(b, None)
            at += 9 + size
        return out

    def boxes_in(self, p: _Paragraph) -> list[str]:
        """The text of the text boxes anchored in the paragraph, each once, in anchor order."""
        found: list[str] = []
        if self.boxes:
            if not hasattr(self, "_anchors"):
                self._anchors = sorted(self.boxes)
            anchors = self._anchors
            for cp in anchors[bisect_left(anchors, p.start):bisect_left(anchors, p.end)]:
                for text in self.boxes[cp]:
                    if text and text not in found:
                        found.append(text)
        return found

    @staticmethod
    def _cps_of(pieces, start: int, end: int) -> list[tuple[int, int]]:
        """The cp ranges of the text that the file offsets [start, end) hold. ``pieces`` is a
        _Pieces: the pieces ordered by where they lie in the file, found by bisection."""
        out = []
        if len(pieces.by_offset) <= 512:  # every piece looked at: exact, even where pieces share bytes
            i, stop = 0, len(pieces.by_offset)
        else:  # past that only a crafted file has so many: found by where they lie
            i, stop = max(0, bisect_right(pieces.offsets, start) - 1), bisect_left(pieces.offsets, end)
        for first, last, offset, width in pieces.by_offset[i:stop]:
            top = offset + (last - first) * width
            a, b = max(start, offset), min(end, top)
            if a < b:
                out.append((first + (a - offset) // width, first + -(-(b - offset) // width)))
        return out

    @staticmethod
    def _without_codes(text: str, dropped: list[tuple[int, int]]) -> str:
        """The text with fields' codes and their marks, and the dropped ranges, blanked to \\0
        (which the paragraphs then leave out): a field's result stays, as in a .docx, where the
        code is instruction text and the result is text. A ruby field, which has no result,
        becomes its reading and its base, as Word writes ruby in a .docx."""
        blank = list(dropped)  # (start, end) of the ranges taken out
        put: list[tuple[int, str]] = []  # (where, text) written over the start of a blanked range
        stack: list[list] = []  # [state, where its code starts], per open field
        codes = 0  # fields of the stack still in their code: inside one, a field's marks are code too
        for m in _FIELD.finditer(text):
            c, at = m.group(), m.start()
            hidden = codes > 0
            if c == "\x13":
                if len(stack) >= MAX_FIELD_DEPTH:
                    raise Unsupported("fields nested deeper than Word makes them")
                stack.append(["code", at])
                codes += 1
                blank.append((at, at + 1))
            elif c == "\x14" and stack:
                if stack[-1][0] == "code":
                    blank.append((stack[-1][1], at + 1))
                    codes -= 1
                stack[-1][0] = "result"
                blank.append((at, at + 1))
            elif c == "\x15" and stack:
                state, begin = stack.pop()
                blank.append((at, at + 1))
                if state == "code":
                    codes -= 1
                    blank.append((begin, at + 1))
                    # a ruby field is a few dozen characters: a long code is not one, and is not read through
                    code = text[begin + 1:at] if at - begin <= MAX_RUBY_CODE else ""
                    switches = sum(1 for s in _RUBY_SWITCHES if s.search(code))
                    if _RUBY_LIKE.search(code) and not _COMBINED.search(code) and switches:
                        if switches < len(_RUBY_SWITCHES):
                            raise Unsupported("ruby written in a way not read here")
                        ruby = _RUBY.fullmatch(code) if "\x13" not in code else None
                        if ruby is None:
                            raise Unsupported("ruby written in a way not read here")
                        put.append((begin, ruby.group(1) + ruby.group(2)))
            elif hidden:
                blank.append((at, at + 1))
        if stack:
            raise Unsupported("a field is not closed")
        if not blank:
            return text
        # the blanked ranges, joined, rebuilt with \0 of the same length (cps stay where they were)
        blank.sort()
        merged: list[list[int]] = []
        for a, b in blank:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        put.sort()
        parts, at, k = [], 0, 0  # k walks the sorted writes once, along the sorted ranges
        for a, b in merged:
            parts.append(text[at:a])
            over = []
            while k < len(put) and put[k][0] < b:
                if put[k][0] >= a:
                    over.append(put[k][1])
                k += 1
            written = "".join(over)[: b - a]
            parts += [written, "\0" * (b - a - len(written))]
            at = b
        parts.append(text[at:])
        return "".join(parts)

    def _huge(self, operand: bytes) -> bytes:
        fc = _u32(operand, 0)[0]
        size = _at(_u16, self.data_stream, fc, "a long paragraph property list")
        return self.data_stream[fc + 2:fc + 2 + size]

    # -- styles

    def _chain(self, props: dict[int, bytes], istd: int):
        """The paragraph's own properties, then its style's, then each base style's."""
        yield props
        seen = set()
        while istd in self.styles and istd not in seen:
            seen.add(istd)
            style = self.styles[istd]
            yield style.papx
            istd = style.base

    def heading_level(self, p: _Paragraph) -> int | None:
        for props in self._chain(p.props, p.istd):
            if _P_OUTLINE in props:
                level = props[_P_OUTLINE][0]
                return level + 1 if level < 9 else None
        style = self.styles.get(p.istd)
        if style is None:
            return None
        if 1 <= style.sti <= 9:  # a built-in heading style: .docx names it "heading N"
            return style.sti
        m = _HEADING_STYLE.match(style.name.split(",")[0])
        return int(m.group(1)) if m else None

    def numbered(self, p: _Paragraph) -> bool:
        for props in self._chain(p.props, p.istd):
            if _P_ILFO in props:
                return _u16(props[_P_ILFO], 0)[0] != 0
        return False

    def in_table(self, p: _Paragraph) -> bool:
        for props in self._chain(p.props, p.istd):
            if _P_IN_TABLE in props:
                return props[_P_IN_TABLE][0] != 0
        return False


# A table placed on the page (a floating table) and the rows around it are tables of their own
# to Word: rows are one table only while these, its placement, stay the same
_T_PLACEMENT = (0x360D, 0x940E, 0x940F, 0x9410, 0x9411, 0x941E, 0x941F, 0x3465)


@dataclass
class _Row:
    lefts: list[int]  # each cell's left edge (twips)
    merged: list[int]  # each cell's vertical merge: 0 none, 1 or 2 continues the cell above, 3 starts one
    placement: tuple  # the row's table placement (_T_PLACEMENT): a change starts another table


def _row(p: _Paragraph, cells: int) -> _Row:
    """A row's layout, from its row-end paragraph: refused when it does not match the cells
    it holds, or when its cells are merged the old way across (Word 97's merge)."""
    if _T_MERGE in p.props or _T_SPLIT in p.props:
        raise Unsupported("merged cells")
    table = p.props.get(_T_DEF_TABLE)
    if table is None:
        raise Unsupported("a table row without its definition")
    n = table[2] if len(table) > 2 else 0
    if n != cells:
        raise Unsupported("a table row's cells do not match its definition")
    if len(table) < 3 + 2 * (n + 1):
        raise Unsupported("a cut table definition")
    lefts = list(struct.unpack_from(f"<{n}h", table, 3))
    merged = [0] * n
    at = 3 + 2 * (n + 1)
    for i in range(n):
        if at + 20 * i + 2 > len(table):
            break  # cell definitions may be left off at the end; they are then plain cells
        flags = _u16(table, at + 20 * i)[0]
        if flags & 0x0003:
            raise Unsupported("cells merged across the old way")
        merged[i] = (flags >> 5) & 3
    for sprm, operand in _sprm_list(p.grpprl):
        if sprm == _T_VERT_MERGE and len(operand) >= 3 and operand[1] < n:
            merged[operand[1]] = operand[2]
    return _Row(lefts, merged, tuple(p.props.get(s) for s in _T_PLACEMENT))


class _Budget:
    def __init__(self, chars: int) -> None:
        self.left = chars

    def spend(self, chars: int) -> None:
        self.left -= chars
        if self.left < 0:
            raise ValueError(f"document text exceeds the {MAX_DOCUMENT_CHARS:,}-character limit")


def extract(path: Path) -> list[Chunk]:
    """The sections of a .doc, as the .docx reader reads the same document saved as .docx."""
    try:
        return _extract(path)
    except (IndexError, struct.error) as e:  # a count or an offset that points past what is there
        raise Unsupported(f"the file does not hold together as the format says ({type(e).__name__})") from e


class _Table:
    """A table being read at one depth: its rows so far, the row and the cell being read."""

    def __init__(self, depth: int) -> None:
        self.depth = depth
        self.rows: list[list[str]] = []
        self.cells: list[str] = []
        self.parts: list[str] = []  # of the cell being read: paragraphs, text boxes, inner tables' rows
        self.above: _Row | None = None  # the previous row, with its cells' text
        self.above_texts: list[str] = []

    def lines(self) -> list[str]:
        if self.parts or self.cells:
            raise Unsupported("a table ends inside a row")
        return table_lines(self.rows)

    def end_cell(self) -> None:
        size, kept = 0, []
        for part in self.parts:  # as the .docx reader: stop once the cell holds enough
            kept.append(part)
            size += len(part)
            if size >= MAX_CELL_CHARS:
                break
        self.cells.append(" ".join(kept)[:MAX_CELL_CHARS])
        self.parts = []


def _flag(props: dict, sprm: int) -> bool:
    """Whether a paragraph's one-byte property ``sprm`` is set (present and not zero)."""
    return props.get(sprm, b"\0")[:1] not in (b"\0", b"")


def _extract(path: Path) -> list[Chunk]:
    from docwhere.extractors.word import MAX_TABLE_DEPTH, TEXT_BOX, _HeadingNumberer
    from docwhere.index import MAX_FILE_BYTES

    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"over the {MAX_FILE_BYTES >> 20} MB limit on files read")
    doc = _Document(path.read_bytes())
    doc.read()
    builder = SectionBuilder()
    numberer = _HeadingNumberer()
    budget = _Budget(MAX_DOCUMENT_CHARS)
    open_tables: list[_Table] = []  # the table being read at each depth, outermost first

    def close() -> None:
        """The innermost table ends: its rows go to the cell around it, or to the text."""
        lines = open_tables.pop().lines()
        if open_tables:
            open_tables[-1].parts += lines  # each row a part of the outer cell, as the .docx reader reads it
        else:
            for line in lines:
                builder.add(line, {})

    for p in doc.paragraphs:
        boxes = doc.boxes_in(p)
        depth = 0
        if doc.in_table(p):
            itap = p.props.get(_P_ITAP)
            depth = max(1, _u32(itap, 0)[0]) if itap and len(itap) >= 4 else 1
        while open_tables and open_tables[-1].depth > depth:
            close()
        if depth > MAX_TABLE_DEPTH:
            continue  # a table nested deeper than the .docx reader reads: its text is not read
        while depth and (not open_tables or open_tables[-1].depth < depth):
            open_tables.append(_Table(open_tables[-1].depth + 1 if open_tables else 1))
        if not depth:
            budget.spend(len(p.text))
            level = doc.heading_level(p)
            if level and p.text.strip():
                builder.heading(level, numberer.label_for(doc.numbered(p), level) + p.text, {})
            else:
                builder.add(p.text, {})
            for text in boxes:
                budget.spend(len(text))
                builder.add(f"{TEXT_BOX} {text}", {})
            continue
        table = open_tables[-1]
        row_end = _flag(p.props, _P_TTP if depth == 1 else _P_INNER_TTP)
        if row_end:
            if boxes:
                raise Unsupported("a text box anchored at a row's end")
            if table.parts:
                raise Unsupported("a row ends inside a cell")
            row = _row(p, len(table.cells))
            if table.above is not None and row.placement != table.above.placement:
                # the rows so far are a table of their own
                done = _Table(depth)
                done.rows = table.rows
                lines = done.lines()
                if len(open_tables) > 1:
                    open_tables[-2].parts += lines
                else:
                    for line in lines:
                        builder.add(line, {})
                table.rows, table.above = [], None
            cells = table.cells
            for i, merge in enumerate(row.merged):
                if merge in (1, 2):  # continues the cell above: Word's .docx repeats that cell's text
                    if cells[i].strip():
                        raise Unsupported("text in a cell merged into the one above")
                    if table.above is None or row.lefts[i] not in table.above.lefts:
                        raise Unsupported("a merged cell with no cell above it")
                    cells[i] = table.above_texts[table.above.lefts.index(row.lefts[i])]
            budget.spend(sum(map(len, cells)) + MAX_HEADER_CHARS * len(cells))
            table.rows.append(cells)
            table.above, table.above_texts = row, cells
            table.cells = []
            continue
        table.parts.append(p.text)
        for text in boxes:
            budget.spend(len(text))
            table.parts.append(f"{TEXT_BOX} {text}")
        if (p.mark == "\x07") if depth == 1 else _flag(p.props, _P_INNER_CELL):
            table.end_cell()
    while open_tables:
        close()
    return builder.finish()
