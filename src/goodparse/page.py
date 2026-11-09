"""Turn a page file's live objects into the drawing model.

See :mod:`goodparse.records` for the object log, :mod:`goodparse.ink` for
stroke paths, :mod:`goodparse.shapes` for vector shapes and fills, and
:mod:`goodparse.richtext` / :mod:`goodparse.text_rtf` for text.
"""

from __future__ import annotations

import warnings
from typing import Dict, List, Optional, Tuple

from .applelz4 import apple_decompress, is_apple_lz4
from .ink import TemplateError, decode_ink
from .model import Image, Page, Stroke, TextBox
from .protobuf import get_bytes, iter_fields_tolerant, parse_message_tolerant
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
from .text_rtf import parse_rtf_runs

# Shape outline width (canvas units) when f9.f15 is absent: the value every
# shape in the samples carries.
_SHAPE_DEFAULT_WIDTH = 10.3937

# GoodNotes draws pencil as a grain texture, not flat ink.  Flat-colour output
# approximates its average coverage with this opacity: a single pencil pass
# covers 4-8 % of a solid stroke in the references (Test8, Test_11), dense
# self-overlapping scribbles 30-40 % (Test6), because the texture accumulates.
PENCIL_OPACITY = 0.2


def _stroke_origin(content_fields) -> Optional[Tuple[float, float]]:
    """Read an object's translation from field ``#6`` (``{1: dx, 2: dy}``).

    Moving a stroke or shape in GoodNotes does not rewrite its geometry; it
    stores the move as this translation, which is *added* to every stored
    point (Test_11 "Moved" page).  Objects never moved carry an empty ``#6``.
    """
    return _point_xy(content_fields.get(6, [None])[0])


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


def _point_xy(msg) -> Optional[Tuple[float, float]]:
    """Parse a point message ``{1: f32 x, 2: f32 y}``; ``None`` otherwise."""
    if not isinstance(msg, (bytes, bytearray)):
        return None
    f = parse_message_tolerant(bytes(msg))
    x, y = f.get(1, [None])[0], f.get(2, [None])[0]
    if isinstance(x, float) and isinstance(y, float):
        return x, y
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


def _parse_stroke(cf) -> List[Stroke]:
    """Build the stroke(s) of a kind-7 object (one per sub-path).

    A shape-recognised stroke keeps its exact geometry in ``f9`` (its ``f2``
    blob is then a bare style template); a freehand stroke has an empty
    ``f9`` and its path in the ``f2`` blob (see :mod:`goodparse.ink`).  Either
    way the ``f6`` translation of a moved object is added to every point.
    """
    color = _extract_color(cf)
    dx, dy = _stroke_origin(cf) or (0.0, 0.0)

    f9 = cf.get(9, [b""])[0]
    if isinstance(f9, (bytes, bytearray)) and f9:
        outline = decode_outline(bytes(f9))
        if outline:
            w = outline_width(bytes(f9)) or _SHAPE_DEFAULT_WIDTH
            return [Stroke(points=[(x + dx, y + dy, w) for x, y in outline.points],
                           color=color, kind="pen")]

    blob = _find_geometry_blob(cf)
    if blob is None:
        return []
    try:
        ink = decode_ink(apple_decompress(blob))
    except TemplateError as exc:
        warnings.warn(f"skipping undecodable stroke: {exc}")
        return []
    if ink is None:
        return []
    kind = "pen"
    if ink.kind == "pencil":
        kind = "pencil"
        color = (color[0], color[1], color[2], color[3] * PENCIL_OPACITY)
    return [Stroke(points=[(x + dx, y + dy, w) for x, y, w in path],
                   color=color, kind=kind)
            for path in ink.subpaths]


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
            strokes = _parse_stroke(obj.fields)
            page.strokes.extend(strokes)
            if strokes:
                by_id[obj.id] = strokes[0]
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
    data = next((attachments[a] for a in obj.attachments if a in attachments), None)
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
