"""Write the parsed stroke model to an Excalidraw ``.excalidraw`` file.

An ``.excalidraw`` file is a JSON scene::

    {"type": "excalidraw", "version": 2, "elements": [...], "appState": {...}}

Each GoodNotes stroke becomes one ``freedraw`` element. Excalidraw renders
``freedraw`` with the perfect-freehand algorithm (``size = 4.25 *
strokeWidth``, thinning 0.6, sine easing), so a point of pressure ``p`` is
drawn ``8.5 * strokeWidth * sin(pi/2 * (0.2 + 0.6 p))`` wide — measured in
excalidraw.com: 8x / 6x / 4.5x ``strokeWidth`` at p = 1 / 0.5 / 0.25.  The
writer inverts that: ``strokeWidth`` makes the widest point p = 1 and every
other point gets the pressure that reproduces its width.  Widths below 0.32x
the stroke's maximum (p = 0) are drawn at that floor.

Excalidraw has a single infinite canvas, so pages are laid out top-to-bottom
with a gap, each framed by a locked light-grey rectangle marking the page
bounds. Colour alpha maps to element ``opacity``. All ids/seeds are derived
deterministically from element order so conversion is reproducible.

Text becomes ``text`` elements, placed by the same layout as the PDF output
(:mod:`goodparse.textlayout`).  An Excalidraw text element has one font,
size and colour, so consecutive lines of one style form one multi-line
element (its ``lineHeight`` is the line pitch), and a line mixing styles is
split into one element per styled span.  Excalidraw has no bold, italic or
serif text: runs use its Helvetica (Cascadia for monospaced families);
underline and strikethrough become thin ``line`` elements.  A text box's
background colour becomes a filled rectangle beneath the ink.

Photos and stickers become ``image`` elements, and each page's paper a
locked image beneath everything (Excalidraw only shows rasters: paper is
rendered at 144 dpi as JPEG, vector stickers at 300 dpi as PNG); the
image data lives once per distinct file in the scene's ``files`` map.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from typing import Dict, List, Optional, Tuple

from .base14_widths import base14_class
from .model import GoodNotesDocument, Stroke, TextBox
from .raster import Raster, image_raster, paper_raster
from .textlayout import PlacedLine, Segment, base14_width, layout_text

SCENE_SOURCE = "goodparse"

# Vertical gap between consecutive pages on the canvas, in points/pixels.
PAGE_GAP = 40.0

# Colour of the locked rectangle outlining each page.
_PAGE_FRAME_COLOR = "#ced4da"

# Floor mirroring xournal.py: a zero-width point still gets a visible stroke.
_MIN_WIDTH = 0.1


def color_to_rgb_hex(color: Tuple[float, float, float, float]) -> str:
    """Convert RGBA floats (0..1) to Excalidraw's ``#rrggbb`` (alpha separate)."""
    r, g, b, _a = (max(0.0, min(1.0, c)) for c in color)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def _opacity(color: Tuple[float, float, float, float]) -> float:
    return round(max(0.0, min(1.0, color[3])) * 100)


def _seed(index: int) -> int:
    # Deterministic stand-in for Excalidraw's random seed (Knuth hash, 31-bit).
    return (index + 1) * 2654435761 % 2147483647 + 1


def _base_element(element_id: str, index: int) -> dict:
    """Fields common to every element, with deterministic ids/seeds."""
    return {
        "id": element_id,
        "angle": 0,
        "fillStyle": "solid",
        "strokeStyle": "solid",
        "roughness": 0,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": _seed(index),
        "version": 1,
        "versionNonce": _seed(index + 7919),
        "isDeleted": False,
        "boundElements": None,
        "updated": 0,
        "link": None,
        "locked": False,
    }


def _page_frame(page_index: int, index: int, offset_y: float,
                width: float, height: float) -> dict:
    el = _base_element(f"page-{page_index + 1}", index)
    el.update({
        "type": "rectangle",
        "x": 0.0,
        "y": offset_y,
        "width": width,
        "height": height,
        "strokeColor": _PAGE_FRAME_COLOR,
        "backgroundColor": "transparent",
        "strokeWidth": 1,
        "locked": True,
    })
    return el


def _stroke_points(stroke: Stroke) -> List[Tuple[float, float, float]]:
    """Return at least two points so single-point dots still render."""
    pts = stroke.points
    if len(pts) == 1:
        x, y, w = pts[0]
        return [(x, y, w), (x + 0.1, y + 0.1, w)]
    return pts


def _page_xy(stroke: Stroke, offset_y: float, page_scale: float):
    """Stroke points in scene coordinates: canvas -> page, then page offset."""
    points = _stroke_points(stroke)
    xs = [p[0] * page_scale for p in points]
    ys = [p[1] * page_scale + offset_y for p in points]
    return points, xs, ys


