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
    get_bytes,
    iter_fields,
    iter_fields_tolerant,
    parse_message_tolerant,
    read_length_delimited_records,
)
from .records import (
    KIND_FILL,
    KIND_IMAGE,
    KIND_STROKE,
    KIND_TEXT,
    KIND_TEXTBOX,
    PageObject,
    read_page_objects,
)
from .richtext import decode_text_object
from .shapes import decode_outline, outline_width
from .text_rtf import TextLine, TextRun, parse_rtf_runs

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

# Pencil strokes (grayscale + semi-transparent, widthless flag-run encoding)
# fall back to _PEN_DEFAULT_WIDTH_PT, which smears them into fat ~11 pt bands.
# The reference draws the gray pencil doodle at ~4 pt (pixel-density match:
# 17568 vs 17615 ref gray px at 4 pt on the laptop region, vs 13922 at 2 pt).
# Apply this only to gray, alpha<0.9 strokes — never to black marks (alpha 1.0)
# or colored tape (not gray), which keep the thick default on purpose.
_PENCIL_DEFAULT_WIDTH_PT = 4.0

# Pencil strokes are pressure-sensitive: each stride-12 record stores
# (x, y, pressure) where pressure (third float) is a 0..~1 per-point value.
# GoodNotes renders the pencil width proportional to pressure.  Measured
# against the reference (gray word strokes ~0.8-1.2 pt), the mapping
#   width_canvas = _PENCIL_PRESSURE_GAIN * pressure
# reproduces the reference stroke weight (median pressure ~0.45 -> ~4 canvas
# units -> ~2.2 pt page).  A floor keeps the lightest taps visible.
_PENCIL_PRESSURE_GAIN = 9.0
_PENCIL_MIN_PRESSURE = 0.10

