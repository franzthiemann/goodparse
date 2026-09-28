"""Parse GoodNotes ``.goodnotes`` archives into a simple drawing model.

Format (reverse-engineered; see README for the full write-up):

* A ``.goodnotes`` file is a ZIP archive. Each ``notes/<UUID>`` member is one
  page, stored as length-delimited protobuf records. Records alternate between
  a small metadata record and a content record whose field ``#7`` carries a
  pen or highlighter stroke.
* In a stroke's ``#7`` message: field ``#2`` is the Apple-LZ4 geometry blob and
  field ``#4`` is the RGBA colour (float32 sub-fields ``1=R 2=G 3=B 4=A``).
* Pen geometry (field ``#21 == 24``) holds float32 ``(x, y, pressure)`` points
  at stride 12.  Highlighter / eraser geometry (``#21 == 25``) is a smooth
  curve: a style template, a long run of 0/1 flags, then ``[x0][y0][u32
  count][count * (x, y)]`` float32 pairs.

Coordinate space.  Strokes, images and text boxes are stored in the document's
*canvas* space.  The exported page is a viewport of that canvas.  The
canvas-to-page mapping is a uniform scale ``page_size / canvas_size`` about
the origin (0, 0).  The scale is recovered from ``index.events.pb`` (which
stores the canvas dimensions as float32) and the paper PDF's MediaBox (which
stores the page size).  When either is absent the scale falls back to 1.0.

Images.  A page's embedded raster lives in a top-level record whose field
``#4`` is the attachment UUID; the matching ``attachments/<UUID>`` zip member
holds the PNG/JPEG bytes.  A companion record carries a placement matrix in
field ``#1``: two nested messages (``f2`` and ``f3``), each holding two
``(x, y)`` float32 points.  ``f2.f1`` is the top-left corner (canvas space);
``f2.f2`` is the (width, height) size vector.

Text boxes.  Stored as a top-level record whose field ``#8`` is a nested
message: ``f2`` = the placement matrix (top-left + size), one field is the
RTF source (Cocoa RTF), and another is the RGBA colour.
"""

from __future__ import annotations

import os
import re
import struct
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .applelz4 import apple_decompress, is_apple_lz4
from .protobuf import (
    iter_fields,
    parse_message,
    read_length_delimited_records,
)
from .text_rtf import TextLine, parse_rtf_runs

# A4 in PDF points (72 dpi) — fallback for documents without an explicit page
# size (samples 1-4 are A4).
A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89

# Points live after the 8-byte "tpl\0"+length header and the 40-byte constant
# style template; restricting the search to offset >= 64 avoids decoding the
# template (which spuriously looks like a point near (10.6, 10.6)).
_POINT_SEARCH_START = 64
_POINT_STRIDE = 12
_COORD_MIN = 1.0
_COORD_MAX = 10000.0   # generous page bound in points
_WIDTH_MAX = 200.0     # sanity cap on the per-point width float

# Highlighter / eraser curve geometry: the longest run of 0/1 flag bytes is
# the boolean style array; the curve follows it immediately.
_HL_FLAG_RUN_MIN = 20

# Highlighter / eraser strokes have no per-point width; use a default.
_HL_DEFAULT_WIDTH_PT = 10.0

# Widthless marker/pen strokes (flag-run and stride-8 encodings store no
# per-point width).  The reference draws these as thick marker swipes (~20 pt
# page = ~36 canvas); the default is a mid-weight canvas value that keeps the
# thin widthless black marks from bloating while making the colored markers
# read as proper bands.
_PEN_DEFAULT_WIDTH_PT = 20.0

# Marker strokes store a *uniform* width as a single float32 at byte 40 of the
# decompressed geometry blob (after the "tpl\0"+length+style-template header).
# A pen stroke has no such header (its bytes 40-43 are part of the template and
# read as a huge, implausible float), so a sane range identifies a marker.
_MARKER_WIDTH_OFFSET = 40
_MARKER_WIDTH_MIN = 0.5
_MARKER_WIDTH_MAX = 500.0