def _fill_element(stroke: Stroke, element_id: str, index: int,
                  offset_y: float, page_scale: float) -> dict:
    """A closed, stroke-less ``line`` polygon carrying a shape's fill.

    Excalidraw has a single opacity per element, so the translucent fill is
    its own element beneath the (opaque) outline.
    """
    assert stroke.fill is not None
    _points, xs, ys = _page_xy(stroke, offset_y, page_scale)
    x0, y0 = min(xs), min(ys)
    rel = [[round(x - x0, 4), round(y - y0, 4)] for x, y in zip(xs, ys)]
    rel.append(rel[0])
    el = _base_element(element_id, index)
    el.update({
        "type": "line",
        "x": x0,
        "y": y0,
        "width": max(xs) - x0,
        "height": max(ys) - y0,
        "strokeColor": "transparent",
        "backgroundColor": color_to_rgb_hex(stroke.fill),
        "strokeWidth": 1,
        "opacity": _opacity(stroke.fill),
        "points": rel,
        "lastCommittedPoint": None,
        "startBinding": None,
        "endBinding": None,
        "startArrowhead": None,
        "endArrowhead": None,
    })
    return el


# Excalidraw's freedraw rendering (see the module docstring).
_FREEHAND_DIAMETER = 2 * 4.25
_THINNING = 0.6


def _freehand_width(stroke_width: float, pressure: float) -> float:
    return _FREEHAND_DIAMETER * stroke_width * math.sin(
        math.pi / 2 * (0.5 - _THINNING * (0.5 - pressure)))


def _freehand_pressure(width: float, stroke_width: float) -> float:
    """The pressure Excalidraw draws ``width`` wide at ``stroke_width``."""
    ratio = min(1.0, width / (_FREEHAND_DIAMETER * stroke_width))
    p = 0.5 - (0.5 - 2 / math.pi * math.asin(ratio)) / _THINNING
    return max(0.0, min(1.0, p))


def _freedraw_element(stroke: Stroke, element_id: str, index: int,
                      offset_y: float, width_scale: float,
                      page_scale: float = 1.0) -> dict:
    points, xs, ys = _page_xy(stroke, offset_y, page_scale)
    widths = [max(_MIN_WIDTH, w * page_scale * width_scale) for _x, _y, w in points]
    stroke_width = max(widths) / _freehand_width(1.0, 1.0)

    x0, y0 = min(xs), min(ys)
    rel = [[round(x - x0, 4), round(y - y0, 4)] for x, y in zip(xs, ys)]

    el = _base_element(element_id, index)
    el.update({
        "type": "freedraw",
        "x": x0,
        "y": y0,
        "width": max(xs) - x0,
        "height": max(ys) - y0,
        "strokeColor": color_to_rgb_hex(stroke.color),
        "backgroundColor": "transparent",
        "strokeWidth": round(stroke_width, 4),
        "opacity": _opacity(stroke.color),
        "points": rel,
        "pressures": [round(_freehand_pressure(w, stroke_width), 4) for w in widths],
        "simulatePressure": False,
        "lastCommittedPoint": rel[-1],
    })
    return el


# Excalidraw font families and their metrics (FONT_METRICS in Excalidraw's
# source: units per em, ascender, descender, default line height), used to
# put the first baseline where the layout wants it.
_HELVETICA, _CASCADIA = 2, 3
_FONT_METRICS = {_HELVETICA: (2048, 1577, -471, 1.15),
                 _CASCADIA: (2048, 1900, -480, 1.2)}


def _font_family(run) -> int:
    return _CASCADIA if base14_class(run.font_family) == "Courier" else _HELVETICA


def _baseline_offset(family: int, size: float, line_height: float) -> float:
    """Distance from a text element's top to its first baseline, as
    Excalidraw renders it (``getVerticalOffset``): the ascent plus half the
    gap between the line box and the font's ascent + descent."""
    upm, asc, desc, _lh = _FONT_METRICS[family]
    em = size / upm
    return em * asc + (size * line_height - em * (asc - desc)) / 2.0


def _style(sg: Segment) -> tuple:
    return (_font_family(sg.run), round(sg.size, 3), tuple(sg.run.color))


