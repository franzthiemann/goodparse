"""Optional TrueType/OTF font embedding for text boxes.

GoodNotes text boxes reference fonts by name (e.g. ``Futura``) that are not part
of PDF's base-14 set, so the default emitter falls back to ``/Helvetica``.  When
:mod:`fontTools` is installed *and* a suitable font is present on the system,
this module subsets the font to the characters actually used and returns the PDF
objects needed to embed it as a **CID** composite font (``Type0`` +
``CIDFontType0C`` + ``/FontFile3`` carrying the raw CFF).  Hex-string text codes
(``<0001 0002 …>``) reference glyphs by 2-byte ID.

Every function here raises/returns a graceful fallback (``None``) on any failure,
so the caller always has a working base-14 alternative.  This module is
imported lazily by :mod:`goodparse.pdf`; importing it requires fontTools, so the
core library still works with zero third-party packages.
"""
from __future__ import annotations

import io
import re
import subprocess
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    from fontTools import subset as _ftsubset
    from fontTools.ttLib import TTFont as _TTFont
except Exception:  # pragma: no cover - optional dependency
    _TTFont = None
    _ftsubset = None

# Stored GoodNotes family -> candidate installed families, in priority order.
# The real Apple fonts are not (and can't be) redistributed, so we match well
# known metric/shape-compatible replacements that ship in common packages.
_FAMILY_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "Futura": ("URW Gothic", "Montserrat", "Century Gothic", "Futura"),
    "Futura PT": ("URW Gothic", "Montserrat", "Century Gothic"),
    "Avenir Next": ("Montserrat", "URW Gothic"),
    "Avenir": ("Montserrat", "URW Gothic"),
    "Helvetica Neue": ("Nimbus Sans", "Helvetica Neue", "Helvetica"),
    "Helvetica": ("Nimbus Sans", "Helvetica"),
}

# A subsetted CFF larger than this is dropped (fall back to base-14) to keep
# output files small.
_MAX_EMBED_CFF = 300 * 1024


def fonttools_available() -> bool:
    """True when fontTools is importable (the only extra needed to embed)."""
    return _TTFont is not None and _ftsubset is not None


def candidates_for(family: Optional[str]) -> Tuple[str, ...]:
    """Installed-family candidates for a stored GoodNotes family (else (,))."""
    return _FAMILY_CANDIDATES.get((family or "").strip(), ())


def _fc_match(query: str) -> Optional[str]:
    """Resolve ``query`` (family[:style]) to a font file path via fontconfig."""
    try:
        out = subprocess.run(
            ["fc-match", "-f", "%{file}", query],
            check=True, capture_output=True, timeout=10,
        ).stdout.decode("ascii", "replace").strip()
    except Exception:
        return None
    return out or None


def find_font_file(family: str, bold: bool, italic: bool) -> Optional[str]:
    """Find an installed font file for ``family`` at the requested style.

    Tries each candidate family (via ``fc-match`` with the style modifier) and
    returns the first that resolves to a real font file.  ``None`` when none is
    installed (caller then uses the base-14 fallback).
    """
    style = ":bold" if bold else (":italic" if italic else "")
    for cand in candidates_for(family):
        path = _fc_match(cand + style)
        if path and re.search(r"\.(ttf|otf|ttc)$", path, re.I):
            return path
    return None


@dataclass
class EmbeddedFont:
    """A subset font ready to embed.

    Carries the raw pieces only; :func:`build_font_objects` turns them into the
    five PDF objects once the caller has assigned real object numbers.
    """
    base: str
    cff: bytes                      # raw CFF bytes for /FontFile3
    char_codes: Dict[str, bytes]    # char -> 2-byte glyph code
    widths: Dict[str, int]          # char -> advance in thousandths of an em
    ascent: int                     # em-per-mille
    descent: int                    # em-per-mille (negative)
    flag: int                       # font-descriptor flag bits

    def code(self, text: str) -> bytes:
        """Concatenated 2-byte codes for ``text`` (unmapped chars -> 0)."""
        out = bytearray()
        for ch in text:
            out += self.char_codes.get(ch, b"\x00\x00")
        return bytes(out)

    def width(self, text: str) -> int:
        """Total advance (thousandths of an em) for ``text``."""
        return sum(self.widths.get(ch, 278) for ch in text)


