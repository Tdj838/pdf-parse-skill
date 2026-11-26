#!/usr/bin/env python3
"""pdfparse.py -- dependency-free PDF structure extractor.

Design goals (see SKILL.md):
  * stdlib only: no PDF library, no poppler, no OCR binary is required.
  * every emitted character keeps a page + bounding box anchor, so a reader can
    jump back to the source position.
  * reading order is reconstructed with a recursive XY-cut, which is what fixes
    two-column / multi-column papers that naive extractors interleave.
  * anything the file does not actually say is reported as unknown instead of
    being guessed: undecodable glyphs become U+FFFD and are counted, pages with
    no text layer are flagged as needing OCR.

Usage:
    python3 pdfparse.py INPUT.pdf --out result.json
    python3 pdfparse.py INPUT.pdf --stdout --no-images
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sys
import time
import zlib
from dataclasses import dataclass, field

VERSION = "1.0.0"
REPLACEMENT = "\ufffd"
_DEBUG = os.environ.get("PDFPARSE_DEBUG") == "1"

# --------------------------------------------------------------------------
# 1. Lexer / object parser
# --------------------------------------------------------------------------

_WHITESPACE = b"\x00\t\n\x0c\r "
_DELIMS = b"()<>[]{}/%"
_NUM_RE = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")


class PDFError(Exception):
    pass


class Lexer:
    __slots__ = ("d", "p", "n")

    def __init__(self, data: bytes, pos: int = 0):
        self.d = data
        self.p = pos
        self.n = len(data)

    def skip(self) -> None:
        d, n = self.d, self.n
        p = self.p
        while p < n:
            c = d[p]
            if c in _WHITESPACE:
                p += 1
            elif c == 0x25:  # '%' comment
                while p < n and d[p] not in (0x0A, 0x0D):
                    p += 1
            else:
                break
        self.p = p

    def peek(self) -> int:
        self.skip()
        return self.d[self.p] if self.p < self.n else -1

    def read_name(self) -> str:
        d, n = self.d, self.n
        p = self.p + 1  # skip '/'
        out = bytearray()
        while p < n:
            c = d[p]
            if c in _WHITESPACE or c in b"()<>[]{}/%":
                break
            if c == 0x23 and p + 2 < n:  # '#XX'
                try:
                    out.append(int(d[p + 1:p + 3], 16))
                    p += 3
                    continue
                except ValueError:
                    pass
            out.append(c)
            p += 1
        self.p = p
        return out.decode("latin-1")

    def read_literal_string(self) -> bytes:
        d, n = self.d, self.n
        p = self.p + 1
        depth = 1
        out = bytearray()
        while p < n:
            c = d[p]
            if c == 0x5C:  # backslash
                p += 1
                if p >= n:
                    break
                e = d[p]
                if e in b"nrtbf":
                    out.append({0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12}[e])
                    p += 1
                elif e in b"()\\":
                    out.append(e)
                    p += 1
                elif 0x30 <= e <= 0x37:  # octal
                    oct_digits = bytearray()
                    while p < n and len(oct_digits) < 3 and 0x30 <= d[p] <= 0x37:
                        oct_digits.append(d[p])
                        p += 1
                    out.append(int(oct_digits, 8) & 0xFF)
                elif e in (0x0A, 0x0D):  # line continuation
                    p += 1
                    if e == 0x0D and p < n and d[p] == 0x0A:
                        p += 1
                else:
                    out.append(e)
                    p += 1
                continue
            if c == 0x28:
                depth += 1
                out.append(c)
                p += 1
                continue
            if c == 0x29:
                depth -= 1
                if depth == 0:
                    p += 1
                    break
                out.append(c)
                p += 1
                continue
            out.append(c)
            p += 1
        self.p = p
        return bytes(out)

    def read_hex_string(self) -> bytes:
        d, n = self.d, self.n
        p = self.p + 1
        end = d.find(b">", p)
        if end < 0:
            end = n
        raw = bytes(ch for ch in d[p:end] if ch in b"0123456789abcdefABCDEF")
        self.p = min(end + 1, n)
        if len(raw) % 2:
            raw += b"0"
        return bytes.fromhex(raw.decode("ascii")) if raw else b""


def parse_value(lx: Lexer):
    """Parse one PDF object. Returns Ref for indirect references."""
    lx.skip()
    if lx.p >= lx.n:
        raise PDFError("unexpected end of data")
    d = lx.d
    c = d[lx.p]

    if c == 0x2F:  # '/'
        return lx.read_name()
    if c == 0x28:  # '('
        return lx.read_literal_string()
    if c == 0x3C:  # '<'
        if lx.p + 1 < lx.n and d[lx.p + 1] == 0x3C:
            lx.p += 2
            out = {}
            while True:
                lx.skip()
                if lx.p >= lx.n:
                    break
                if d[lx.p] == 0x3E and lx.p + 1 < lx.n and d[lx.p + 1] == 0x3E:
                    lx.p += 2
                    break
                key = parse_value(lx)
                if not isinstance(key, str):
                    continue
                try:
                    val = parse_value(lx)
                except PDFError:
                    break
                out[key] = val
            return out
        return lx.read_hex_string()
    if c == 0x5B:  # '['
        lx.p += 1
        out = []
        while True:
            lx.skip()
            if lx.p >= lx.n:
                break
            if d[lx.p] == 0x5D:
                lx.p += 1
                break
            try:
                out.append(parse_value(lx))
            except PDFError:
                break
        return out

    # keyword or number
    m = _NUM_RE.match(d, lx.p)
    if m:
        raw = m.group(0)
        num: object
        try:
            num = int(raw) if b"." not in raw else float(raw)
        except ValueError:
            num = 0
        lx.p = m.end()
        # indirect reference: "num gen R"
        save = lx.p
        try:
            lx.skip()
            m2 = _NUM_RE.match(lx.d, lx.p)
            if m2:
                lx.p = m2.end()
                lx.skip()
                if lx.p < lx.n and lx.d[lx.p] == 0x52:  # 'R'
                    lx.p += 1
                    return Ref(int(num), int(m2.group(0)))
        except PDFError:
            pass
        lx.p = save
        return num

    # bare keyword
    start = lx.p
    while lx.p < lx.n and lx.d[lx.p] not in _WHITESPACE and lx.d[lx.p] not in _DELIMS:
        lx.p += 1
    word = d[start:lx.p].decode("latin-1", "replace")
    if word == "true":
        return True
    if word == "false":
        return False
    if word == "null":
        return None
    if lx.p == start:  # unknown delimiter, consume it to make progress
        lx.p += 1
    return Keyword(word)


class Keyword(str):
    pass


@dataclass(frozen=True)
class Ref:
    num: int
    gen: int = 0


@dataclass
class Stream:
    dict: dict
    raw: bytes

    def decoded(self) -> bytes:
        return decode_stream(self.dict, self.raw)


def decode_stream(sdict: dict, raw: bytes) -> bytes:
    filters = sdict.get("Filter")
    if filters is None:
        return raw
    if isinstance(filters, str):
        filters = [filters]
    data = raw
    for f in filters:
        if f in ("FlateDecode", "Fl"):
            data = _inflate(data)
        elif f in ("ASCIIHexDecode", "AHx"):
            hex_chars = bytes(c for c in data if c in b"0123456789abcdefABCDEF")
            if len(hex_chars) % 2:
                hex_chars += b"0"
            data = bytes.fromhex(hex_chars.decode("ascii"))
        elif f in ("ASCII85Decode", "A85"):
            data = _ascii85(data)
        else:
            # Unsupported filter (DCT/JPX/CCITT/LZW): the caller reports it.
            raise PDFError("unsupported filter: %s" % f)
    return data


def _inflate(data: bytes) -> bytes:
    try:
        return zlib.decompress(data)
    except zlib.error:
        pass
    for wbits in (15, -15, 31):
        try:
            return zlib.decompressobj(wbits).decompress(data)
        except zlib.error:
            continue
    raise PDFError("flate decode failed")


def _ascii85(data: bytes) -> bytes:
    import base64

    s = data.strip()
    if s.startswith(b"<~"):
        s = s[2:]
    idx = s.find(b"~>")
    if idx >= 0:
        s = s[:idx]
    return base64.a85decode(s, adobe=False)


# --------------------------------------------------------------------------
# 2. Document
# --------------------------------------------------------------------------

_OBJ_RE = re.compile(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b")


class PDFDocument:
    def __init__(self, data: bytes):
        self.data = data
        self.objects: dict[int, object] = {}
        self.trailers: list[dict] = []
        self.repairs: list[str] = []
        self._scan()
        self._load_object_streams()
        self.pages: list[dict] = []
        self._load_pages()

    # -- object scanning ---------------------------------------------------
    def _scan(self) -> None:
        data = self.data
        consumed: list[tuple[int, int]] = []
        for m in _OBJ_RE.finditer(data):
            start = m.start()
            if any(a <= start < b for a, b in consumed):
                continue
            num = int(m.group(1))
            lx = Lexer(data, m.end())
            try:
                obj = parse_value(lx)
            except PDFError:
                continue
            lx.skip()
            if isinstance(obj, dict) and data.startswith(b"stream", lx.p):
                p = lx.p + 6
                if data[p:p + 2] == b"\r\n":
                    p += 2
                elif data[p:p + 1] in (b"\n", b"\r"):
                    p += 1
                length = obj.get("Length")
                end = None
                if isinstance(length, int) and 0 <= length <= len(data) - p:
                    tail = data[p + length:p + length + 20]
                    if tail.lstrip().startswith(b"endstream"):
                        end = p + length
                if end is None:
                    idx = data.find(b"endstream", p)
                    end = idx if idx >= 0 else len(data)
                    while end > p and data[end - 1] in b"\r\n":
                        end -= 1
                obj = Stream(obj, data[p:end])
                consumed.append((start, min(end + 9, len(data))))
            self.objects[num] = obj

        for m in re.finditer(rb"trailer\s*<<", data):
            try:
                t = parse_value(Lexer(data, m.end() - 2))
                if isinstance(t, dict):
                    self.trailers.append(t)
            except PDFError:
                pass
        # xref-stream trailers
        for num, obj in list(self.objects.items()):
            if isinstance(obj, Stream) and obj.dict.get("Type") == "XRef":
                self.trailers.append(obj.dict)
        if not self.trailers:
            self.repairs.append("no trailer found; rebuilt from scanned objects")

    def _load_object_streams(self) -> None:
        for num, obj in list(self.objects.items()):
            if not isinstance(obj, Stream) or obj.dict.get("Type") != "ObjStm":
                continue
            try:
                body = obj.decoded()
            except PDFError:
                self.repairs.append("object stream %d could not be decoded" % num)
                continue
            count = self.resolve(obj.dict.get("N"))
            first = self.resolve(obj.dict.get("First"))
            if not isinstance(count, int) or not isinstance(first, int):
                continue
            lx = Lexer(body, 0)
            header = []
            for _ in range(count):
                try:
                    onum = parse_value(lx)
                    ooff = parse_value(lx)
                except PDFError:
                    break
                if isinstance(onum, int) and isinstance(ooff, int):
                    header.append((onum, ooff))
            for onum, ooff in header:
                if onum in self.objects:
                    continue
                try:
                    self.objects[onum] = parse_value(Lexer(body, first + ooff))
                except PDFError:
                    continue

    # -- resolution --------------------------------------------------------
    def resolve(self, obj, depth: int = 0):
        while isinstance(obj, Ref) and depth < 32:
            obj = self.objects.get(obj.num)
            depth += 1
        return obj

    def get(self, d: dict, key: str, default=None):
        if not isinstance(d, dict):
            return default
        return self.resolve(d.get(key, default))

    def catalog(self) -> dict:
        for t in reversed(self.trailers):
            root = self.resolve(t.get("Root")) if isinstance(t, dict) else None
            if isinstance(root, dict):
                return root
        # fallback: any object that looks like a catalog
        for obj in self.objects.values():
            if isinstance(obj, dict) and obj.get("Type") == "Catalog":
                return obj
        raise PDFError("no document catalog")

    def _load_pages(self) -> None:
        root = self.catalog()
        tree = self.resolve(root.get("Pages"))
        out: list[dict] = []
        seen: set[int] = set()

        def walk(node, inherited: dict, depth: int = 0):
            node = self.resolve(node)
            if not isinstance(node, dict) or depth > 64:
                return
            inherits = dict(inherited)
            for key in ("Resources", "MediaBox", "CropBox", "Rotate"):
                if key in node:
                    inherits[key] = self.resolve(node.get(key))
            kids = self.resolve(node.get("Kids"))
            if isinstance(kids, list):
                for kid in kids:
                    if isinstance(kid, Ref):
                        if kid.num in seen:
                            continue
                        seen.add(kid.num)
                    walk(kid, inherits, depth + 1)
            elif node.get("Type") == "Page" or "Contents" in node:
                page = dict(node)
                page["_inherited"] = inherits
                out.append(page)

        walk(tree, {})
        if not out:
            # broken page tree: fall back to every page object in the file
            for num in sorted(self.objects):
                obj = self.objects[num]
                if isinstance(obj, dict) and obj.get("Type") == "Page":
                    obj = dict(obj)
                    obj["_inherited"] = {}
                    out.append(obj)
        self.pages = out

    def page_count(self) -> int:
        return len(self.pages)

    def page_size(self, page: dict) -> tuple[float, float]:
        box = self.get(page, "CropBox") or self.get(page, "MediaBox") or page["_inherited"].get("MediaBox")
        if isinstance(box, list) and len(box) == 4:
            try:
                x0, y0, x1, y1 = (float(self.resolve(v)) for v in box)
                if x1 > x0 and y1 > y0:
                    return x1 - x0, y1 - y0
            except (TypeError, ValueError):
                pass
        return 612.0, 792.0

    def page_content(self, page: dict) -> bytes:
        contents = self.resolve(page.get("Contents"))
        if contents is None:
            return b""
        if isinstance(contents, Stream):
            contents = [contents]
        if not isinstance(contents, list):
            return b""
        chunks = []
        for c in contents:
            c = self.resolve(c)
            if isinstance(c, Stream):
                try:
                    chunks.append(c.decoded())
                except PDFError:
                    self.repairs.append("content stream could not be decoded")
        return b"\n".join(chunks)

    def page_resources(self, page: dict) -> dict:
        res = self.get(page, "Resources")
        if res is None:
            res = page["_inherited"].get("Resources")
        return res if isinstance(res, dict) else {}

    def metadata(self) -> dict:
        info = {}
        for t in self.trailers:
            info_obj = self.resolve(t.get("Info")) if isinstance(t, dict) else None
            if isinstance(info_obj, dict):
                info = info_obj
                break
        out = {}
        for key in ("Title", "Author", "Subject", "Producer", "Creator", "CreationDate"):
            val = self.resolve(info.get(key))
            if isinstance(val, bytes):
                out[key.lower()] = _pdf_text_string(val)
            elif isinstance(val, str):
                out[key.lower()] = val
        return out


def _pdf_text_string(raw: bytes) -> str:
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace")
    return raw.decode("latin-1", "replace")


# --------------------------------------------------------------------------
# 3. Glyph name / encoding tables
# --------------------------------------------------------------------------

_AGL = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$",
    "percent": "%", "ampersand": "&", "quotesingle": "'", "parenleft": "(",
    "parenright": ")", "asterisk": "*", "plus": "+", "comma": ",",
    "hyphen": "-", "period": ".", "slash": "/", "zero": "0", "one": "1",
    "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "colon": ":", "semicolon": ";",
    "less": "<", "equal": "=", "greater": ">", "question": "?", "at": "@",
    "bracketleft": "[", "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|",
    "braceright": "}", "asciitilde": "~",
    "exclamdown": "¡", "cent": "¢", "sterling": "£", "currency": "¤",
    "yen": "¥", "brokenbar": "¦", "section": "§", "dieresis": "¨",
    "copyright": "©", "ordfeminine": "ª", "guillemotleft": "«", "logicalnot": "¬",
    "registered": "®", "macron": "¯", "degree": "°", "plusminus": "±",
    "twosuperior": "²", "threesuperior": "³", "acute": "´", "mu": "µ",
    "paragraph": "¶", "periodcentered": "·", "cedilla": "¸", "onesuperior": "¹",
    "ordmasculine": "º", "guillemotright": "»", "onequarter": "¼",
    "onehalf": "½", "threequarters": "¾", "questiondown": "¿",
    "Agrave": "À", "Aacute": "Á", "Acircumflex": "Â", "Atilde": "Ã",
    "Adieresis": "Ä", "Aring": "Å", "AE": "Æ", "Ccedilla": "Ç", "Egrave": "È",
    "Eacute": "É", "Ecircumflex": "Ê", "Edieresis": "Ë", "Igrave": "Ì",
    "Iacute": "Í", "Icircumflex": "Î", "Idieresis": "Ï", "Eth": "Ð",
    "Ntilde": "Ñ", "Ograve": "Ò", "Oacute": "Ó", "Ocircumflex": "Ô",
    "Otilde": "Õ", "Odieresis": "Ö", "multiply": "×", "Oslash": "Ø",
    "Ugrave": "Ù", "Uacute": "Ú", "Ucircumflex": "Û", "Udieresis": "Ü",
    "Yacute": "Ý", "Thorn": "Þ", "germandbls": "ß", "agrave": "à",
    "aacute": "á", "acircumflex": "â", "atilde": "ã", "adieresis": "ä",
    "aring": "å", "ae": "æ", "ccedilla": "ç", "egrave": "è", "eacute": "é",
    "ecircumflex": "ê", "edieresis": "ë", "igrave": "ì", "iacute": "í",
    "icircumflex": "î", "idieresis": "ï", "eth": "ð", "ntilde": "ñ",
    "ograve": "ò", "oacute": "ó", "ocircumflex": "ô", "otilde": "õ",
    "odieresis": "ö", "divide": "÷", "oslash": "ø", "ugrave": "ù",
    "uacute": "ú", "ucircumflex": "û", "udieresis": "ü", "yacute": "ý",
    "thorn": "þ", "ydieresis": "ÿ",
    "dotlessi": "ı", "dotlessj": "ȷ", "Lslash": "Ł", "lslash": "ł",
    "OE": "Œ", "oe": "œ", "Scaron": "Š", "scaron": "š", "Zcaron": "Ž",
    "zcaron": "ž", "Ydieresis": "Ÿ", "florin": "ƒ", "circumflex": "ˆ",
    "caron": "ˇ", "breve": "˘", "dotaccent": "˙", "ring": "˚",
    "ogonek": "˛", "tilde": "˜", "hungarumlaut": "˝", "endash": "–",
    "emdash": "—", "quotesinglbase": "‚", "quotedblbase": "„",
    "quotedblleft": "“", "quotedblright": "”", "quoteleft": "‘",
    "quoteright": "’", "guilsinglleft": "‹", "guilsinglright": "›",
    "dagger": "†", "daggerdbl": "‡", "bullet": "•", "ellipsis": "…",
    "perthousand": "‰", "trademark": "™", "minus": "−", "fraction": "⁄",
    "fi": "fi", "fl": "fl", "ff": "ff", "ffi": "ffi", "ffl": "ffl",
    "dotmath": "⋅", "periodcentered": "·", "middot": "·",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "epsilon1": "ϵ", "zeta": "ζ", "eta": "η",
    "theta": "θ", "theta1": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "nu": "ν", "xi": "ξ", "omicron": "ο", "pi": "π",
    "pi1": "ϖ", "rho": "ρ", "rho1": "ϱ", "sigma": "σ", "sigma1": "ς",
    "tau": "τ", "upsilon": "υ", "phi": "φ", "phi1": "ϕ", "chi": "χ",
    "psi": "ψ", "omega": "ω",
    "Alpha": "Α", "Beta": "Β", "Gamma": "Γ", "Delta": "Δ",
    "Epsilon": "Ε", "Zeta": "Ζ", "Eta": "Η", "Theta": "Θ",
    "Iota": "Ι", "Kappa": "Κ", "Lambda": "Λ", "Mu": "Μ", "Nu": "Ν",
    "Xi": "Ξ", "Omicron": "Ο", "Pi": "Π", "Rho": "Ρ", "Sigma": "Σ",
    "Tau": "Τ", "Upsilon": "Υ", "Phi": "Φ", "Chi": "Χ", "Psi": "Ψ",
    "Omega": "Ω",
    "summation": "∑", "product": "∏", "integral": "∫", "radical": "√",
    "infinity": "∞", "partialdiff": "∂", "gradient": "∇", "nabla": "∇",
    "approxequal": "≈", "lessequal": "≤", "greaterequal": "≥",
    "notequal": "≠", "equivalence": "≡", "congruent": "≅",
    "similar": "∼", "proportional": "∝", "perpendicular": "⊥",
    "angle": "∠", "element": "∈", "notelement": "∉", "union": "∪",
    "intersection": "∩", "logicaland": "∧", "logicalor": "∨",
    "arrowright": "→", "arrowleft": "←", "arrowup": "↑", "arrowdown": "↓",
    "arrowboth": "↔", "arrowdblright": "⇒", "arrowdblleft": "⇐",
    "arrowdblboth": "⇔", "prime": "′", "second": "″",
    "asteriskmath": "∗", "circleplus": "⊕", "circlemultiply": "⊗",
    "emptyset": "∅", "universal": "∀", "existential": "∃",
    "therefore": "∴", "propersubset": "⊂", "propersuperset": "⊃",
    "reflexsubset": "⊆", "reflexsuperset": "⊇", "aleph": "ℵ",
    "Ifraktur": "ℑ", "Rfraktur": "ℜ", "weierstrass": "℘",
    "lozenge": "◊", "spade": "♠", "club": "♣", "heart": "♥", "diamond": "♦",
    "dagger1": "†", "quoteleft1": "‘", "cwm": "", "compwordmark": "",
    "perthousandzero": "",
}

# StandardEncoding slots that differ from Latin-1 (only the ones pdfTeX uses).
_STANDARD_ENCODING = {
    0x27: "quoteright", 0x60: "quoteleft", 0xA1: "exclamdown", 0xA2: "cent",
    0xA3: "sterling", 0xA4: "fraction", 0xA5: "yen", 0xA6: "florin",
    0xA7: "section", 0xA8: "currency", 0xA9: "quotesingle",
    0xAA: "quotedblleft", 0xAB: "guillemotleft", 0xAC: "guilsinglleft",
    0xAD: "guilsinglright", 0xAE: "fi", 0xAF: "fl", 0xB1: "endash",
    0xB2: "dagger", 0xB3: "daggerdbl", 0xB4: "periodcentered", 0xB6: "paragraph",
    0xB7: "bullet", 0xB8: "quotesinglbase", 0xB9: "quotedblbase",
    0xBA: "quotedblright", 0xBB: "guillemotright", 0xBC: "ellipsis",
    0xBD: "perthousand", 0xBF: "questiondown", 0xC1: "grave", 0xC2: "acute",
    0xC3: "circumflex", 0xC4: "tilde", 0xC5: "macron", 0xC6: "breve",
    0xC7: "dotaccent", 0xC8: "dieresis", 0xCA: "ring", 0xCB: "cedilla",
    0xCD: "hungarumlaut", 0xCE: "ogonek", 0xCF: "caron", 0xD0: "emdash",
    0xE1: "AE", 0xE3: "ordfeminine", 0xE8: "Lslash", 0xE9: "Oslash",
    0xEA: "OE", 0xEB: "ordmasculine", 0xF1: "ae", 0xF5: "dotlessi",
    0xF8: "lslash", 0xF9: "oslash", 0xFA: "oe", 0xFB: "germandbls",
}


def glyph_to_text(name: str) -> str | None:
    if name in _AGL:
        return _AGL[name]
    if len(name) == 1:
        return name
    m = re.fullmatch(r"uni([0-9A-Fa-f]{4,6})", name)
    if m:
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return None
    m = re.fullmatch(r"u([0-9A-Fa-f]{4,6})", name)
    if m:
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return None
    # /g23 or /cid45 style subset names carry no information
    return None


def _base_encoding(name: str) -> dict[int, str]:
    table: dict[int, str] = {}
    if name == "StandardEncoding":
        for code in range(32, 127):
            table[code] = chr(code)
        for code, gname in _STANDARD_ENCODING.items():
            glyph = glyph_to_text(gname)
            if glyph is not None:
                table[code] = glyph
    else:  # WinAnsiEncoding / MacRomanEncoding / missing -> cp1252 as best effort
        for code in range(256):
            try:
                table[code] = bytes([code]).decode("cp1252")
            except UnicodeDecodeError:
                continue
    return table


# --------------------------------------------------------------------------
# 4. Fonts
# --------------------------------------------------------------------------

@dataclass
class Font:
    base_name: str = "unknown"
    subtype: str = "Type1"
    two_byte: bool = False
    cmap: dict[int, str] = field(default_factory=dict)
    encoding: dict[int, str] = field(default_factory=dict)
    widths: dict[int, float] = field(default_factory=dict)
    default_width: float = 500.0
    vertical: bool = False
    symbol: bool = False

    def decode(self, raw: bytes) -> list[tuple[str, int, float]]:
        """Return (text, code, width_in_text_space_units) per glyph."""
        out = []
        if self.two_byte:
            for i in range(0, len(raw) - 1, 2):
                code = (raw[i] << 8) | raw[i + 1]
                text = self.cmap.get(code)
                if text is None:
                    text = REPLACEMENT
                out.append((text, code, self.widths.get(code, self.default_width)))
            if len(raw) % 2:
                out.append((REPLACEMENT, raw[-1], self.default_width))
        else:
            for code in raw:
                text = self.cmap.get(code)
                if text is None:
                    text = self.encoding.get(code)
                if text is None:
                    text = REPLACEMENT
                out.append((text, code, self.widths.get(code, self.default_width)))
        return out


def _hex_to_str(h: str) -> str:
    b = bytes.fromhex(h if len(h) % 2 == 0 else h + "0")
    if len(b) == 1:
        return chr(b[0])
    try:
        return b.decode("utf-16-be")
    except UnicodeDecodeError:
        try:
            return b.decode("utf-16-be", "replace")
        except Exception:
            return REPLACEMENT


def parse_tounicode(data: bytes) -> dict[int, str]:
    text = data.decode("latin-1", "replace")
    cmap: dict[int, str] = {}
    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, re.S):
        for src, dst in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            try:
                cmap[int(src, 16)] = _hex_to_str(dst)
            except ValueError:
                continue
    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, re.S):
        for m in re.finditer(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<([0-9A-Fa-f]+)>|\[([^\]]*)\])", block, re.S):
            try:
                lo = int(m.group(1), 16)
                hi = int(m.group(2), 16)
            except ValueError:
                continue
            if m.group(3).startswith("["):
                items = re.findall(r"<([0-9A-Fa-f]+)>", m.group(5) or "")
                for i, h in enumerate(items):
                    if 0 <= lo + i <= hi:
                        cmap[lo + i] = _hex_to_str(h)
            else:
                base = m.group(4)
                width = len(base) // 4  # UTF-16 units
                b = int(base, 16)
                for offset in range(0, min(hi - lo + 1, 65536)):
                    cmap[lo + offset] = _hex_to_str("%0*x" % (len(base), b + offset)) if width else REPLACEMENT
    return cmap


def build_font(doc: PDFDocument, font_dict: dict) -> Font:
    font = Font()
    font.subtype = doc.get(font_dict, "Subtype") or "Type1"
    font.base_name = doc.get(font_dict, "BaseFont") or "unknown"
    font.two_byte = font.subtype == "Type0"

    tu = doc.get(font_dict, "ToUnicode")
    if isinstance(tu, Stream):
        try:
            font.cmap = parse_tounicode(tu.decoded())
        except PDFError:
            doc.repairs.append("ToUnicode stream undecodable in %s" % font.base_name)

    encoding = doc.get(font_dict, "Encoding")
    base_name = "WinAnsiEncoding"
    diffs = None
    if isinstance(encoding, str):
        base_name = encoding
    elif isinstance(encoding, dict):
        base_name = doc.get(encoding, "BaseEncoding") or "WinAnsiEncoding"
        diffs = doc.get(encoding, "Differences")
    font.encoding = _base_encoding(base_name)
    if isinstance(diffs, list):
        code = 0
        for item in diffs:
            item = doc.resolve(item)
            if isinstance(item, int):
                code = item
            elif isinstance(item, str):
                glyph = glyph_to_text(item)
                if glyph is None:
                    font.encoding.pop(code, None)
                else:
                    font.encoding[code] = glyph
                code += 1

    # widths
    if font.two_byte:
        desc = doc.get(font_dict, "DescendantFonts")
        desc_font = {}
        if isinstance(desc, list) and desc:
            desc_font = doc.resolve(desc[0]) or {}
        font.default_width = float(doc.get(desc_font, "DW", 1000) or 1000)
        w = doc.get(desc_font, "W")
        if isinstance(w, list):
            i = 0
            while i < len(w):
                first = doc.resolve(w[i])
                nxt = doc.get({"x": w[i + 1] if i + 1 < len(w) else None}, "x")
                if isinstance(nxt, list):
                    for k, width in enumerate(nxt):
                        if isinstance(first, int):
                            font.widths[first + k] = float(doc.resolve(width) or 0)
                    i += 2
                elif isinstance(first, int) and isinstance(nxt, int) and i + 2 < len(w):
                    last = nxt
                    width = float(doc.resolve(w[i + 2]) or 0)
                    for c in range(first, last + 1):
                        font.widths[c] = width
                    i += 3
                else:
                    i += 1
    else:
        first = doc.get(font_dict, "FirstChar")
        widths = doc.get(font_dict, "Widths")
        if isinstance(first, int) and isinstance(widths, list):
            for k, width in enumerate(widths):
                width = doc.resolve(width)
                if isinstance(width, (int, float)):
                    font.widths[first + k] = float(width)
        fd = doc.get(font_dict, "FontDescriptor")
        if isinstance(fd, dict):
            missing = doc.get(fd, "MissingWidth")
            if isinstance(missing, (int, float)) and missing:
                font.default_width = float(missing)
            flags = doc.get(fd, "Flags")
            if isinstance(flags, int) and flags & 4:  # symbolic
                font.symbol = True
    if "/" in font.base_name:
        pass
    return font


# --------------------------------------------------------------------------
# 5. Content stream interpretation
# --------------------------------------------------------------------------

@dataclass
class Glyph:
    page: int
    text: str
    x0: float
    x1: float
    y: float
    size: float
    font: str
    ordered: int


@dataclass
class ImageRef:
    page: int
    name: str
    width: int
    height: int
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 0.0


def _num(v, default=0.0) -> float:
    return float(v) if isinstance(v, (int, float)) else default


def iter_content_ops(data: bytes):
    """Yield (operator, operands) from a content stream."""
    lx = Lexer(data, 0)
    operands: list = []
    while True:
        lx.skip()
        if lx.p >= lx.n:
            return
        c = data[lx.p]
        if c in b"([/<" or c in b"+-." or 0x30 <= c <= 0x39:
            try:
                operands.append(parse_value(lx))
            except PDFError:
                lx.p += 1
                operands = []
            continue
        start = lx.p
        while lx.p < lx.n and data[lx.p] not in _WHITESPACE and data[lx.p] not in _DELIMS:
            lx.p += 1
        if lx.p == start:
            lx.p += 1
            continue
        op = data[start:lx.p].decode("latin-1")
        yield op, operands
        operands = []


def extract_glyphs(doc: PDFDocument, page_index: int, page: dict, resources: dict,
                   font_cache: dict) -> tuple[list[Glyph], list[ImageRef], dict]:
    content = doc.page_content(page)
    glyphs: list[Glyph] = []
    images: list[ImageRef] = []
    stats = {"rotated_runs": 0, "unsupported_filters": 0, "vector_ops": 0}

    def load_font(name: str) -> Font:
        fonts = doc.get(resources, "Font") or {}
        ref = fonts.get(name) if isinstance(fonts, dict) else None
        key = ref.num if isinstance(ref, Ref) else name
        if key in font_cache:
            return font_cache[key]
        fdict = doc.resolve(ref) if ref is not None else None
        font = build_font(doc, fdict) if isinstance(fdict, dict) else Font()
        font_cache[key] = font
        return font

    def xobject(name: str):
        xo = doc.get(resources, "XObject") or {}
        ref = xo.get(name) if isinstance(xo, dict) else None
        return doc.resolve(ref) if ref is not None else None

    tm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    tlm = list(tm)
    font = Font()
    size = 12.0
    char_spacing = 0.0
    word_spacing = 0.0
    h_scale = 1.0
    leading = 0.0
    order = 0

    stack = []

    def emit(text: str, adv: float):
        nonlocal order
        a, b, c, d, e, f = tm
        scale = (a * a + b * b) ** 0.5 or 1.0
        eff = size * scale
        if abs(b) > 1e-3 or abs(c) > 1e-3:
            # Rotated text (arXiv side stamps, vertical labels) is not part of the
            # reading order; keep it aside as evidence instead of interleaving it.
            stats["rotated_runs"] += 1
            stats.setdefault("rotated_texts", []).append(text)
            order += 1
            return
        x0 = e
        x1 = e + adv
        if adv < 0:
            x0, x1 = x1, x0
        glyphs.append(Glyph(page_index, text, x0, x1, f, eff, font.base_name, order))
        order += 1

    for op, args in iter_content_ops(content):
        if op == "q":
            stack.append((tm, font, size, char_spacing, word_spacing, h_scale, leading))
        elif op == "Q":
            if stack:
                tm, font, size, char_spacing, word_spacing, h_scale, leading = stack.pop()
        elif op == "BT":
            tm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
            tlm = list(tm)
        elif op == "ET":
            pass
        elif op == "Tf" and len(args) >= 2:
            font = load_font(str(args[0]))
            size = _num(doc.resolve(args[1]), size)
        elif op == "Tc" and args:
            char_spacing = _num(doc.resolve(args[0]))
        elif op == "Tw" and args:
            word_spacing = _num(doc.resolve(args[0]))
        elif op == "Tz" and args:
            h_scale = _num(doc.resolve(args[0]), 100.0) / 100.0
        elif op == "TL" and args:
            leading = _num(doc.resolve(args[0]))
        elif op == "Td" and len(args) >= 2:
            tx, ty = _num(doc.resolve(args[0])), _num(doc.resolve(args[1]))
            tlm = [tlm[0], tlm[1], tlm[2], tlm[3], tlm[4] + tx * tlm[0], tlm[5] + ty * tlm[3]]
            tm = list(tlm)
        elif op == "TD" and len(args) >= 2:
            tx, ty = _num(doc.resolve(args[0])), _num(doc.resolve(args[1]))
            leading = -ty
            tlm = [tlm[0], tlm[1], tlm[2], tlm[3], tlm[4] + tx * tlm[0], tlm[5] + ty * tlm[3]]
            tm = list(tlm)
        elif op == "Tm" and len(args) >= 6:
            tlm = [_num(doc.resolve(v)) for v in args[:6]]
            tm = list(tlm)
        elif op == "T*":
            ty = -leading
            tlm = [tlm[0], tlm[1], tlm[2], tlm[3], tlm[4], tlm[5] + ty * tlm[3]]
            tm = list(tlm)
        elif op in ("Tj", "'", '"'):
            if op != "Tj":
                ty = -leading
                tlm = [tlm[0], tlm[1], tlm[2], tlm[3], tlm[4], tlm[5] + ty * tlm[3]]
                tm = list(tlm)
            if op == '"' and len(args) >= 3:
                word_spacing = _num(doc.resolve(args[0]))
                char_spacing = _num(doc.resolve(args[1]))
                arg = args[2]
            else:
                if not args:
                    continue
                arg = args[-1]
            if not isinstance(arg, bytes):
                continue
            for text, code, width in font.decode(arg):
                adv = (width / 1000.0 * size + char_spacing) * h_scale
                if code == 32:
                    adv += word_spacing * h_scale
                emit(text, adv)
                tm[4] += adv * tm[0]
                tm[5] += adv * tm[1]
        elif op == "TJ":
            if not args or not isinstance(args[0], list):
                continue
            for item in args[0]:
                if isinstance(item, bytes):
                    for text, code, width in font.decode(item):
                        adv = (width / 1000.0 * size + char_spacing) * h_scale
                        if code == 32:
                            adv += word_spacing * h_scale
                        emit(text, adv)
                        tm[4] += adv * tm[0]
                        tm[5] += adv * tm[1]
                elif isinstance(item, (int, float)):
                    shift = -item / 1000.0 * size * h_scale
                    if abs(shift) > 0:
                        tm[4] += shift * tm[0]
                        tm[5] += shift * tm[1]
        elif op == "Do" and args:
            name = str(args[0])
            obj = xobject(name)
            if isinstance(obj, Stream) and obj.dict.get("Subtype") == "Image":
                try:
                    w = int(doc.resolve(obj.dict.get("Width")) or 0)
                    h = int(doc.resolve(obj.dict.get("Height")) or 0)
                except (TypeError, ValueError):
                    w = h = 0
                a, b, c, d, e, f = tm
                images.append(ImageRef(page_index, name, w, h, e, f,
                                       e + a * 1.0, f + d * 1.0))
        elif op in ("m", "l", "c", "v", "y", "re", "S", "s", "f", "F", "f*", "B", "b"):
            stats["vector_ops"] += 1

    return glyphs, images, stats


# --------------------------------------------------------------------------
# 6. Line / block reconstruction
# --------------------------------------------------------------------------

@dataclass
class Line:
    page: int
    y: float
    x0: float
    x1: float
    size: float
    glyphs: list[Glyph]

    @property
    def bold(self) -> bool:
        names = [g.font.lower() for g in self.glyphs[:6]]
        hits = sum(1 for n in names if "bold" in n or "black" in n or "semibold" in n)
        return bool(names) and hits * 2 >= len(names)

    @property
    def text(self) -> str:
        out = []
        prev = None
        for g in self.glyphs:
            if prev is not None:
                gap = g.x0 - prev.x1
                if gap > 0.18 * max(g.size, 1.0) and out and not out[-1].endswith(" ") and g.text != " ":
                    out.append(" ")
                elif gap > 0.18 * max(g.size, 1.0) and g.text == " ":
                    pass
            if g.text != " ":
                out.append(g.text)
            elif out and not out[-1].endswith(" "):
                out.append(" ")
            prev = g
        return re.sub(r"[ \t]{2,}", " ", "".join(out)).strip()


def build_lines(glyphs: list[Glyph]) -> list[Line]:
    if not glyphs:
        return []
    ordered = sorted(glyphs, key=lambda g: (-round(g.y, 1), g.x0))
    lines: list[list[Glyph]] = []
    for g in ordered:
        if lines:
            last = lines[-1]
            ref = last[0]
            tol = max(2.0, 0.45 * max(ref.size, g.size))
            if abs(ref.y - g.y) <= tol:
                last.append(g)
                continue
        lines.append([g])
    result = []
    for group in lines:
        group.sort(key=lambda g: g.x0)
        sizes = sorted(g.size for g in group)
        size = sizes[len(sizes) // 2]
        result.append(Line(group[0].page, group[0].y,
                           min(g.x0 for g in group), max(g.x1 for g in group),
                           size, group))
    return result


@dataclass
class Block:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    lines: list[Line]
    kind: str = "paragraph"

    @property
    def text(self) -> str:
        return join_lines([l.text for l in self.lines])

    @property
    def size(self) -> float:
        sizes = [l.size for l in self.lines]
        return max(set(sizes), key=sizes.count) if sizes else 10.0


def join_lines(lines: list[str]) -> str:
    out = ""
    for i, raw in enumerate(lines):
        piece = raw.strip()
        if not piece:
            continue
        if not out:
            out = piece
            continue
        if re.search(r"[A-Za-z]-$", out) and piece[:1].islower():
            out = out[:-1] + piece  # de-hyphenate across the break
        elif out.endswith("-") and piece[:1].islower() and len(out) > 3 and out[-3:-2].isalpha():
            out = out[:-1] + piece
        else:
            out += " " + piece
    return out


def _gaps(spans: list[tuple[float, float]], min_gap: float) -> list[tuple[float, float, float]]:
    """Whitespace gaps between the union of 1-D spans, widest first."""
    out = []
    cur_end = spans[0][1]
    for a, b in spans[1:]:
        if a - cur_end >= min_gap:
            out.append((a - cur_end, cur_end, a))
        cur_end = max(cur_end, b)
    out.sort(key=lambda t: -t[0])
    return out


def _y_levels(glyphs: list[Glyph], tol: float = 2.5) -> list[float]:
    ys = sorted(g.y for g in glyphs)
    levels: list[float] = []
    for y in ys:
        if levels and abs(y - levels[-1]) <= tol:
            continue
        levels.append(y)
    return levels


def glyph_xy_cut(glyphs: list[Glyph], body_size: float, depth: int = 0) -> list[list[Glyph]]:
    """Recursive XY-cut over glyph boxes.

    Returns leaf regions (columns / paragraph runs) already in reading order:
    a vertical cut means "left column, then right column"; a horizontal cut
    means "above, then below". Working on glyph boxes instead of assembled
    lines is what keeps two columns from being interleaved word by word.

    Ranking matters: a page-wide vertical gutter is preferred over narrower
    whitespace seams, otherwise a short final paragraph in the left column can
    be split off below the right column and read out of order.
    """
    if len(glyphs) < 12 or depth > 16:
        return [glyphs]
    min_side = max(3, int(0.04 * len(glyphs)))

    xs = sorted((min(g.x0, g.x1), max(g.x0, g.x1)) for g in glyphs)
    vertical = _gaps(xs, max(6.0, 0.85 * body_size))
    if vertical:
        height = max(g.y for g in glyphs) - min(g.y for g in glyphs)
        best: tuple[float, list[Glyph], list[Glyph]] | None = None
        for _gap, ca, cb in vertical:
            cut = (ca + cb) / 2.0
            left = [g for g in glyphs if (g.x0 + g.x1) / 2.0 < cut]
            right = [g for g in glyphs if (g.x0 + g.x1) / 2.0 >= cut]
            if (len(left) < min_side or len(right) < min_side
                    or len(left) + len(right) != len(glyphs)):
                continue
            # A real column gutter is flanked by text over most of the region's
            # height; a seam between table cells is flanked only by those rows.
            near = [g for g in glyphs if ca - 14 <= g.x0 <= ca or cb <= g.x0 <= cb + 14]
            span = (max(g.y for g in near) - min(g.y for g in near)) if near else 0.0
            score = span / height if height > 0 else 0.0
            if best is None or score > best[0]:
                best = (score, left, right)
        if best:
            cut = (best[1][0].x0 + best[1][-1].x1) / 2.0
            if _DEBUG:
                print("  " * depth + f"V-cut n={len(glyphs)} -> L{len(best[1])}/R{len(best[2])} "
                      f"score={best[0]:.2f}", file=sys.stderr)
            return (glyph_xy_cut(best[1], body_size, depth + 1)
                    + glyph_xy_cut(best[2], body_size, depth + 1))

    ys = sorted((g.y - 0.22 * g.size, g.y + 0.78 * g.size) for g in glyphs)
    for _gap, ca, cb in _gaps(ys, 0.75 * body_size):
        cut = (ca + cb) / 2.0
        top = [g for g in glyphs if g.y >= cut]
        bottom = [g for g in glyphs if g.y < cut]
        if len(top) >= min_side and len(bottom) >= min_side and len(top) + len(bottom) == len(glyphs):
            if _DEBUG:
                print("  " * depth + f"H-cut n={len(glyphs)} y={cut:.0f} -> T{len(top)}/B{len(bottom)}",
                      file=sys.stderr)
            return (glyph_xy_cut(top, body_size, depth + 1)
                    + glyph_xy_cut(bottom, body_size, depth + 1))
    return [glyphs]


def _block_levels(b: Block) -> list[float]:
    return _y_levels([g for line in b.lines for g in line.glyphs])


def _looks_cellular(b: Block) -> bool:
    """Short, single-row lines are the shape of table cells, not prose."""
    if len(b.lines) < 2:
        return False
    return all(len(line.text) <= 30 for line in b.lines)


def reflow_tables(blocks: list[Block]) -> list[Block]:
    """Rejoin a table that the XY-cut split into its columns.

    Pieces are matched by their shared row baselines, rebuilt row by row
    (left to right inside a row), and then re-inserted after every block that
    sits above them, so a full-width table lands where a reader expects it
    instead of inside one of the two columns.
    """
    if len(blocks) < 2:
        return blocks
    used = [False] * len(blocks)
    created: list[tuple[Block, int]] = []  # (table block, insertion anchor bottom)

    for i, b in enumerate(blocks):
        if used[i] or not _looks_cellular(b):
            continue
        levels = _block_levels(b)
        if not (2 <= len(levels) <= 15 and len(b.lines) == len(levels)):
            continue
        parts = [i]
        for j, cand in enumerate(blocks):
            if j == i or used[j] or not _looks_cellular(cand):
                continue
            cand_levels = _block_levels(cand)
            if len(cand_levels) != len(levels):
                continue
            if not all(abs(x - y) <= 2.5 for x, y in zip(cand_levels, levels)):
                continue
            parts.append(j)
        if len(parts) < 2:
            continue
        parts.sort(key=lambda k: blocks[k].x0)
        rows: list[Line] = []
        for level in sorted(levels, reverse=True):  # top row first
            glyphs: list[Glyph] = []
            for p in parts:
                for line in blocks[p].lines:
                    if abs(line.y - level) <= 2.5:
                        glyphs.extend(line.glyphs)
            if not glyphs:
                continue
            glyphs.sort(key=lambda g: g.x0)
            sizes = sorted(g.size for g in glyphs)
            rows.append(Line(glyphs[0].page, glyphs[0].y,
                             min(g.x0 for g in glyphs), max(g.x1 for g in glyphs),
                             sizes[len(sizes) // 2], glyphs))
        if len(rows) < 2:
            continue
        top = max(r.y for r in rows)
        bottom = min(r.y for r in rows)
        anchor = bottom
        table = Block(rows[0].page, min(r.x0 for r in rows), bottom,
                      max(r.x1 for r in rows), top, rows, kind="table")
        for p in parts:
            used[p] = True
        created.append((table, anchor))

    if not created:
        return blocks

    remaining = [b for i, b in enumerate(blocks) if not used[i]]
    for table, _anchor in sorted(created, key=lambda t: -t[0].y1):
        index = 0
        for k, b in enumerate(remaining):
            if b.y0 >= table.y1 - 2.0:  # this block sits above the table
                index = k + 1
        remaining.insert(index, table)
    return remaining


def lines_to_blocks(lines: list[Line]) -> list[Block]:
    """Merge consecutive lines into paragraph blocks before cutting."""
    blocks: list[Block] = []
    for ln in lines:
        if blocks:
            prev = blocks[-1]
            last = prev.lines[-1]
            aligned = abs(ln.x0 - last.x0) < 4.0 or (
                0.0 <= ln.x0 - last.x0 <= 22.0 and abs(ln.x1 - last.x1) < 14.0
            ) or (abs(ln.x0 - last.x0) < 45.0 and abs(ln.x1 - last.x1) < 45.0)
            gap = last.y - ln.y
            body = max(ln.size, last.size)
            same_size = abs(ln.size - last.size) < 0.9
            same_weight = ln.bold == last.bold
            heading_like = bool(_SECTION_RE.match(last.text.strip())) and len(last.text) < 70
            if aligned and same_size and same_weight and not heading_like and 0 < gap < 1.6 * body:
                prev.lines.append(ln)
                prev.y0 = ln.y
                prev.x0 = min(prev.x0, ln.x0)
                prev.x1 = max(prev.x1, ln.x1)
                continue
        blocks.append(Block(ln.page, ln.x0, ln.y, ln.x1, ln.y, [ln]))
    return blocks


# --------------------------------------------------------------------------
# 7. Structure classification
# --------------------------------------------------------------------------

_SECTION_RE = re.compile(
    r"^(?:(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})\.?|(?P<roman>[IVX]{1,5})\.)\s+(?P<title>[A-Z][^.]{1,80})$"
)
_CAPTION_RE = re.compile(r"^(?:Figure|Fig\.|Table|Algorithm|Listing)\s*(\d+[A-Za-z]?)\s*[.:]?\s+", re.I)
_EQUATION_LABEL_RE = re.compile(r"\((\d{1,3}[a-z]?)\)\s*$")


def classify_blocks(blocks: list[Block], body_size: float,
                    header_texts: set[str], footer_texts: set[str],
                    page_height: float, page_width: float) -> None:
    for b in blocks:
        if b.kind == "table":
            continue
        text = b.text.strip()
        first = b.lines[0].text.strip()
        norm = re.sub(r"\d+", "#", text.strip().lower())
        norm = re.sub(r"\s+", " ", norm)[:80]
        if norm in header_texts and b.y1 > page_height * 0.92:
            b.kind = "header"
            continue
        if norm in footer_texts and b.y0 < page_height * 0.08:
            b.kind = "footer"
            continue
        if re.fullmatch(r"(?:page\s*)?\d{1,4}", text.strip(), re.I) and (
                b.y0 < page_height * 0.08 or b.y1 > page_height * 0.95):
            b.kind = "footer" if b.y0 < page_height * 0.5 else "header"
            continue
        size = b.size
        bold = any(("bold" in (g.font or "").lower() or "black" in (g.font or "").lower())
                   for l in b.lines for g in l.glyphs[:3])
        if _CAPTION_RE.match(first):
            b.kind = "caption"
            continue
        if "@" in text and len(text) <= 500 and re.search(r"[∗†‡§¶]", text):
            b.kind = "authors"
            continue
        if size >= body_size * 1.35 and len(text) <= 300:
            b.kind = "title"
            continue
        if _SECTION_RE.match(first) and len(first) <= 120:
            b.kind = "heading"
            continue
        if size >= body_size * 1.08 and len(first) <= 120:
            b.kind = "heading"
            continue
        if bold and len(first) <= 90 and not first.endswith((".", ",", ";")):
            b.kind = "heading"
            continue
        if _EQUATION_LABEL_RE.search(text) and len(text) <= 200 and not re.search(r"[a-z]{4,}\s+[a-z]{4,}", text):
            b.kind = "equation"
            continue
        if text:
            b.kind = "paragraph"


def heading_level(text: str, size: float, body_size: float) -> int:
    m = _SECTION_RE.match(text.strip())
    if m and m.group("num"):
        return min(m.group("num").count(".") + 1, 4)
    if size >= body_size * 1.35:
        return 1
    if size >= body_size * 1.18:
        return 2
    return 3


# --------------------------------------------------------------------------
# 8. Tables (whitespace-column fallback)
# --------------------------------------------------------------------------

def detect_tables(blocks: list[Block]) -> list[dict]:
    tables = []
    for b in blocks:
        if b.kind == "table":
            table = _table_from_lines([b])
            if table:
                tables.append(table)
    i = 0
    while i < len(blocks):
        b = blocks[i]
        candidate = [b]
        j = i + 1
        while j < len(blocks) and blocks[j].kind == "paragraph" and \
                abs(blocks[j].lines[0].size - b.lines[0].size) < 0.6 and \
                blocks[j].y1 <= candidate[-1].y0 + 1.5 * b.size:
            candidate.append(blocks[j])
            j += 1
        if len(candidate) >= 2:
            table = _table_from_lines(candidate)
            if table:
                tables.append(table)
                i = j
                continue
        i += 1
    return tables


def _cells_for_line(line: Line, min_gap_em: float = 0.9) -> list[tuple[float, float, str]]:
    cells = []
    cur: list[Glyph] = [line.glyphs[0]]
    for g in line.glyphs[1:]:
        gap = g.x0 - cur[-1].x1
        if gap > min_gap_em * max(g.size, 1.0):
            cells.append((cur[0].x0, cur[-1].x1, "".join(x.text for x in cur).strip()))
            cur = [g]
        else:
            cur.append(g)
    cells.append((cur[0].x0, cur[-1].x1, "".join(x.text for x in cur).strip()))
    return [c for c in cells if c[2]]


def _table_from_lines(block_list: list[Block]) -> dict | None:
    rows = []
    bounds: list[list[float]] = []
    for b in block_list:
        for line in b.lines:
            cells = _cells_for_line(line)
            if len(cells) >= 2:
                rows.append(cells)
                bounds.append([round(c[0], 1) for c in cells])
    if len(rows) < 3:
        return None
    col_count = max(len(r) for r in rows)
    if col_count < 2:
        return None
    aligned = sum(1 for r in rows if len(r) == col_count)
    if aligned < max(3, int(0.7 * len(rows))):
        return None
    # Column starts must line up across rows, otherwise this is prose, not a table.
    consistent = 0
    for col in range(col_count):
        xs = [b[col] for b in bounds if len(b) == col_count]
        if not xs:
            continue
        spread = max(xs) - min(xs)
        if spread <= 12.0:
            consistent += 1
    if consistent < max(2, int(0.7 * col_count)):
        return None
    numeric = sum(1 for r in rows for c in r if re.search(r"\d", c[2]))
    if numeric < 2:
        return None
    cells_all = [c[2] for r in rows for c in r]
    if not cells_all or sum(len(c) for c in cells_all) / len(cells_all) > 60:
        return None
    first = block_list[0]
    return {
        "page": first.page + 1,
        "bbox": [round(first.x0, 2), round(first.y0, 2), round(first.x1, 2), round(first.y1, 2)],
        "n_rows": len(rows),
        "n_cols": col_count,
        "rows": [[c[2] for c in r] for r in rows],
        "method": "whitespace_column",
        "confidence": round(min(aligned, consistent) / len(rows), 3),
    }


# --------------------------------------------------------------------------
# 9. Document assembly
# --------------------------------------------------------------------------

def parse_pdf(path: str, max_pages: int | None = None, want_images: bool = True) -> dict:
    t_start = time.time()
    with open(path, "rb") as fh:
        data = fh.read()
    doc = PDFDocument(data)
    meta = doc.metadata()
    total = doc.page_count()
    if max_pages:
        total = min(total, max_pages)

    per_page_glyphs: list[list[Glyph]] = []
    per_page_images: list[list[ImageRef]] = []
    page_sizes: list[tuple[float, float]] = []
    stats_all = {"rotated_runs": 0, "unsupported_filters": 0}
    font_cache: dict = {}

    for idx in range(total):
        page = doc.pages[idx]
        resources = doc.page_resources(page)
        glyphs, images, stats = extract_glyphs(doc, idx, page, resources, font_cache)
        per_page_glyphs.append(glyphs)
        per_page_images.append(images)
        page_sizes.append(doc.page_size(page))
        for k in ("rotated_runs", "unsupported_filters"):
            stats_all[k] += stats.get(k, 0)
        stats_all.setdefault("rotated_texts", []).extend(stats.get("rotated_texts", []))

    # body size = most common glyph size across the document
    size_hist: dict[float, int] = {}
    for glyphs in per_page_glyphs:
        for g in glyphs:
            key = round(g.size, 1)
            size_hist[key] = size_hist.get(key, 0) + len(g.text)
    body_size = max(size_hist.items(), key=lambda kv: kv[1])[0] if size_hist else 10.0

    # Reading order: glyph boxes -> XY-cut regions -> lines -> blocks.
    # Running headers and footers are removed first: a centred page number sits
    # exactly in the column gutter and would otherwise fake a column boundary.
    band_glyphs: list[list[Glyph]] = []
    body_glyphs: list[list[Glyph]] = []
    for idx in range(total):
        height = page_sizes[idx][1]
        band, body = [], []
        for g in per_page_glyphs[idx]:
            if g.y > height * 0.90 or g.y < height * 0.08:
                band.append(g)
            else:
                body.append(g)
        band_glyphs.append(band)
        body_glyphs.append(body)

    def norm_text(text: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"\d+", "#", text.strip().lower()))[:80]

    counts: dict[str, int] = {}
    for band in band_glyphs:
        for line in build_lines(band):
            key = norm_text(line.text)
            if key:
                counts[key] = counts.get(key, 0) + 1
    threshold = max(2, int(0.35 * max(total, 1)))
    repeated = {key for key, hits in counts.items() if hits >= threshold}

    def is_furniture(text: str) -> bool:
        key = norm_text(text)
        if not key:
            return False
        if re.fullmatch(r"#+", key):  # bare page number
            return True
        return key in repeated

    page_blocks: list[list[Block]] = []
    furniture: list[list[dict]] = []
    for idx in range(total):
        removed: list[dict] = []
        keep = list(body_glyphs[idx])
        for line in build_lines(band_glyphs[idx]):
            if is_furniture(line.text):
                removed.append({"text": line.text, "kind": "header_or_footer",
                                "bbox": [round(line.x0, 1), round(line.y, 1),
                                         round(line.x1, 1), round(line.y, 1)]})
            else:
                keep.extend(line.glyphs)
        blocks: list[Block] = []
        for region in glyph_xy_cut(keep, body_size):
            blocks.extend(lines_to_blocks(build_lines(region)))
        page_blocks.append(reflow_tables(blocks))
        furniture.append(removed)

    header_texts: set[str] = set()
    footer_texts: set[str] = set()

    pages_out = []
    all_blocks: list[Block] = []
    for idx in range(total):
        glyphs = per_page_glyphs[idx]
        width, height = page_sizes[idx]
        blocks = page_blocks[idx]
        classify_blocks(blocks, body_size, header_texts, footer_texts, height, width)
        page_obj = {
            "page": idx + 1,
            "width": round(width, 1),
            "height": round(height, 1),
            "char_count": sum(len(g.text) for g in glyphs),
            "has_text_layer": bool(glyphs),
            "blocks": [],
            "removed_headers_footers": list(furniture[idx]),
            "images": [
                {"name": im.name, "width": im.width, "height": im.height, "bbox": _bbox_image(im)}
                for im in per_page_images[idx]
            ] if want_images else [],
        }
        for b in blocks:
            if b.kind in ("header", "footer"):
                page_obj["removed_headers_footers"].append(
                    {"text": b.text, "kind": b.kind, "bbox": _bbox(b)})
                continue
            page_obj["blocks"].append({
                "type": b.kind,
                "text": b.text,
                "bbox": _bbox(b),
                "font_size": round(b.size, 1),
                "line_count": len(b.lines),
            })
            all_blocks.append(b)
        pages_out.append(page_obj)

    # The title is the largest-font block on the opening pages.
    if pages_out:
        for page_obj in pages_out[:2]:
            cands = [b for b in page_obj["blocks"]
                     if b["type"] not in ("header", "footer", "caption", "authors")]
            if cands:
                best = max(cands[:5], key=lambda b: b["font_size"])
                if best["font_size"] >= body_size * 1.2 and len(best["text"]) <= 300:
                    best["type"] = "title"
                    break

    # Front matter between the title and the abstract is the author block, not
    # section headings -- a positional rule is more reliable than font names,
    # which LaTeX papers often name without any "bold" token.
    if pages_out:
        p1 = pages_out[0]
        blocks = p1["blocks"]
        title_at = next((i for i, b in enumerate(blocks) if b["type"] == "title"), None)
        if title_at is not None:
            for b in blocks[title_at + 1:]:
                if re.match(r"^abstract\b", b["text"].strip(), re.I):
                    break
                if b["type"] not in ("caption", "footer", "header"):
                    b["type"] = "authors"

    sections = []
    for page_obj in pages_out:
        for block in page_obj["blocks"]:
            if block["type"] in ("heading", "title"):
                if block["type"] == "title":
                    continue
                level = heading_level(block["text"], block["font_size"], body_size)
                sections.append({
                    "heading": block["text"],
                    "level": level,
                    "page": page_obj["page"],
                    "bbox": block["bbox"],
                })
    # drop title-as-section duplicate
    if sections and sections[0]["level"] == 1 and len(sections) > 1 and sections[1]["level"] == 1:
        pass

    figures = []
    for page_obj in pages_out:
        for caption in [b for b in page_obj["blocks"] if b["type"] == "caption"
                        and re.match(r"(?:Figure|Fig\.)", b["text"], re.I)]:
            figures.append({
                "page": page_obj["page"],
                "caption": caption["text"][:300],
                "bbox": caption["bbox"],
                "has_image_on_page": bool(page_obj["images"]),
                "kind": "image" if page_obj["images"] else "vector_or_unknown",
            })

    tables = detect_tables(all_blocks)
    captions = [b["text"] for p in pages_out for b in p["blocks"] if b["type"] == "caption"]
    for t in tables:
        near = [c for c in captions if abs(len(c)) > 0 and re.match(r"Table", c, re.I)]
        t["caption_guess"] = near[len(tables) - 1] if len(near) >= len(tables) else None

    equations = []
    for page_obj in pages_out:
        for block in page_obj["blocks"]:
            m = _EQUATION_LABEL_RE.search(block["text"])
            if block["type"] == "equation" or (m and re.search(r"[=+\-^_/]", block["text"])):
                equations.append({
                    "label": m.group(1) if m else None,
                    "page": page_obj["page"],
                    "text": block["text"],
                    "bbox": block["bbox"],
                })

    references = _extract_references(pages_out)

    unknown = sum(1 for glyphs in per_page_glyphs for g in glyphs if REPLACEMENT in g.text)
    total_chars = sum(len(g.text) for glyphs in per_page_glyphs for g in glyphs) or 1
    scanned_pages = [p["page"] for p in pages_out if not p["has_text_layer"]]
    checks = {
        "unknown_glyph_count": unknown,
        "unknown_glyph_ratio": round(unknown / total_chars, 5),
        "pages_total": len(pages_out),
        "pages_without_text_layer": scanned_pages,
        "header_footer_removed": sum(len(p["removed_headers_footers"]) for p in pages_out),
        "rotated_text_runs": stats_all["rotated_runs"],
        "repairs": doc.repairs,
        "body_font_size": body_size,
    }

    status = "success"
    if not pages_out:
        status = "failed"
    elif scanned_pages:
        status = "partial"
    elif checks["unknown_glyph_ratio"] > 0.02:
        status = "partial"

    result = {
        "case_id": None,
        "status": status,
        "input": {
            "path": os.path.abspath(path),
            "filename": os.path.basename(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "pages": len(pages_out),
        },
        "provenance": {
            "engine": "pdfparse",
            "version": VERSION,
            "pdf_header": data[:8].decode("latin-1", "replace"),
            "info": meta,
            "runtime_ms": int((time.time() - t_start) * 1000),
            "dependencies": ["python-stdlib"],
        },
        "document": {
            "title": _guess_title(pages_out),
            "sections": sections,
            "figures": figures,
            "tables": tables,
            "equations": equations,
            "references": references,
            "pages": pages_out,
        },
        "checks": checks,
        "uncertainty": _uncertainty(checks, sections, tables, figures),
    }
    if scanned_pages:
        result["document"]["ocr_required_pages"] = scanned_pages
    return result


def _bbox(b: Block) -> list[float]:
    return [round(b.x0, 1), round(b.y0, 1), round(b.x1, 1), round(b.y1, 1)]


def _bbox_image(im: ImageRef) -> list[float]:
    return [round(im.x0, 1), round(im.y0, 1), round(im.x1, 1), round(im.y1, 1)]


def _guess_title(pages: list[dict]) -> str | None:
    for page in pages[:2]:
        for block in page["blocks"]:
            if block["type"] == "title":
                return block["text"]
    if pages and pages[0]["blocks"]:
        first = pages[0]["blocks"][0]
        if len(first["text"]) <= 200:
            return first["text"]
    return None


def _extract_references(pages: list[dict]) -> list[dict]:
    refs = []
    in_refs = False
    buf: list[tuple[int, list[float], str]] = []
    for page in pages:
        for block in page["blocks"]:
            text = block["text"].strip()
            if re.match(r"^(?:References|REFERENCES|Bibliography)\s*$", text):
                in_refs = True
                continue
            if in_refs:
                if re.match(r"^(?:Appendix|APPENDIX)\b", text):
                    in_refs = False
                    continue
                buf.append((page["page"], block["bbox"], text))
    if not buf:
        return []
    entries = []
    for page_no, bbox, text in buf:
        parts = re.split(r"(?<=[.\]])\s+(?=[A-Z][a-z]+,\s)", text)
        for part in parts:
            part = part.strip()
            if len(part) > 20:
                entries.append({"page": page_no, "bbox": bbox, "raw": part[:400]})
    return entries[:200]


def _uncertainty(checks: dict, sections: list, tables: list, figures: list) -> list[str]:
    notes = []
    if checks["pages_without_text_layer"]:
        notes.append(
            "Pages %s carry no text layer; their content is NOT extracted and needs OCR "
            "(status partial)." % checks["pages_without_text_layer"])
    if checks["unknown_glyph_ratio"] > 0.005:
        notes.append(
            "%.2f%% of glyphs could not be mapped to Unicode (font subset without ToUnicode); "
            "those characters appear as U+FFFD." % (checks["unknown_glyph_ratio"] * 100))
    if not sections:
        notes.append("No section headings were detected; section structure is unavailable.")
    if not tables:
        notes.append("No tables were detected by the whitespace-column heuristic.")
    if not figures:
        notes.append("No figure captions were matched; figure inventory may be incomplete.")
    if checks["rotated_text_runs"]:
        notes.append("%d rotated text runs were excluded from reading order."
                     % checks["rotated_text_runs"])
    if checks["repairs"]:
        notes.append("Structural repairs applied: %s" % "; ".join(sorted(set(checks["repairs"]))))
    return notes


# --------------------------------------------------------------------------
# 10. CLI
# --------------------------------------------------------------------------

def find_pdf_in_case(obj, depth: int = 0) -> str | None:
    """Locate the PDF path inside a case.json without assuming its key name."""
    if depth > 6:
        return None
    if isinstance(obj, str):
        if obj.lower().endswith(".pdf"):
            return obj
        return None
    if isinstance(obj, dict):
        # prefer obvious keys first
        for key in ("pdf", "pdf_path", "input_path", "path", "file", "document", "material"):
            if key in obj:
                found = find_pdf_in_case(obj[key], depth + 1)
                if found:
                    return found
        for value in obj.values():
            found = find_pdf_in_case(value, depth + 1)
            if found:
                return found
        return None
    if isinstance(obj, list):
        for item in obj:
            found = find_pdf_in_case(item, depth + 1)
            if found:
                return found
    return None


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Dependency-free PDF structure extractor")
    ap.add_argument("input", nargs="?", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--stdout", action="store_true")
    ap.add_argument("--max-pages", type=int, default=None)
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--case-json", default=None)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)

    case = None
    if args.case_json and os.path.exists(args.case_json):
        with open(args.case_json, "r", encoding="utf-8") as fh:
            case = json.load(fh)
    if not args.input:
        if case is not None:
            args.input = find_pdf_in_case(case)
        if not args.input:
            print("error: no PDF path given and none found in --case-json", file=sys.stderr)
            return 2
    if not os.path.exists(args.input):
        print("error: input not found: %s" % args.input, file=sys.stderr)
        return 2

    result = parse_pdf(args.input, max_pages=args.max_pages, want_images=not args.no_images)

    if case is not None:
        result["case_id"] = case.get("case_id") or case.get("id")
        contract = case.get("output_contract") or case.get("outputs") or {}
        if isinstance(contract, dict):
            filled = {}
            for key in contract:
                if key in result:
                    filled[key] = result[key]
                elif key in result["document"]:
                    filled[key] = result["document"][key]
                else:
                    filled[key] = None
            result["output_contract_fields"] = {"declared": list(contract), "filled": filled}

    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
    if args.stdout or not args.out:
        sys.stdout.reconfigure(encoding="utf-8")
        print(payload)
    if args.debug:
        print(json.dumps(result["checks"], ensure_ascii=False), file=sys.stderr)
    return 0 if result["status"] != "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