def _text_element(element_id: str, index: int, lines: List[str], x: float,
                  baseline: float, width: float, sg: Segment, pitch: float,
                  align: str) -> dict:
    family = _font_family(sg.run)
    line_height = pitch / sg.size if len(lines) > 1 and pitch > 0 \
        else _FONT_METRICS[family][3]
    text = "\n".join(lines)
    el = _base_element(element_id, index)
    el.update({
        "type": "text",
        "x": x,
        "y": baseline - _baseline_offset(family, sg.size, line_height),
        "width": width,
        "height": sg.size * line_height * len(lines),
        "strokeColor": color_to_rgb_hex(sg.run.color),
        "backgroundColor": "transparent",
        "strokeWidth": 1,
        "opacity": _opacity(sg.run.color),
        "text": text,
        "originalText": text,
        "fontSize": round(sg.size, 4),
        "fontFamily": family,
        "textAlign": align if align in ("center", "right") else "left",
        "verticalAlign": "top",
        "containerId": None,
        "lineHeight": round(line_height, 6),
        "autoResize": True,
    })
    return el


def _rule_element(element_id: str, index: int, x0: float, x1: float, y: float,
                  sg: Segment) -> dict:
    """A thin horizontal ``line`` (underline / strikethrough)."""
    el = _base_element(element_id, index)
    el.update({
        "type": "line",
        "x": x0,
        "y": y,
        "width": x1 - x0,
        "height": 0,
        "strokeColor": color_to_rgb_hex(sg.run.color),
        "backgroundColor": "transparent",
        "strokeWidth": round(max(0.5, sg.size * 0.05), 4),
        "opacity": _opacity(sg.run.color),
        "points": [[0, 0], [round(x1 - x0, 4), 0]],
        "lastCommittedPoint": None,
        "startBinding": None,
        "endBinding": None,
        "startArrowhead": None,
        "endArrowhead": None,
    })
    return el


def _spans(line: PlacedLine) -> List[List[Segment]]:
    """The line's word pieces grouped into runs of one style (whitespace-only
    pieces join the current span); spans without visible text are dropped."""
    spans: List[List[Segment]] = []
    for sg in line.segments:
        if spans and (not sg.text.strip() or _style(sg) == _style(spans[-1][0])):
            spans[-1].append(sg)
        else:
            spans.append([sg])
    return [sp for sp in spans if any(sg.text.strip() for sg in sp)]


def _ink(span: List[Segment]) -> Tuple[float, float, str]:
    """``(x, width, text)`` of a span without surrounding whitespace."""
    text = "".join(sg.text for sg in span)
    first = span[0]
    lead = text[:len(text) - len(text.lstrip())]
    x = first.x + base14_width(lead, first.run, first.size)
    return x, base14_width(text.strip(), first.run, first.size), text.strip()


def _text_elements(t: TextBox, prefix: str, start: int, offset_y: float,
                   page_scale: float) -> List[dict]:
    """Elements for one text box (see the module docstring)."""
    box_x, box_w = t.position[0] * page_scale, t.size[0] * page_scale
    out: List[dict] = []

    def add(make, *args) -> None:
        out.append(make(f"{prefix}-{len(out) + 1}", start + len(out), *args))

    # Blocks: runs of consecutive lines that are each one span of the same
    # style and alignment at a constant pitch; any other line stands alone.
    blocks: List[List[Tuple[PlacedLine, List[List[Segment]]]]] = []
    prev = None
    for line in layout_text(t, page_scale):
        spans = _spans(line)
        if not spans:
            prev = None
            continue
        joins = (prev is not None and len(spans) == 1 and len(prev[1]) == 1
                 and _style(spans[0][0]) == _style(prev[1][0][0])
                 and line.align == prev[0].align
                 and abs(line.pitch - prev[0].pitch) < 1e-6
                 and abs(line.baseline - prev[0].baseline - prev[0].pitch) < 1e-6)
        if joins:
            blocks[-1].append((line, spans))
        else:
            blocks.append([(line, spans)])
        prev = (line, spans)

    for block in blocks:
        line0, spans0 = block[0]
        baseline = line0.baseline + offset_y
        if len(spans0) == 1:
            inks = [_ink(spans[0]) for _line, spans in block]
            width = max(x + w for x, w, _t in inks) - min(x for x, _w, _t in inks)
            if line0.align == "center" and box_w > 0:
                x = box_x + (box_w - width) / 2.0
            elif line0.align == "right" and box_w > 0:
                x = box_x + box_w - width
            else:
                x = min(x for x, _w, _t in inks)
            add(_text_element, [txt for _x, _w, txt in inks], x, baseline, width,
                spans0[0][0], line0.pitch, line0.align)
        else:
            for span in spans0:
                x, w, txt = _ink(span)
                add(_text_element, [txt], x, baseline, w, span[0], 0.0, "left")
        # Underline / strikethrough per word piece, as in the PDF output.
        for line, _line_spans in block:
            for sg in line.segments:
                for flag, dy in ((sg.run.underline, 0.12), (sg.run.strike, -0.33)):
                    if flag and sg.text.strip():
                        w = base14_width(sg.text.rstrip(), sg.run, sg.size)
                        add(_rule_element, sg.x, sg.x + w,
                            line.baseline + offset_y + sg.size * dy, sg)
    return out