# Shape outline width (canvas units) when f9.f15 is absent: the value every
# shape in the samples carries.
_SHAPE_DEFAULT_WIDTH = 10.3937


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
    ``fill`` is the RGBA interior colour of a filled (closed) shape, else None;
    writers paint it underneath the outline.
    """

    points: List[Tuple[float, float, float]]  # (x, y, width)
    color: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    kind: str = "pen"
    fill: Optional[Tuple[float, float, float, float]] = None

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
    # Field-21 sticker text carries an explicit *canvas*-unit size (still
    # scaled by ``s`` at emit time).  0 = not present -> the emitter falls back
    # to guessing the size from the RTF (the field-8 text-box path).
    font_size: float = 0.0


@dataclass
class Page:
    strokes: List[Stroke] = field(default_factory=list)  # in paint order
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
    for i in range(8, len(buf)):
        if buf[i] in (0, 1):
            run += 1
        else:
            if run >= 30:
                return i
            run = 0
    if run >= 30:
        return len(buf)
    return 0


def _stride8_scan(raw: bytes, start: int, max_gap: int = 3) -> List[Tuple[float, float, float]]:
    """Collect stride-8 ``(x, y)`` pairs from ``start``, skipping isolated
    invalid samples.

    A single garbage sample used to *truncate* a path: a closed loop drawn as
    ~132 stride-8 points had one bad pair mid-way, so the old "longest
    contiguous run" reader returned only the first ~half (the "cut short"
    defect).  We now skip up to ``max_gap - 1`` consecutive invalid samples,
    then stop when a longer non-point stretch begins.  Coordinates are gated
    to the page bound so trailing template bytes are not collected.
    """
    pts: List[Tuple[float, float, float]] = []
    bad = 0
    o = start
    while o + 8 <= len(raw):
        x, y = _f32(raw, o), _f32(raw, o + 4)
        if _COORD_MIN < x < _COORD_MAX and _COORD_MIN < y < _COORD_MAX:
            pts.append((x, y, 0.0))
            bad = 0
        else:
            bad += 1
            if bad >= max_gap:
                break
        o += 8
    return pts


def _points_after_flag(raw: bytes) -> List[Tuple[float, float, float]]:
    """Decode a genuine flag-run (stride-8 / stride-16) stroke.

    Layout: ``template + flag-run + [x0][y0][u32 count] + point array`` where
    ``base = fe + 12`` starts the array.  Two point layouts exist, distinguished
    by the buffer-length signature ``rem = len(raw) - base``:

    * ``rem == 16·count`` — **stride-16**: each 16-byte record holds two smooth
      points ``(x, y, x', y')``; emit both.  (Freehand ink strokes.)
    * ``rem == 8·count``  — **stride-8**: ``count`` single ``(x, y)`` points.
    * ``rem > 8·count``   — **stride-8, spurious count**: the stored count is a
      mis-read (e.g. the loop's left edge), so collect *all* valid stride-8
      pairs up to a short gap.  This is what fixed closed loops that were being
      cut short at the first bad sample.

    A spurious flag-run (no valid points at ``base``) returns ``[]`` so the
    caller can fall through to the stride-12 pen layout.
    """
    fe = _flag_run_end(raw)
    base = fe + 12
    if base > len(raw):
        return []
    rem = len(raw) - base
    count = struct.unpack("<I", raw[fe + 8:fe + 12])[0]

    # Stride-16: each record holds two points.
    if 0 < count < 10_000_000 and rem == 16 * count:
        pts: List[Tuple[float, float, float]] = []
        for i in range(count):
            o = base + i * 16
            if o + 16 > len(raw):
                break
            x1, y1 = _f32(raw, o), _f32(raw, o + 4)
            x2, y2 = _f32(raw, o + 8), _f32(raw, o + 12)
            if _COORD_MIN < x1 < _COORD_MAX and _COORD_MIN < y1 < _COORD_MAX:
                pts.append((x1, y1, 0.0))
            if _COORD_MIN < x2 < _COORD_MAX and _COORD_MIN < y2 < _COORD_MAX:
                pts.append((x2, y2, 0.0))
        return pts if len(pts) >= 2 else []

    # Stride-8: the count may be a spurious read; collect all valid pairs.
    pts8 = _stride8_scan(raw, base)
    if len(pts8) < 2:
        return []
    if 2 <= count < len(pts8) and rem == 8 * count:
        return pts8[:count]
    return pts8


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


def _is_coherent(pts: List[Tuple[float, float, float]],
                 max_median_jump: float = 40.0) -> bool:
    """Reject a decoded path whose points are not spatially coherent.

    A genuine pen/marker stroke is continuous: the median distance between
    consecutive points is small (measured <= 14 canvas units across the
    samples).  A *mis-decoded* blob (a template / wrong-phase buffer read as a
    path) yields points that teleport around the page — the Test9 stroke i=108
    has a median consecutive jump of ~712 units and paints a giant smudge.
    Threshold of 40 sits comfortably above every real stroke (~14) and far
    below a garbage path (~700).
    """
    if len(pts) < 8:
        return True
    jumps = []
    for i in range(1, len(pts)):
        dx = pts[i][0] - pts[i - 1][0]
        dy = pts[i][1] - pts[i - 1][1]
        jumps.append((dx * dx + dy * dy) ** 0.5)
    jumps.sort()
    return jumps[len(jumps) // 2] <= max_median_jump


def extract_points(raw: bytes) -> List[Tuple[float, float, float]]:
    """Extract the stroke path from decompressed pen geometry.

    Layouts (checked in this order):
      * **stride-12** ``(x, y, width)`` — a real pen, with plausible per-point
        widths.  Checked *first* because a valid-width run is the most specific
        signal: a spurious flag-run sitting before a stride-12 pen used to
        steal it (the Test4 red stroke).
      * **flag-run**  ``template + flag-run + [x0][y0][u32 count] + points`` —
        a genuine widthless stroke (stride-8 / stride-16).  Taken when the
        buffer length matches the stored count (or the count is a spurious
        mis-read) and no valid stride-12 pen exists.
      * **stride-8**  ``(x, y)`` — a widthless marker (the red bar), read via
        the longest contiguous run when the flag-run signature doesn't hold.
    """
    s12, c12 = _earliest_stride12(raw)
    if c12 >= 2 and _stride12_widths_ok(raw, s12, c12):
        # A stride-12 pen/marker stores (x, y, width) records, but interleaves
        # control / pen-up records every third slot, so a *contiguous* run is
        # short even when the real path is long.  Read every stride-12 record
        # at the anchor's phase and keep the valid ones, skipping the control
        # records (this is what reconstructs the full red-tape band that
        # ``_earliest_stride12``'s contiguous count would truncate to a dot).
        pts: List[Tuple[float, float, float]] = []
        bad = 0
        o = s12
        while o + _POINT_STRIDE <= len(raw):
            # A real stride-12 record has plausible coordinates AND a plausible
            # width.  The width gate matters: past the real path the phase
            # mis-aligns and the *y* lands in the width slot (reads 30..200),
            # so a width bound of 10 (matching ``_stride12_widths_ok``) both
            # bridges the interleaved control records and stops before the junk.
            w = _f32(raw, o + 8)
            if _valid_point(raw, o) and 0.0 <= w <= 10.0:
                # The stored per-point value is a half-width (radius): the
                # reference marker/pen band is 2.00x the width that w*scale
                # produces (e.g. the red tape stores 9.0 -> 4.91pt, but the
                # reference renders it at 9.83pt).  Store the diameter so the
                # emitter (w * scale) yields the true ink thickness.
                pts.append((_f32(raw, o), _f32(raw, o + 4), w * 2.0))
                bad = 0
            else:
                bad += 1
                if bad > 3:  # control records appear at most every 3rd slot
                    break
            o += _POINT_STRIDE
        if len(pts) >= 2:
            return pts
    # No valid stride-12 pen.  A genuine flag-run is identified by a buffer
    # length that is a whole multiple of the per-record stride (8 or 16 bytes)
    # times the stored count, or by a count too small to fill the buffer
    # (spurious mis-read -> collect all valid pairs).
    if _flag_run_end(raw):
        pts = _points_after_flag(raw)
        if len(pts) >= 2:
            return pts
    # Stride-8 widthless marker: longest contiguous (x, y) run.
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


def extract_pencil(raw: bytes) -> List[Tuple[float, float, float]]:
    """Extract a pressure-sensitive *pencil* path from its geometry blob.

    The pencil stores stride-12 records ``(x, y, pressure)`` — the third float
    is the per-point pressure, i.e. the per-point width (the pencil is
    pressure-sensitive, unlike the uniform-width highlighter).  Real anchor
    points sit at a fixed phase and have plausible canvas coordinates
    (``1 < x, y``); the interleaved control / Bézier-handle records carry
    ``x < 1`` or ``y < 1`` and are skipped.  ``extract_curve`` (stride-20) and
    ``extract_points`` both mis-read this layout (they read the wrong floats /
    break at the control records), returning only a few far-apart points that
    the emitter connects into giant chords.  We instead scan for contiguous
    runs of valid stride-12 anchors and return ``(x, y, pressure)`` triples in
    drawing order.
    """
    n = len(raw)
    if n < 24:
        return []

    def anchor(o: int) -> Optional[Tuple[float, float, float]]:
        if o + _POINT_STRIDE > n:
            return None
        x, y, p = _f32(raw, o), _f32(raw, o + 4), _f32(raw, o + 8)
        # Real anchors sit well inside the canvas.  The stride-12 array also
        # carries spurious pen-state / reset records with tiny near-origin
        # coordinates (x, y ~ 1-3); letting those through connects the stroke
        # to the top-left corner and draws a giant chord.  A floor of 5 (the
        # canvas corner is off-page, so no real anchor ever lands there) drops
        # them without touching genuine points.
        if not (5.0 < x < 1600.0 and 5.0 < y < 1600.0):
            return None
        # A real pencil anchor always carries positive pressure (min ~0.046 in
        # practice).  Reset / pen-lift sentinel records carry pressure ~0 (a
        # subnormal float), e.g. a stray (58, 98) point; including one connects
        # the stroke to it and draws a giant diagonal chord.  Require a
        # minimum pressure to drop those sentinels.
        if not (0.02 <= p < 3.0):
            return None
        return x, y, p

    pts: List[Tuple[float, float, float]] = []
    s = 0
    while s + _POINT_STRIDE <= n:
        a = anchor(s)
        if a is None:
            s += 1
            continue
        e = s
        while anchor(e) is not None:
            e += _POINT_STRIDE
        for i in range((e - s) // _POINT_STRIDE):
            a = anchor(s + i * _POINT_STRIDE)
            if a is not None:
                pts.append(a)
        s = e
    if len(pts) < 2:
        return []
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    if (max(xs) - min(xs)) < 0.5 and (max(ys) - min(ys)) < 0.5:
        return []  # degenerate (all-zero placeholder)
    return pts


def _stroke_origin(content_fields) -> Optional[Tuple[float, float]]:
    """Read an object's translation from field ``#6`` (``{1: dx, 2: dy}``).

    Moving a stroke or shape in GoodNotes does not rewrite its geometry; it
    stores the move as this translation, which is *added* to every stored
    point (Test_11 "Moved" page).  Objects never moved carry an empty ``#6``.
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


def _rgba(msg) -> Optional[Tuple[float, float, float, float]]:
    """Decode an RGBA message (float32 sub-fields ``1..4``).

    An omitted R/G/B is 0.0; an omitted alpha is taken as opaque.
    """
    if not isinstance(msg, (bytes, bytearray)):
        return None
    color = [0.0, 0.0, 0.0, 1.0]
    for sub_field, _wt, val in iter_fields_tolerant(bytes(msg)):
        if 1 <= sub_field <= 4 and isinstance(val, float):
            color[sub_field - 1] = val
    return (color[0], color[1], color[2], color[3])


def _extract_color(content_fields) -> Tuple[float, float, float, float]:
    """Read a stroke's RGBA colour from field ``#4`` (default opaque black)."""
    return _rgba(content_fields.get(4, [None])[0]) or (0.0, 0.0, 0.0, 1.0)


def _find_geometry_blob(content_fields) -> Optional[bytes]:
    for chunk in content_fields.get(2, []):
        if isinstance(chunk, (bytes, bytearray)) and is_apple_lz4(chunk):
            return bytes(chunk)
    return None


# --------------------------------------------------------------------------- #
# Placement matrix helpers (shared by images and text boxes)
# --------------------------------------------------------------------------- #

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
    f2 = get_bytes(msg, 2)
    if not f2:
        return None
    pts = []
    for pno in (1, 2):
        p = get_bytes(f2, pno)
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


def _is_pencil(color) -> bool:
    """True for a GoodNotes *pencil* stroke: grayscale and semi-transparent.

    Pencil ink is stored as gray (r == g == b) at alpha ~0.5, and uses the
    widthless flag-run encoding (no offset-40 width, no per-point width).  This
    separates it from black marks (alpha ~1.0) and colored tape (not gray), so
    only pencil gets the thin width.  (A black marker has alpha 1.0; a colored
    tape has r != g != b.)
    """
    r, g, b, a = color
    return abs(r - g) < 0.02 and abs(g - b) < 0.02 and a < 0.9


def _parse_stroke(cf, scale: float = 1.0) -> Optional[Stroke]:
    """Build a Stroke from a field ``#7`` content message."""
    blob = _find_geometry_blob(cf)
    raw = apple_decompress(blob) if blob is not None else None
    color = _extract_color(cf)
    tool_id = cf.get(21, [None])[0]

    # Shape-recognised strokes keep their exact geometry in f9 (the f2 blob is
    # only the style template).  An empty f9 means a freehand stroke.
    f9 = cf.get(9, [b""])[0]
    if isinstance(f9, (bytes, bytearray)) and f9:
        outline = decode_outline(bytes(f9))
        if outline:
            w = outline_width(bytes(f9)) or _SHAPE_DEFAULT_WIDTH
            # A moved shape keeps its original geometry plus a translation in
            # f6, exactly like a moved freehand stroke.
            dx, dy = _stroke_origin(cf) or (0.0, 0.0)
            return Stroke(points=[(x + dx, y + dy, w) for x, y in outline.points],
                          color=color, kind="pen")

    if tool_id == 25 and raw is not None:
        # Pencil: dense pressure-sensitive stride-12 (x, y, pressure) records.
        # The third float is the per-point pressure -> per-point width.
        ppts = extract_pencil(raw)
        if not ppts:
            # Fall back to the legacy highlighter/curve layout (some builds).
            pts = extract_curve(raw)
            if not pts:
                return None
            w = _HL_DEFAULT_WIDTH_PT
            return Stroke(points=[(x, y, w) for x, y in pts], color=color,
                          kind="highlighter")
        # pressure (0..~1) -> canvas-unit width; store so the emitter ×scale.
        per_pt = []
        for x, y, p in ppts:
            w = max(_PENCIL_PRESSURE_GAIN * p,
                    _PENCIL_PRESSURE_GAIN * _PENCIL_MIN_PRESSURE)
            per_pt.append((x, y, w))
        return Stroke(points=per_pt, color=color, kind="pen")

    pts = extract_points(raw) if raw is not None else []
    # A blob that decodes into a spatially incoherent scatter (points teleport
    # hundreds of units between consecutive samples) is a mis-read template,
    # not a real stroke — e.g. Test9 i=108, which paints a giant page-spanning
    # smudge.  Drop it rather than emit the artifact.
    if pts and not _is_coherent(pts):
        return None
    # Fountain-pen / pressure strokes in the latest build carry their points as
    # repeated field-#4 point messages ({1: x, 2: y}, no per-point width) rather
    # than an LZ4 blob.  A single uniform width float lives in field #6.  Only
    # fall back to this when there is no LZ4 geometry (raw is None), so the
    # colour blob in #4 of a normal stroke is never mistaken for points.
    if not pts and raw is None:
        f4_pts = []
        for p in cf.get(4, []):
            if not isinstance(p, (bytes, bytearray)):
                continue
            xy = _point_xy(bytes(p))
            if xy:
                f4_pts.append(xy)
        if len(f4_pts) >= 2:
            f6 = cf.get(6, [None])[0]
            w_canvas = (float(f6) if isinstance(f6, float) and 0.05 < f6 < 200
                        else _PEN_DEFAULT_WIDTH_PT)
            origin = _stroke_origin(cf)
            if origin is not None:
                f4_pts = [(x + origin[0], y + origin[1]) for x, y in f4_pts]
            # This layout keeps its points in #4, so _extract_color (which reads
            # #4 as RGBA) is wrong here.  The stroke carries no per-point RGBA;
            # its ink is the tool's stored colour, approximated as a dark gray.
            fp_color = (0.30, 0.30, 0.30, 1.0)
            return Stroke(points=[(x, y, w_canvas) for x, y in f4_pts],
                          color=fp_color, kind="pen")
    if not pts:
        return None
    # If the buffer carries a marker width float (offset 40) use it — GoodNotes
    # markers have a single uniform width; otherwise fall back to the default pen width.
    if all(w == 0.0 for _x, _y, w in pts):
        mw = _marker_width(raw)
        if mw is not None and scale > 0:
            # offset-40 value × 0.25 = rendered page-pt (calibrated against
            # Test7's three known marker lines); store canvas units so the PDF
            # emitter's ×scale yields the right page thickness.
            w_canvas = mw * 0.25 / scale
        elif _is_pencil(color):
            # Grayscale, semi-transparent, widthless = the pencil.  Thin it
            # down from the fat marker default so the gray doodle doesn't smear.
            w_canvas = (_PENCIL_DEFAULT_WIDTH_PT / scale) if scale > 0 else _PENCIL_DEFAULT_WIDTH_PT
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
    """Parse one ``notes/<UUID>`` page file into a :class:`Page`.

    Only live objects are drawn (see :mod:`goodparse.records`): deleted ones
    stay in the file with a tombstone flag.
    """
    page = Page()
    if page_size:
        page.width, page.height = page_size
    if canvas and page_size:
        page.scale = page_size[0] / canvas[0]

    by_id: Dict[str, Stroke] = {}
    fills: List[PageObject] = []
    for obj in read_page_objects(data):
        if obj.kind == KIND_STROKE:
            stroke = _parse_stroke(obj.fields, scale=page.scale)
            if stroke:
                page.strokes.append(stroke)
                by_id[obj.id] = stroke
        elif obj.kind == KIND_FILL:
            fills.append(obj)
        elif obj.kind == KIND_IMAGE:
            img = _parse_image(obj, attachments)
            if img:
                page.images.append(img)
        elif obj.kind == KIND_TEXTBOX:
            box = _parse_textbox(obj.body)
            if box:
                page.texts.append(box)
        elif obj.kind == KIND_TEXT:
            box = _parse_text_object(obj.body)
            if box:
                page.texts.append(box)
    for obj in fills:
        _apply_fill(obj, by_id)
    # The file lists strokes topmost-first; expose them in paint order
    # (bottom-most first) so every writer can simply draw them in sequence.
    page.strokes.reverse()
    return page


def _apply_fill(obj: PageObject, strokes: Dict[str, "Stroke"]) -> None:
    """Attach a fill object's colour to the shape outline it fills.

    A fill (body kind 9) references its outline stroke by UUID in ``f5`` and
    stores the fill RGBA in ``f7``; its ``f4`` repeats the outline geometry, so
    the outline's points already describe the filled area.
    """
    target = obj.fields.get(5, [None])[0]
    if not isinstance(target, (bytes, bytearray)):
        return
    stroke = strokes.get(bytes(target).decode("ascii", "ignore").strip())
    if stroke is None:
        return
    rgba = _rgba(obj.fields.get(7, [None])[0])
    if rgba is not None:
        stroke.fill = rgba


def _parse_image(obj: PageObject, attachments: Dict[str, bytes]) -> Optional[Image]:
    """Image object: attachment UUID in the header, placement matrix in the body."""
    data = attachments.get(obj.attachment or "")
    m = _matrix_from(obj.body)
    if not data or not m:
        return None
    pos, size = m
    return Image(position=pos, size=size, data=data, fmt=_raster_format(data))


def _parse_textbox(f8: bytes) -> Optional[TextBox]:
    """Field-#8 text box: placement matrix plus a Cocoa RTF document."""
    m = _matrix_from(f8)
    if not m:
        return None
    top_left, size_vec = m
    rtf = None
    for _k, _wt, v in iter_fields_tolerant(f8):
        if isinstance(v, (bytes, bytearray)) and b"\\rtf1" in v:
            rtf = bytes(v)
    if rtf is None:
        return None
    lines = parse_rtf_runs(rtf)
    plain = "\n".join(ln.text().strip() for ln in lines if ln.text().strip())
    # The box colour is the first run's RTF colour (an empty \colortbl = black).
    color = next((ln.runs[0].color for ln in lines if ln.runs), (0.0, 0.0, 0.0, 1.0))
    return TextBox(position=top_left, size=size_vec,
                   rtf=rtf.decode("cp1252", "replace"),
                   plain=plain, color=color, lines=lines)


def _parse_text_object(f21: bytes) -> Optional[TextBox]:
    """Kind-21 text object (see :mod:`goodparse.richtext`)."""
    obj = decode_text_object(f21)
    if obj is None:
        return None
    plain = "\n".join(ln.text() for ln in obj.lines)
    color = obj.lines[0].runs[0].color
    size = max((r.size or obj.font_size) for ln in obj.lines for r in ln.runs)
    return TextBox(position=obj.position, size=obj.size, rtf="", plain=plain,
                   color=color, lines=obj.lines, font_size=size)


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
            top = parse_message_tolerant(rec)
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
        top = parse_message_tolerant(rec)
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
        top = parse_message_tolerant(rec)
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
    pages (a page may hold only a background).

    Per-page geometry (size + scale) is read from the event log: in current
    GoodNotes builds each page has its own canvas (``f54`` -> layer ->
    create-info ``f8``), so a single global page size is wrong for documents
    mixing page sizes (A4 + landscape + strips).  Old builds, which carry no
    such per-page canvas, fall back to the global ``canvas``/``page_size``.
    """
    member_names = _page_members(names)
    meta = _read_page_meta(opener, names, member_names)

    # bare-member-name (no "notes/" prefix) -> (canvas, paper)
    per_page = {m.split("/")[-1]: (c, p) for m, _p, c, p in meta}

    order: List[str] = []
    if meta:
        for m, _layer, _c, _p in meta:
            if m and m not in order:
                order.append(m)
    for m in _read_page_order(opener, names, member_names):  # defensive fill
        if m not in order:
            order.append(m)

    bg_map = _read_page_backgrounds(opener, names)
    deleted = set(_read_deleted_pages(opener, names))
    paper_att = _read_paper_to_attachment(opener, names)

    def resolve_paper(paper):
        if not paper:
            return None
        # a paper UUID may be an internal id; map to its real attachment
        return paper_att.get(paper, paper)

    pages = []
    for name in order:
        if name in deleted:
            continue  # page was deleted; keep it out of the output
        member = name.split("/")[-1]
        data = opener(name)
        p_canvas, p_paper = per_page.get(member, (None, None))
        if p_canvas:
            ps = _paper_size(opener, names, resolve_paper(p_paper))
            page = parse_page(data, attachments, p_canvas, ps)
        else:
            page = parse_page(data, attachments, canvas, page_size)
        paper = resolve_paper(p_paper) or resolve_paper(bg_map.get(member))
        if paper and paper in attachments:
            page.background = attachments[paper]
        pages.append(page)
    return pages


_UUID_RE = re.compile(
    rb"([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})")


def _uuid_from(v) -> Optional[str]:
    if not isinstance(v, (bytes, bytearray)):
        return None
    m = _UUID_RE.search(bytes(v))
    return m.group(1).decode("ascii").upper() if m else None


def _paper_size(opener, names, paper) -> Optional[Tuple[float, float]]:
    """Page size from a paper PDF attachment's MediaBox ``[0 0 W H]``."""
    if not paper or f"attachments/{paper}" not in names:
        return None
    try:
        data = opener(f"attachments/{paper}")
    except Exception:
        return None
    if not data.startswith(b"%PDF"):
        return None
    m = re.search(
        rb"/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]", data)
    if not m:
        m = re.search(rb"/MediaBox\s*\[0\s+0\s+([\d.]+)\s+([\d.]+)\]", data)
        if m:
            return (float(m.group(1)), float(m.group(2)))
        return None
    return (float(m.group(3)), float(m.group(4)))


def _read_page_meta(opener, names, member_names) -> List[Tuple[str, str,
                                                                Optional[Tuple[float, float]], Optional[str]]]:
    """Derive per-page metadata from ``index.events.pb``.

    Returns ``[(member_name, layer, canvas_wh_or_None, paper_uuid_or_None)]`` in
    display order.  Each entry resolves a page's ``f54`` create op (page UUID in
    field 2, layer in field 3) to that layer's create-info op (top field 1 =
    layer, nested field ``f2.f4`` = paper, ``f2.f8`` = canvas dims).  When the
    event log is absent or carries no canvas dims this returns ``[]`` and the
    caller falls back to the single global canvas/page size.
    """
    if "index.events.pb" not in names:
        return []
    try:
        data = opener("index.events.pb")
    except Exception:
        return []
    try:
        recs = list(read_length_delimited_records(data))
    except Exception:
        return []

    # 1) layer create-info ops: top f1 = layer UUID, f2 = info message.
    layer_info: Dict[str, Tuple[Optional[str], Optional[Tuple[float, float]]]] = {}
    for rec in recs:
        t = parse_message_tolerant(rec)
        if 1 not in t or 2 not in t:
            continue
        layer = _uuid_from(t[1][0])
        info = t[2][0]
        if not layer or not isinstance(info, (bytes, bytearray)):
            continue
        paper: Optional[str] = None
        cxy: Optional[Tuple[float, float]] = None
        for f, _wt, v in iter_fields_tolerant(info):
            if f == 4:
                paper = _uuid_from(v) or paper
            elif f == 8 and isinstance(v, (bytes, bytearray)):
                cf = parse_message_tolerant(v)
                if 1 in cf and 2 in cf:
                    w, h = cf[1][0], cf[2][0]
                    if 100 < w < 5000 and 100 < h < 5000:
                        cxy = (float(w), float(h))
        if cxy:
            layer_info[layer] = (paper, cxy)
        elif paper:
            # paper known but no canvas yet; keep the best (canvas) if seen
            prev = layer_info.get(layer)
            if not prev or not prev[1]:
                layer_info[layer] = (paper, prev[1] if prev else None)

    # 2) pages: f54 create ops (page UUID f2, layer f3, position key f4).
    #    Display order is NOT creation order: each page carries a
    #    fractional-index position key (``f4 = {1: key, 2: {1: version}}``,
    #    e.g. "4mgFG1" < "Am5Pm" < "L2FtP" < "O9t!N"), and moving a page
    #    rewrites it through an f55 update op (``f3`` = new key, same layout).
    #    Pages sort by their latest key, bytewise.
    member_prefix = {p32: m for p32, m in
                     ((m[-36:][:32], m) for m in member_names)}
    created: List[Tuple[str, Optional[str]]] = []  # (page UUID, layer)
    position: Dict[str, Tuple[int, bytes]] = {}    # page -> (version, key)
    for rec in recs:
        t = parse_message_tolerant(rec)
        for op, key_field in ((54, 4), (55, 3)):
            if op not in t or not isinstance(t[op][0], (bytes, bytearray)):
                continue
            body = parse_message_tolerant(t[op][0])
            page = _uuid_from(body.get(2, [None])[0])
            if not page:
                continue
            if op == 54:
                created.append((page, _uuid_from(body.get(3, [None])[0])))
            pk = _position_key(body.get(key_field, [None])[0])
            if pk and pk[0] >= position.get(page, (-1, b""))[0]:
                position[page] = pk
    out: List[Tuple[str, str, Optional[Tuple[float, float]], Optional[str]]] = []
    for page, layer in created:
        member = member_prefix.get(page[:32])
        paper, cxy = layer_info.get(layer, (None, None)) if layer else (None, None)
        out.append((member, layer, cxy, paper))
    # Stable sort: pages without a key keep their creation order at the front.
    order = {page: position.get(page, (0, b""))[1] for page, _l in created}
    keyed = sorted(range(len(out)), key=lambda i: order[created[i][0]])
    return [out[i] for i in keyed]


def _position_key(msg) -> Optional[Tuple[int, bytes]]:
    """Decode a page position ``{1: key bytes, 2: {1: version}}``."""
    if not isinstance(msg, (bytes, bytearray)):
        return None
    f = parse_message_tolerant(bytes(msg))
    key = f.get(1, [None])[0]
    if not isinstance(key, (bytes, bytearray)) or not key:
        return None
    ver = f.get(2, [None])[0]
    version = 0
    if isinstance(ver, (bytes, bytearray)):
        v = parse_message_tolerant(bytes(ver)).get(1, [0])[0]
        version = v if isinstance(v, int) else 0
    return version, bytes(key)


def _collect_attachments(opener, names) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    for n in names:
        if n.startswith("attachments/"):
            data = opener(n)
            if data:
                out[n.split("/")[-1]] = data
    return out


def _read_paper_to_attachment(opener, names) -> Dict[str, str]:
    """Map an *internal* paper UUID to its backing ``attachments/`` UUID.

    Most papers are stored under their own UUID, but some (e.g. imported or
    template papers) have an internal id that differs from the attachment
    filename.  An op whose field #1 is the paper UUID and whose field #6
    sub-message's field #2 is the attachment UUID records that mapping.
    Papers that map to themselves are the common case; the mapping is only
    needed to resolve the rest.
    """
    if "index.events.pb" not in names:
        return {}
    try:
        data = opener("index.events.pb")
    except Exception:
        return {}
    out: Dict[str, str] = {}
    try:
        recs = list(read_length_delimited_records(data))
    except Exception:
        return {}
    for rec in recs:
        t = parse_message_tolerant(rec)
        if 1 not in t or 6 not in t:
            continue
        paper = _uuid_from(t[1][0])
        v6 = t[6][0]
        if not paper or not isinstance(v6, (bytes, bytearray)):
            continue
        sub = parse_message_tolerant(v6)
        if 2 in sub:
            att = _uuid_from(sub[2][0])
            if att:
                out[paper] = att
    return out


def _read_page_size(opener, names) -> Optional[Tuple[float, float]]:
    """Recover the page point size from a paper PDF attachment.

    Paper PDFs are stored in ``attachments/<UUID>`` with no extension; they
    start with ``%PDF`` and carry a ``/MediaBox [0 0 W H]``.  When all paper
    PDFs share the same size we use it; otherwise we fall back to A4.
    """
    import re as _re
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
