"""Parse GoodNotes ``.goodnotes`` archives into a simple stroke model.

Format (reverse-engineered; see README for the full write-up):

* A ``.goodnotes`` file is a ZIP archive. Each ``notes/<UUID>`` member is one
  page, stored as length-delimited protobuf records. Records alternate between a
  small metadata record and a content record whose field ``#7`` carries a stroke.
* In a stroke's ``#7`` message: field ``#2`` is the Apple-LZ4 geometry blob and
  field ``#4`` is the RGBA colour (float32 sub-fields ``1=R 2=G 3=B 4=A``).
* The decompressed geometry holds float32 ``(x, y, pressure)`` points at stride
  12, in PDF points. Pages are A4 (595.28 x 841.89 pt).
"""

from __future__ import annotations

import os
import struct
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .applelz4 import apple_decompress, is_apple_lz4
from .protobuf import (
    iter_fields,
    parse_message,
    read_length_delimited_records,
)

# A4 in PDF points (72 dpi) — matches GoodNotes' coordinate space and Xournal++.
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


@dataclass
class Stroke:
    """One pen stroke: a polyline with per-point width and an RGBA colour.

    The third component of each point is the *rendered* width in PDF points:
    GoodNotes bakes pen pressure into the width, so thin pressure-varying pens
    give values around 0.7-1.4 while a thick fixed pen gives a larger constant.
    """

    points: List[Tuple[float, float, float]]  # (x, y, width)
    color: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)  # RGBA 0..1

    @property
    def is_dot(self) -> bool:
        return len(self.points) <= 1 or _bbox_diagonal(self.points) < 1.0


@dataclass
class Page:
    strokes: List[Stroke] = field(default_factory=list)
    width: float = A4_WIDTH_PT
    height: float = A4_HEIGHT_PT


@dataclass
class GoodNotesDocument:
    pages: List[Page] = field(default_factory=list)
    title: str = "GoodNotes"


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
    """A point is float32 ``(x, y, width)``; gate on the coordinates only.

    Width is *not* range-checked against [0,1]: thick pens store widths above 1,
    so an earlier pressure-style gate wrongly dropped those whole strokes.
    """
    if o + _POINT_STRIDE > len(buf):
        return False
    x, y, w = _f32(buf, o), _f32(buf, o + 4), _f32(buf, o + 8)
    return (_COORD_MIN < x < _COORD_MAX and _COORD_MIN < y < _COORD_MAX
            and 0.0 <= w < _WIDTH_MAX)


def extract_points(raw: bytes) -> List[Tuple[float, float, float]]:
    """Extract the stroke path from decompressed geometry.

    Each point is a float32 ``(x, y, width)`` triplet at stride 12. The buffer
    holds three run regions: the rendered path first (near the style template),
    then small/high-resolution trailer runs much later whose coordinates jitter.
    We therefore take the *earliest* run of >= 2 valid triplets at offset >= 64,
    not the longest, and ignore the trailer.
    """
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
                return [
                    (_f32(raw, start + i * _POINT_STRIDE),
                     _f32(raw, start + i * _POINT_STRIDE + 4),
                     _f32(raw, start + i * _POINT_STRIDE + 8))
                    for i in range(count)
                ]
            # too short to be the path (stray template float) -> keep scanning
            o = start + _POINT_STRIDE
        else:
            o += 1
    return []


def _extract_color(content_fields) -> Tuple[float, float, float, float]:
    """Read the RGBA colour from field ``#4`` (float32 sub-fields 1..4)."""
    color = [0.0, 0.0, 0.0, 1.0]
    blobs = content_fields.get(4)
    if not blobs:
        return tuple(color)  # default black
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
# Page / archive parsing
# --------------------------------------------------------------------------- #

def parse_page(data: bytes) -> Page:
    """Parse one ``notes/<UUID>`` page file into a :class:`Page`."""
    page = Page()
    for record in read_length_delimited_records(data):
        fields = parse_message(record)
        # Stroke content lives in field #7 of the content record.
        for content in fields.get(7, []):
            if not isinstance(content, (bytes, bytearray)):
                continue
            cfields = parse_message(content)
            blob = _find_geometry_blob(cfields)
            if blob is None:
                continue
            points = extract_points(apple_decompress(blob))
            if not points:
                continue
            page.strokes.append(Stroke(points=points, color=_extract_color(cfields)))
    return page


def _read_page_members(opener, names) -> List[Page]:
    pages = []
    for name in sorted(n for n in names if "/notes/" in n or n.startswith("notes/")):
        data = opener(name)
        if not data:
            continue
        page = parse_page(data)
        if page.strokes:
            pages.append(page)
    return pages


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

        pages = _read_page_members(opener, names)
    else:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            pages = _read_page_members(lambda n: zf.read(n), names)

    return GoodNotesDocument(pages=pages, title=title)