def _background_element(t: TextBox, element_id: str, index: int, offset_y: float,
                        page_scale: float) -> dict:
    """A text box's fill colour as a stroke-less filled rectangle."""
    assert t.background is not None
    el = _base_element(element_id, index)
    el.update({
        "type": "rectangle",
        "x": t.position[0] * page_scale,
        "y": t.position[1] * page_scale + offset_y,
        "width": t.size[0] * page_scale,
        "height": t.size[1] * page_scale,
        "strokeColor": "transparent",
        "backgroundColor": color_to_rgb_hex(t.background),
        "strokeWidth": 1,
        "opacity": _opacity(t.background),
    })
    return el


def _image_element(element_id: str, index: int, x: float, y: float,
                   width: float, height: float, file_id: str,
                   locked: bool = False) -> dict:
    el = _base_element(element_id, index)
    el.update({
        "type": "image",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "strokeColor": "transparent",
        "backgroundColor": "transparent",
        "strokeWidth": 1,
        "fileId": file_id,
        "status": "saved",
        "scale": [1, 1],
        "crop": None,
        "locked": locked,
    })
    return el


class _Files:
    """The scene's ``files`` map: each distinct raster stored once, keyed by
    its content hash (Excalidraw's own file ids are content hashes too)."""

    def __init__(self) -> None:
        self.files: Dict[str, dict] = {}

    def add(self, raster: Raster) -> str:
        file_id = hashlib.sha1(raster.data).hexdigest()
        if file_id not in self.files:
            self.files[file_id] = {
                "mimeType": raster.mime,
                "id": file_id,
                "dataURL": f"data:{raster.mime};base64,"
                           + base64.b64encode(raster.data).decode("ascii"),
                "created": 0,
                "lastRetrieved": 0,
            }
        return file_id


def build_scene(doc: GoodNotesDocument, width_scale: float = 1.0) -> dict:
    """Render the document model to an Excalidraw scene dict.

    Per page, bottom to top: the paper (a locked image, rendered like the PDF
    writer's backgrounds), the page frame, photos and stickers, text-box
    backgrounds, strokes, text.
    """
    elements = []
    files = _Files()
    papers: Dict[Tuple[str, int], Optional[str]] = {}
    offset_y = 0.0
    for page_index, page in enumerate(doc.pages):
        pid = f"p{page_index + 1}"
        if page.background:
            key = (hashlib.sha1(page.background).hexdigest(), page.background_page)
            if key not in papers:
                raster = paper_raster(page.background, page.background_page)
                papers[key] = files.add(raster) if raster else None
            if papers[key]:
                elements.append(_image_element(f"{pid}-paper", len(elements), 0.0, offset_y,
                                               page.width, page.height, papers[key],
                                               locked=True))
        elements.append(_page_frame(page_index, len(elements), offset_y,
                                    page.width, page.height))
        for image_index, image in enumerate(page.images):
            raster = image_raster(image, page.scale)
            if raster is not None:
                elements.append(_image_element(
                    f"{pid}-i{image_index + 1}", len(elements),
                    image.position[0] * page.scale, image.position[1] * page.scale + offset_y,
                    image.size[0] * page.scale, image.size[1] * page.scale, files.add(raster)))
        for text_index, text in enumerate(page.texts):
            if text.background is not None:
                elements.append(_background_element(
                    text, f"{pid}-t{text_index + 1}-bg", len(elements),
                    offset_y, page.scale))
        for stroke_index, stroke in enumerate(page.strokes):
            sid = f"{pid}-s{stroke_index + 1}"
            if stroke.fill is not None:
                elements.append(_fill_element(
                    stroke, sid + "-fill", len(elements), offset_y, page.scale))
            elements.append(_freedraw_element(
                stroke, sid, len(elements), offset_y, width_scale, page.scale))
        for text_index, text in enumerate(page.texts):
            elements.extend(_text_elements(
                text, f"{pid}-t{text_index + 1}", len(elements),
                offset_y, page.scale))
        offset_y += page.height + PAGE_GAP
    return {
        "type": "excalidraw",
        "version": 2,
        "source": SCENE_SOURCE,
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": files.files,
    }


def write_excalidraw(doc: GoodNotesDocument, path: str,
                     width_scale: float = 1.0) -> None:
    """Write the document to an ``.excalidraw`` JSON file."""
    scene = build_scene(doc, width_scale)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(scene, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