def build_font_objects(base_num: int, ef: EmbeddedFont) -> Dict[int, bytes]:
    """Build the five PDF objects for ``ef`` starting at ``base_num``.

    Returns ``{num: body}`` where ``base_num`` is the Type0 font (referenced by
    name), followed by its CID descendant, font descriptor, ``/FontFile3`` CFF,
    and the ToUnicode CMap.
    """
    t0, t_cid, t_fd, t_ff, t_u = base_num, base_num + 1, base_num + 2, \
        base_num + 3, base_num + 4
    w_entries = " ".join(
        "%d %d %d" % (int.from_bytes(ef.char_codes[ch], "big"),
                      int.from_bytes(ef.char_codes[ch], "big"), ef.widths[ch])
        for ch in sorted(ef.char_codes, key=lambda c: int.from_bytes(ef.char_codes[c], "big")))
    touni = "\n".join([
        "/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
        "1 beginbfchar",
        *[ "%04X <%04X>" % (int.from_bytes(ef.char_codes[ch], "big"), ord(ch))
           for ch in sorted(ef.char_codes) ],
        "endbfchar endcmap", "end", "end"]).encode("ascii", "replace")
    return {
        t0: ("<< /Type /Font /Subtype /Type0 /BaseFont /" + ef.base +
             " /Encoding /Identity-H /DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>"
             % (t_cid, t_u)).encode(),
        t_cid: ("<< /Type /Font /Subtype /CIDFontType0C /BaseFont /" + ef.base +
                " /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) "
                "/Supplement 0 >> /FontDescriptor %d 0 R /CIDToGIDMap /Identity "
                "/DW 1000 /W [%s] >>" % (t_fd, w_entries)).encode(),
        t_fd: ("<< /Type /FontDescriptor /FontName /" + ef.base +
               " /Flags %d /FontBBox [-200 -300 1200 1200] /Ascent %d /Descent %d "
               "/ItalicAngle 0 /StemV 90 /FontFile3 %d 0 R >>"
               % (ef.flag, ef.ascent, ef.descent, t_ff)).encode(),
        t_ff: ("<< /Length %d /Length1 %d >>\nstream\n" % (len(ef.cff), len(ef.cff)))
              .encode() + ef.cff + b"\nendstream",
        t_u: ("<< /Length %d >>\nstream\n" % len(touni)).encode() + touni + b"\nendstream",
    }


def subset_and_embed(path: str, text: str, base: str) -> Optional[EmbeddedFont]:
    """Build an embedded CID font from ``path`` for the characters in ``text``.

    Returns an :class:`EmbeddedFont` or ``None`` (on any failure) so the caller
    can fall back to base-14.
    """
    if _TTFont is None or _ftsubset is None:
        return None
    TTFont = _TTFont
    ftsubset = _ftsubset
    unicodes = sorted({ord(c) for c in text if ord(c) > 0x20})
    if not unicodes:
        return None
    try:
        font = TTFont(path)
        if "CFF " not in font:
            return None
        upm = font["head"].unitsPerEm
        hhea = font["hhea"]
        hmtx = font["hmtx"]
        cmap = font.getBestCmap()
        opts = ftsubset.Options()
        opts.glyph_names = False
        opts.notdef_outline = True
        opts.desubroutinize = True
        sub = ftsubset.Subsetter(options=opts)
        sub.populate(unicodes=unicodes)
        sub.subset(font)
        font.flavor = None
        buf = io.BytesIO()
        font.save(buf)
        buf.seek(0)
        reloaded = TTFont(buf)
        cff = reloaded.reader["CFF "]
        if len(cff) > _MAX_EMBED_CFF:
            return None
        cmap2 = reloaded.getBestCmap()
        hmtx2 = reloaded["hmtx"]
        gorder = reloaded.getGlyphOrder()
        gid = {name: i for i, name in enumerate(gorder)}
        if not cmap2:
            return None
        char_codes: Dict[str, bytes] = {}
        widths: Dict[str, int] = {}
        w_entries: List[str] = []
        for ch in sorted(set(text)):
            if ord(ch) <= 0x20:
                continue
            name = cmap2.get(ord(ch))
            if not name:
                continue
            g = gid[name]
            adv = hmtx2.metrics[name][0]
            w = int(round(adv * 1000 / upm))
            char_codes[ch] = g.to_bytes(2, "big")
            widths[ch] = w
            w_entries.append("%d %d %d" % (g, g, w))
        if not char_codes:
            return None
    except Exception:
        return None

    safe = base.replace(" ", "")[:60]
    safe = safe.replace("-", "").replace("_", "") or "GoodParseFont"
    ascent = int(round(hhea.ascent * 1000 / upm))
    descent = int(round(hhea.descent * 1000 / upm))
    flag = 0b01  # symbol (no built-in metrics to trust)
    return EmbeddedFont(base=safe, cff=cff, char_codes=char_codes,
                        widths=widths, ascent=ascent, descent=descent, flag=flag)