@dataclass
class Stroke:
    """One pen, highlighter, or eraser stroke.

    ``points`` is a list of ``(x, y, width)`` triples in canvas space.
    ``kind`` is ``"pen"``, ``"highlighter"`` or ``"eraser"``.
    ``color`` is RGBA (0..1).  Eraser strokes are always white.
    """

    points: List[Tuple[float, float, float]]  # (x, y, width)
    color: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    kind: str = "pen"

    @property
    def is_dot(self) -> bool:
        return len(self.points) <= 1 or _bbox_diagonal(self.points) < 1.0


@dataclass
class Image:
    """One embedded raster placed on the page.

    ``position`` is the top-left corner in canvas space; ``size`` is
    ``(width, height)`` in canvas space.  ``data`` is the raw raster bytes.
    """

    position: Tuple[float, float]
    size: Tuple[float, float]
    data: bytes
    fmt: str = "png"

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        x, y = self.position
        w, h = self.size
        return x, y, x + w, y + h


@dataclass
class TextBox:
    """One text box with RTF content.

    ``lines`` holds the styled run breakdown (per-line, per-run bold/italic/
    underline/strike/colour) parsed from the RTF; exporters use it to render
    formatting, while ``plain``/``rtf`` are kept for simple consumers.
    """

    position: Tuple[float, float]  # top-left, canvas space
    size: Tuple[float, float]      # (w, h), canvas space
    rtf: str
    plain: str
    color: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    lines: List["TextLine"] = field(default_factory=list)


@dataclass
class Page:
    strokes: List[Stroke] = field(default_factory=list)
    images: List[Image] = field(default_factory=list)
    texts: List[TextBox] = field(default_factory=list)
    width: float = A4_WIDTH_PT
    height: float = A4_HEIGHT_PT
    scale: float = 1.0  # canvas → page uniform scale
    # Raw bytes of this page's background ("paper") PDF, or None.  Background
    # PDFs are stored as attachments and define the page size.
    background: Optional[bytes] = None


@dataclass
class GoodNotesDocument:
    pages: List[Page] = field(default_factory=list)
    title: str = "GoodNotes"
    canvas: Optional[Tuple[float, float]] = None  # (w, h) in points


# --------------------------------------------------------------------------- #
# Geometry decoding
# --------------------------------------------------------------------------- #

def _f32(buf: bytes, o: int) -> float:
    return struct.unpack_from("<f", buf, o)[0]


def _bbox_diagonal(points) -> float:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5


def _valid_point(buf: bytes, o: int) -> bool:
    """A pen point is float32 ``(x, y, width)``; gate on coordinates only."""
    if o + _POINT_STRIDE > len(buf):
        return False
    x, y, w = _f32(buf, o), _f32(buf, o + 4), _f32(buf, o + 8)
    return (_COORD_MIN < x < _COORD_MAX and _COORD_MIN < y < _COORD_MAX
            and 0.0 <= w < _WIDTH_MAX)


def _flag_run_end(buf: bytes) -> int:
    """Return the offset just past the *first* long run of 0x00/0x01 bytes.

    The flag-run pen encoding stores ``template + flag-run + [x0][y0][u32
    count] + count*(x, y)``.  The style template before the flag run is ~30-48
    bytes of floats; its bytes do not form a long 0x00/0x01 run, so the first
    run of >= 30 flag bytes reliably marks the real array.  Returns the offset
    one past that run's *end* (0 when there is none).
    """
    run = 0
    start = 0
    for i in range(8, len(buf)):
        if buf[i] in (0, 1):
            if run == 0:
                start = i
            run += 1
        else:
            if run >= 30:
                return i
            run = 0
    if run >= 30:
        return len(buf)
    return 0


def _points_after_flag(raw: bytes) -> List[Tuple[float, float, float]]:
    """Decode the flag-run encoding: ``[x0][y0][u32 count] + count*(x, y)``.

    ``raw`` is the full buffer; the flag run's end is found first.  The per-point
    width is not stored, so points carry width 0 (rendered at the nominal width).
    """
    fe = _flag_run_end(raw)
    if fe + 12 > len(raw):
        return []
    count = struct.unpack("<I", raw[fe + 8:fe + 12])[0]
    p = fe + 12
    pts: List[Tuple[float, float, float]] = []
    for _ in range(count):
        if p + 8 > len(raw):
            break
        x, y = _f32(raw, p), _f32(raw, p + 4)
        if not (_COORD_MIN < x < _COORD_MAX and _COORD_MIN < y < _COORD_MAX):
            break
        pts.append((x, y, 0.0))
        p += 8
    return pts


