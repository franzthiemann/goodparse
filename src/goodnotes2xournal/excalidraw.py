"""Write the parsed stroke model to an Excalidraw ``.excalidraw`` file.

An ``.excalidraw`` file is a JSON scene::

    {"type": "excalidraw", "version": 2, "elements": [...], "appState": {...}}

Each GoodNotes stroke becomes one ``freedraw`` element. Excalidraw renders
``freedraw`` with the perfect-freehand algorithm: ``strokeWidth`` sets the base
size and the per-point ``pressures`` array (0..1, ``simulatePressure`` off)
modulates it, so GoodNotes' baked per-point widths are mapped to the stroke's
maximum width plus normalised pressures. This preserves thin/thick pens and
pressure tapering, though the rendered thickness is an approximation of the
original (Excalidraw's freehand renderer applies its own thinning curve).

Excalidraw has a single infinite canvas, so pages are laid out top-to-bottom
with a gap, each framed by a locked light-grey rectangle marking the page
bounds. Colour alpha maps to element ``opacity``. All ids/seeds are derived
deterministically from element order so conversion is reproducible.
"""

from __future__ import annotations

import json
from typing import List, Tuple

from .goodnotes import GoodNotesDocument, Stroke

SCENE_SOURCE = "goodnotes2xournal"

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


def _freedraw_element(stroke: Stroke, element_id: str, index: int,
                      offset_y: float, scale: float) -> dict:
    points = _stroke_points(stroke)
    widths = [max(_MIN_WIDTH, w * scale) for _x, _y, w in points]
    nominal = max(widths)

    xs = [p[0] for p in points]
    ys = [p[1] + offset_y for p in points]
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
        "strokeWidth": round(nominal, 4),
        "opacity": _opacity(stroke.color),
        "points": rel,
        "pressures": [round(w / nominal, 4) for w in widths],
        "simulatePressure": False,
        "lastCommittedPoint": rel[-1],
    })
    return el


def build_scene(doc: GoodNotesDocument, width_scale: float = 1.0) -> dict:
    """Render the document model to an Excalidraw scene dict."""
    elements = []
    offset_y = 0.0
    for page_index, page in enumerate(doc.pages):
        elements.append(_page_frame(page_index, len(elements), offset_y,
                                    page.width, page.height))
        for stroke_index, stroke in enumerate(page.strokes):
            elements.append(_freedraw_element(
                stroke, f"p{page_index + 1}-s{stroke_index + 1}",
                len(elements), offset_y, width_scale,
            ))
        offset_y += page.height + PAGE_GAP
    return {
        "type": "excalidraw",
        "version": 2,
        "source": SCENE_SOURCE,
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }


def write_excalidraw(doc: GoodNotesDocument, path: str,
                     width_scale: float = 1.0) -> None:
    """Write the document to an ``.excalidraw`` JSON file."""
    scene = build_scene(doc, width_scale)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(scene, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