def _longest_run(raw: bytes, stride: int, xoff: int, yoff: int,
                 woff: int = -1) -> Tuple[int, int]:
    """Return the ``(start, count)`` of the longest contiguous run of valid
    coordinate samples at ``stride`` bytes, reading ``(x, y[, w])`` at the given
    offsets.  ``woff`` -1 means no width is stored (stride‑8 / stride‑20)."""
    n = len(raw)

    def valid(o: int) -> bool:
        if o + stride > n:
            return False
        x, y = _f32(raw, o + xoff), _f32(raw, o + yoff)
        if not (_COORD_MIN < x < _COORD_MAX and _COORD_MIN < y < _COORD_MAX):
            return False
        if woff >= 0 and not (0.0 <= _f32(raw, o + woff) <= _WIDTH_MAX):
            return False
        return True

    best_start, best_count, s = 0, 0, 0
    while s + stride <= n:
        if valid(s):
            e = s
            while valid(e):
                e += stride
            c = (e - s) // stride
            if c > best_count:
                best_start, best_count = s, c
            s = e
        else:
            s += 1
    return best_start, best_count


def _stride12_widths_ok(raw: bytes, start: int, count: int) -> bool:
    """True if a stride‑12 run carries plausible per‑point pen widths.

    A genuine stride‑12 buffer stores a real width (a few points) in every
    third float.  A *stride‑8* buffer read at stride 12 instead has its y
    values land in the width slot, so the "widths" come out in the hundreds —
    implausible.  This separates a thin pen (stride‑12) from a widthless
    marker (stride‑8): the red bar's mis-read widths are ~100+, a real pen's
    are ~1‑3.
    """
    for i in range(count):
        w = _f32(raw, start + i * 12 + 8)
        if w < 0.0 or w > 10.0:
            return False
    return True


def _earliest_stride12(raw: bytes) -> Tuple[int, int]:
    """Return ``(start, count)`` of the first run of >= 2 valid stride‑12
    triplets at offset >= 64 (the original pen layout), else ``(0, 0)``."""
    n = len(raw)
    o = _POINT_SEARCH_START
    while o + _POINT_STRIDE <= n:
        if _valid_point(raw, o):
            start = o
            count = 0
            while _valid_point(raw, o):
                count += 1
                o += _POINT_STRIDE
            if count >= 2:
                return start, count
            o = start + _POINT_STRIDE
        else:
            o += 1
    return 0, 0


def extract_points(raw: bytes) -> List[Tuple[float, float, float]]:
    """Extract the stroke path from decompressed pen geometry.

    Three layouts exist:
      * **flag‑run**  ``[x0][y0][u32 count] + count*(x, y)`` after a long 0/1 run;
      * **stride‑12** ``(x, y, width)`` — the primary pen layout (has width);
      * **stride‑8**  ``(x, y)`` — a widthless marker layout (the red bar).
    The stride‑12 run is taken when its per‑point widths are plausible (a real
    pen); a stride‑8 buffer mis-read at stride 12 yields implausible widths
    (its y values land in the width slot), so we fall back to the stride‑8
    layout in that case.
    """
    if _flag_run_end(raw):
        pts = _points_after_flag(raw)
        if len(pts) >= 2:
            return pts
    s12, c12 = _earliest_stride12(raw)
    if c12 >= 2 and _stride12_widths_ok(raw, s12, c12):
        return [(_f32(raw, s12 + i * 12),
                 _f32(raw, s12 + i * 12 + 4),
                 _f32(raw, s12 + i * 12 + 8)) for i in range(c12)]
    # Stride-12 run absent or implausible (it was a stride-8 buffer): read the
    # widthless (x, y) layout.
    s8, c8 = _longest_run(raw, 8, 0, 4)
    if c8 >= 2:
        return [(_f32(raw, s8 + i * 8), _f32(raw, s8 + i * 8 + 4), 0.0)
                for i in range(c8)]
    return []


def extract_curve(raw: bytes) -> List[Tuple[float, float]]:
    """Extract a highlighter / eraser / pencil smooth curve.

    Points are stored as stride‑20 records ``[a, b, c, x, y]`` (five float32s,
    x at +12, y at +16) — the first three floats are per‑sample style attributes
    (pressure/velocity), the last two are the position.  The buffer carries a
    style template and flag array before the path; we keep the *longest*
    contiguous run of valid ``(x, y)`` samples.  A run must span real distance
    (all‑zero "eraser" placeholders are dropped).
    """
    n = len(raw)
    if n < 24:
        return []

    def valid(o: int) -> bool:
        if o + 20 > n:
            return False
        x, y = _f32(raw, o + 12), _f32(raw, o + 16)
        return 1.0 < x < 5000.0 and 1.0 < y < 5000.0

    best_start = best_len = 0
    s = 0
    while s < n:
        if valid(s):
            e = s
            while valid(e):
                e += 20
            if e - s > best_len:
                best_start, best_len = s, e - s
            s = e
        else:
            s += 1
    count = best_len // 20
    if count < 2:
        return []
    pts = [(_f32(raw, best_start + i * 20 + 12),
            _f32(raw, best_start + i * 20 + 16)) for i in range(count)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if (max(xs) - min(xs)) < 0.5 and (max(ys) - min(ys)) < 0.5:
        return []  # degenerate (all-zero placeholder)
    return pts


def _stroke_origin(content_fields) -> Optional[Tuple[float, float]]:
    """Read the stroke's origin/anchor offset from field ``#6``.

    Some strokes (the grouped marker strokes, tagged with a layer UUID in
    field ``#10``) store their points relative to a per-stroke origin instead
    of the canvas.  Field ``#6`` is a small nested message holding that origin
    as float32 sub-fields ``1 = x`` and ``2 = y``.  It is the translation that
    maps the stored (relative) frame back to canvas space, so it is *added* to
    every stored point.  Strokes without a ``#6`` message — including all
    primary (stride-12) strokes and the small flag-run strokes — carry no
    offset and decode in canvas coordinates directly.
    """
    blobs = content_fields.get(6)
    if not blobs:
        return None
    if not isinstance(blobs[0], (bytes, bytearray)):
        return None
    try:
        vals = {fno: val for fno, _wt, val in iter_fields(blobs[0])}
    except (ValueError, struct.error):
        return None
    x, y = vals.get(1), vals.get(2)
    if isinstance(x, float) and isinstance(y, float):
        return x, y
    return None


def _extract_color(content_fields) -> Tuple[float, float, float, float]:
    """Read the RGBA colour from field ``#4`` (float32 sub-fields 1..4)."""
    color = [0.0, 0.0, 0.0, 1.0]
    blobs = content_fields.get(4)
    if not blobs:
        return tuple(color)
    for sub_field, _wt, val in iter_fields(blobs[0]):
        if 1 <= sub_field <= 4 and isinstance(val, float):
            color[sub_field - 1] = val
    return tuple(color)


def _find_geometry_blob(content_fields) -> Optional[bytes]:
    for chunk in content_fields.get(2, []):
        if isinstance(chunk, (bytes, bytearray)) and is_apple_lz4(chunk):
            return bytes(chunk)
    return None


# --------------------------------------------------------------------------- #
# Placement matrix helpers (shared by images and text boxes)
# --------------------------------------------------------------------------- #

def _read_varint(data: bytes, i: int) -> Tuple[int, int]:
    shift = 0
    result = 0
    while True:
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7


def _get_blob(b: bytes, field_no: int) -> Optional[bytes]:
    """Fetch the raw bytes of a single length-delimited field."""
    i = 0
    n = len(b)
    while i < n:
        key, i = _read_varint(b, i)
        fno = key >> 3
        wt = key & 7
        if wt == 2:
            ln, i = _read_varint(b, i)
            if fno == field_no:
                return b[i:i + ln]
            i += ln
        elif wt == 0:
            _, i = _read_varint(b, i)
        elif wt == 5:
            i += 4
        elif wt == 1:
            i += 8
        else:
            break
    return None


def _point_xy(blob: bytes) -> Optional[Tuple[float, float]]:
    """Parse a point message: ``{1: f32 x, 2: f32 y}``."""
    d = {}
    try:
        for fno, _wt, v in iter_fields(blob):
            d[fno] = v
    except (ValueError, struct.error):
        return None
    if isinstance(d.get(1), float) and isinstance(d.get(2), float):
        return (d[1], d[2])
    return None


def _matrix_from(msg: bytes) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Parse the 2-point placement matrix from nested field ``#2``.

    Field ``#2`` is a message holding two point messages (sub-fields 1 and 2).
    Returns ``(point1, point2)`` — for images/text: (top-left, size).
    """
    f2 = _get_blob(msg, 2)
    if not f2:
        return None
    pts = []
    for pno in (1, 2):
        p = _get_blob(f2, pno)
        if p:
            xy = _point_xy(p)
            if xy:
                pts.append(xy)
    if len(pts) >= 2:
        return pts[0], pts[1]
    return None


def _raster_format(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    return "png"


def _rtf_to_plain(rtf: bytes) -> str:
    """Best-effort RTF → display text (strips control words, keeps text).

    Handles the common Cocoa RTF subset: drops the ``\\fonttbl`` /
    ``\\colortbl`` / ``\\stylesheet`` table groups, maps ``\\par``/``\\n`` to
    newlines, and removes every other control word.  Not a full RTF parser.
    """
    s = rtf.decode("cp1252", errors="replace")
    # drop the table groups ({\fonttbl ...}, {\colortbl ...}, ...) by finding
    # the opening brace *before* the marker and matching it
    for tbl in ("fonttbl", "colortbl", "expandedcolortbl", "stylesheet"):
        marker = "\\" + tbl
        i = s.find(marker)
        while i != -1:
            # opening brace is the one immediately preceding the marker
            j = s.rfind("{", 0, i)
            if j == -1:
                break
            depth = 0
            k = j
            while k < len(s):
                if s[k] == "{":
                    depth += 1
                elif s[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            if k < len(s):
                s = s[:j] + s[k + 1:]
            else:
                break
            i = s.find(marker)
    s = re.sub(r"\\'([0-9a-fA-F]{2})", "", s)

    def _sub(m):
        word = m.group(1)
        if word in ("n", "par"):
            return "\n"
        return ""

    s = re.sub(r"\\([a-zA-Z]+)(-?\d*) ?", _sub, s)
    s = s.replace("{", "").replace("}", "")
    s = s.replace("\\~", " ").replace("\\*", "*")
    lines = [ln.strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


# --------------------------------------------------------------------------- #
# Page / archive parsing
# --------------------------------------------------------------------------- #

def _marker_width(raw: bytes) -> Optional[float]:
    """Return the marker's uniform width (canvas units) if the buffer holds a
    marker width float at byte 40, else ``None``.

    Marker strokes (and the flag-run/stride-8 layouts) carry one uniform width
    as a float32 at offset 40 of the decompressed blob, right after the
    "tpl\\0"+length+style-template header.  Pen strokes have no such header —
    bytes 40-43 there are template data that decode to a huge, implausible
    float — so a sane range (0.5..500 canvas units) identifies a marker.
    """
    if len(raw) < _MARKER_WIDTH_OFFSET + 4:
        return None
    w = _f32(raw, _MARKER_WIDTH_OFFSET)
    if _MARKER_WIDTH_MIN <= w <= _MARKER_WIDTH_MAX:
        return w
    return None


def _parse_stroke(cf, scale: float = 1.0) -> Optional[Stroke]:
    """Build a Stroke from a field ``#7`` content message."""
    blob = _find_geometry_blob(cf)
    if blob is None:
        return None
    raw = apple_decompress(blob)
    color = _extract_color(cf)
    tool_id = cf.get(21, [None])[0]

    if tool_id == 25:
        pts = extract_curve(raw)
        if not pts:
            return None
        w = _HL_DEFAULT_WIDTH_PT
        return Stroke(points=[(x, y, w) for x, y in pts], color=color,
                      kind="highlighter")

    pts = extract_points(raw)
    if not pts:
        return None
    # flag-run / stride-8 strokes store no per-point width.  If the buffer
    # carries a marker width float (offset 40) use it — GoodNotes markers have
    # a single uniform width; otherwise fall back to the default pen width.
    if all(w == 0.0 for _x, _y, w in pts):
        mw = _marker_width(raw)
        if mw is not None and scale > 0:
            # offset-40 value × 0.25 = rendered page-pt (calibrated against
            # Test7's three known marker lines); store canvas units so the PDF
            # emitter's ×scale yields the right page thickness.
            w_canvas = mw * 0.25 / scale
        else:
            w_canvas = _PEN_DEFAULT_WIDTH_PT
        pts = [(x, y, w_canvas) for x, y, _w in pts]
    # Grouped strokes store their points relative to a per-stroke origin held
    # in field #6 (a nested {1: x, 2: y} float32 message); add it back.
    origin = _stroke_origin(cf)
    if origin is not None:
        pts = [(x + origin[0], y + origin[1], w) for x, y, w in pts]
    # f10 (a second UUID) marks strokes on a shared layer/group — it is NOT an
    # eraser flag; the stroke keeps its own stored colour.
    return Stroke(points=pts, color=color, kind="pen")


def parse_page(data: bytes, attachments: Dict[str, bytes],
              canvas: Optional[Tuple[float, float]],
              page_size: Optional[Tuple[float, float]]) -> Page:
    """Parse one ``notes/<UUID>`` page file into a :class:`Page`."""
    page = Page()
    if page_size:
        page.width, page.height = page_size
    if canvas and page_size:
        page.scale = page_size[0] / canvas[0]

    recs = list(read_length_delimited_records(data))

    # collect images and their object IDs for matrix matching
    pending_images: List[Tuple[Image, str]] = []

    for rec in recs:
        top = parse_message(rec)
        # pen / highlighter / eraser strokes: content in field #7
        for content in top.get(7, []):
            if not isinstance(content, (bytes, bytearray)):
                continue
            cf = parse_message(content)
            stroke = _parse_stroke(cf, scale=page.scale)
            if stroke:
                page.strokes.append(stroke)
        # images: top-level record with #4 = attachment UUID (no #7)
        if top.get(4) and not top.get(7):
            att0 = top[4][0]
            if not isinstance(att0, (bytes, bytearray)):
                continue
            att = bytes(att0).decode("ascii", "ignore").strip()
            img_data = attachments.get(att)
            if img_data:
                obj_id = ""
                if top.get(1) and isinstance(top[1][0], (bytes, bytearray)):
                    obj_id = bytes(top[1][0]).decode("ascii", "ignore").strip()
                pending_images.append((
                    Image(position=(0, 0), size=(0, 0),
                          data=img_data, fmt=_raster_format(img_data)),
                    obj_id,
                ))
        # text boxes: top-level record with field #8 (nested matrix + RTF)
        for f8 in top.get(8, []):
            if not isinstance(f8, (bytes, bytearray)):
                continue
            m = _matrix_from(f8)
            if m:
                top_left, size_vec = m
                rtf = None
                for k, _wt, v in iter_fields(f8):
                    if isinstance(v, (bytes, bytearray)) and b"\\rtf1" in v:
                        rtf = v
                # GoodNotes text boxes here use black text (the RTF \colortbl is
                # empty, meaning the default black).  The 15-byte sub-fields in
                # the box message are geometry, not colour floats, so we keep
                # the black default.
                color = (0.0, 0.0, 0.0, 1.0)
                if rtf is not None:
                    lines = parse_rtf_runs(bytes(rtf))
                    plain = "\n".join(ln.text().strip()
                                      for ln in lines if ln.text().strip())
                    # prefer the colour the RTF itself selects (default black)
                    for ln in lines:
                        if ln.runs:
                            color = ln.runs[0].color
                            break
                    page.texts.append(TextBox(
                        position=top_left, size=size_vec,
                        rtf=bytes(rtf).decode("cp1252", "replace"),
                        plain=plain, color=color, lines=lines,
                    ))

    # second pass: match images to their companion matrix records
    for rec in recs:
        top = parse_message(rec)
        for nested in top.get(1, []):
            if not isinstance(nested, (bytes, bytearray)):
                continue
            m = _matrix_from(nested)
            if not m:
                continue
            nid = _get_blob(nested, 1)
            if not nid:
                continue
            nid = nid.decode("ascii", "ignore").strip()
            for i, (img, obj_id) in enumerate(pending_images):
                if obj_id == nid:
                    pos, sz = m
                    img.position = pos
                    img.size = sz
                    page.images.append(img)
                    pending_images.pop(i)
                    break

    return page


def _page_members(names) -> List[str]:
    return sorted(n for n in names if "/notes/" in n or n.startswith("notes/"))


def _read_deleted_pages(opener, names) -> List[str]:
    """Return the full 36-char ``notes/`` member names of *deleted* pages.

    ``index.events.pb`` is the op log.  A page is created by an op carrying
    field #54 and later deleted/hidden by an op carrying field #56 (the same
    page UUID).  Deleted members are kept in the archive but must not produce
    a page (the reference export omits them).  Note the op log stores an
    *internal* page id that differs from the member name by one nibble in the
    final hex byte, so matching is on the first 32 chars.
    """
    if "index.events.pb" not in names:
        return []
    try:
        data = opener("index.events.pb")
    except Exception:
        return []
    deleted = set()
    for rec in read_length_delimited_records(data):
        try:
            top = parse_message(rec)
        except Exception:
            continue
        f1 = top.get(1, [None])[0]
        if not isinstance(f1, (bytes, bytearray)):
            continue
        fields = {k for k in top if k != 1}
        if 56 in fields:
            deleted.add(bytes(f1).decode("ascii", "ignore").strip()[:32])
    if not deleted:
        return []
    out = []
    for n in names:
        if "/notes/" in n or n.startswith("notes/"):
            if n.split("/")[-1][:32] in deleted:
                out.append(n)
    return out


def _read_page_order(opener, names, fallback) -> List[str]:
    """Return page member names in document order.

    ``index.notes.pb`` lists the pages in their display order (each record's
    field #1 is a page UUID); we map those to ``notes/<UUID>`` members.  When
    the index is absent we fall back to the sorted member list.
    """
    if "index.notes.pb" not in names:
        return list(fallback)
    try:
        data = opener("index.notes.pb")
    except Exception:
        return list(fallback)
    nameset = set(names)
    ordered = []
    for rec in read_length_delimited_records(data):
        top = parse_message(rec)
        if top.get(1) and isinstance(top[1][0], (bytes, bytearray)):
            member = "notes/" + bytes(top[1][0]).decode("ascii", "ignore").strip()
            if member in nameset and member not in ordered:
                ordered.append(member)
    for n in fallback:  # keep any pages missing from the index (defensive)
        if n not in ordered:
            ordered.append(n)
    return ordered


def _read_page_backgrounds(opener, names) -> Dict[str, str]:
    """Map page member UUID -> background paper attachment UUID.

    ``index.events.pb`` is an operation log.  A page is *created* by an op whose
    field #1 is the page UUID and which carries field #54; a *paper* is applied
    by an op whose field #1 is the paper (background PDF) UUID and which carries
    field #105.  Creations and paper-assignments are interleaved in document
    order, so we record both sequences and zip them: the i-th created page gets
    the i-th assigned paper, and a page with no explicit assignment inherits the
    most recent paper (a freshly created page keeps the current paper).  When the
    events file is absent we return an empty mapping (no backgrounds).
    """
    if "index.events.pb" not in names:
        return {}
    member_names = _page_members(names)
    try:
        data = opener("index.events.pb")
    except Exception:
        return {}
    creations: List[str] = []   # page UUIDs, creation order
    papers: List[str] = []      # paper UUIDs, assignment order
    for rec in read_length_delimited_records(data):
        top = parse_message(rec)
        f1 = top.get(1, [None])[0]
        if not isinstance(f1, (bytes, bytearray)):
            continue
        uuid = bytes(f1).decode("ascii", "ignore").strip()
        if not uuid or uuid.count("-") != 4:
            continue
        fields = {k for k in top if k != 1}
        if 54 in fields:
            if uuid not in creations:
                creations.append(uuid)
        elif 105 in fields:
            papers.append(uuid)
    if not papers:
        return {}
    # NOTE: the page-creation ops store an *internal* page-object id that differs
    # from the ``notes/`` member name by exactly one nibble in the final hex byte
    # (e.g. ``...EC7A`` vs member ``...EC7B``).  The first 32 chars are identical,
    # so we join on that prefix.
    def p32(u):
        return u[:32]

    member_prefix = {p32(name.split("/")[-1]): name.split("/")[-1]
                     for name in member_names}
    bg: Dict[str, str] = {}
    last = None
    for i, created in enumerate(creations):
        if i < len(papers):
            last = papers[i]
        if last:
            member = member_prefix.get(p32(created))
            if member:
                bg[member] = last
    return bg


def _read_page_members(opener, names, attachments, canvas,
                       page_size) -> List[Page]:
    """Parse every ``notes/<UUID>`` member, in document order, keeping empty
    pages (a page may hold only a background)."""
    fallback = _page_members(names)
    order = _read_page_order(opener, names, fallback)
    bg_map = _read_page_backgrounds(opener, names)
    deleted = set(_read_deleted_pages(opener, names))
    pages = []
    for name in order:
        if name in deleted:
            continue  # page was deleted; keep it out of the output
        data = opener(name)
        page = parse_page(data, attachments, canvas, page_size)
        paper = bg_map.get(name.split("/")[-1])
        if paper and paper in attachments:
            page.background = attachments[paper]
        pages.append(page)
    return pages


def _collect_attachments(opener, names) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    for n in names:
        if n.startswith("attachments/"):
            data = opener(n)
            if data:
                out[n.split("/")[-1]] = data
    return out


def _read_page_size(opener, names) -> Optional[Tuple[float, float]]:
    """Recover the page point size from a paper PDF attachment.

    Paper PDFs are stored in ``attachments/<UUID>`` with no extension; they
    start with ``%PDF`` and carry a ``/MediaBox [0 0 W H]``.  When all paper
    PDFs share the same size we use it; otherwise we fall back to A4.
    """
    import re as _re
    import zlib as _zlib
    sizes = set()
    for n in names:
        if not n.startswith("attachments/"):
            continue
        data = opener(n)
        if not data or not data.startswith(b"%PDF"):
            continue
        m = _re.search(rb"/MediaBox\s*\[0\s+0\s+([\d.]+)\s+([\d.]+)\]", data)
        if m:
            sizes.add((float(m.group(1)), float(m.group(2))))
    if len(sizes) == 1:
        return sizes.pop()
    return None


def _read_canvas_size(opener, names,
                      page_size: Optional[Tuple[float, float]]
                      ) -> Optional[Tuple[float, float]]:
    """Recover the canvas point size from ``index.events.pb``.

    The event log stores the canvas width and height as float32 fields
    (834.24 × 1078.82 for sample 5), nested deep in protobuf messages.  A raw
    f32 byte-scan is noisy, so we disambiguate with the *known* page size:
    the canvas and the page share the same aspect ratio, so the true
    ``(canvas_w, canvas_h)`` pair is the one for which
    ``page_w / canvas_w == page_h / canvas_h`` (a uniform viewport scale).
    Returns ``None`` when no such pair exists (samples 1-4 have no events
    file, and their canvas is the page itself, scale 1.0).
    """
    if "index.events.pb" not in names or not page_size:
        return None
    try:
        data = opener("index.events.pb")
    except Exception:
        return None
    pw, ph = page_size
    vals = set()
    for i in range(0, len(data) - 3):
        v = struct.unpack_from("<f", data, i)[0]
        if 200.0 < v < 5000.0:  # plausible note-canvas dimension in points
            vals.add(round(v, 1))
    best = None
    for w in vals:
        for h in vals:
            if w >= h * 2.0 or h >= w * 2.0:
                continue
            sx = pw / w
            sy = ph / h
            if 0.1 <= sx <= 3.0 and abs(sx - sy) < 1e-3:
                # prefer the largest canvas (smallest scale) among matches
                if best is None or w * h > best[0] * best[1]:
                    best = (w, h)
    if best:
        return best
    return None


def parse_goodnotes(path: str) -> GoodNotesDocument:
    """Parse a ``.goodnotes`` archive (or an already-extracted directory)."""
    title = os.path.splitext(os.path.basename(path.rstrip("/")))[0]

    if os.path.isdir(path):
        names = []
        for root, _dirs, files in os.walk(path):
            for f in files:
                names.append(os.path.relpath(os.path.join(root, f), path))

        def opener(name):
            with open(os.path.join(path, name), "rb") as fh:
                return fh.read()

        attachments = _collect_attachments(opener, names)
        page_size = _read_page_size(opener, names)
        canvas = _read_canvas_size(opener, names, page_size)
        pages = _read_page_members(opener, names, attachments, canvas, page_size)
        return GoodNotesDocument(pages=pages, title=title, canvas=canvas)
    else:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            attachments = _collect_attachments(zf.read, names)
            page_size = _read_page_size(zf.read, names)
            canvas = _read_canvas_size(zf.read, names, page_size)
            pages = _read_page_members(zf.read, names, attachments, canvas, page_size)
            return GoodNotesDocument(pages=pages, title=title, canvas=canvas)
