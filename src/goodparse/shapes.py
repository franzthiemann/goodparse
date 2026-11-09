"""Vector shapes: decode GoodNotes shape geometry into sampled outlines.

A shape-recognised stroke keeps its geometry in ``f7.f9`` and a shape *fill*
keeps a copy of the same geometry in ``f9.f4``.  Both use one message whose
populated field selects the shape type (coordinates in canvas units, every
point an ``{1: x, 2: y}`` float32 message):

======  ===================================================================
field   geometry
======  ===================================================================
``f1``  polygon / polyline: repeated points (closed when last == first)
``f2``  quadratic Bézier: ``{1: start, 2: control, 3: end}``
``f3``  rectangle: ``{1: centre, 2: (width, height), 3: rotation (rad)}``
``f4``  ellipse: ``{1: centre, 2: (semi-axis x, semi-axis y), 3: rotation}``
======  ===================================================================

Verified against the reference exports: the Test8 square (``f3``) and circle
(``f4``) land at the reference size and position, and the Test_11 "line"
(``f2``) sags exactly as a quadratic Bézier through its control point would
(apex 547.6 predicted vs. 547.5 measured, canvas units).  ``f15`` beside the
geometry is the outline width in canvas units.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .protobuf import parse_message_tolerant

Point = Tuple[float, float]

_ELLIPSE_STEPS = 72
_BEZIER_STEPS = 32


@dataclass
class Outline:
    points: List[Point]
    closed: bool


def _point(v) -> Optional[Point]:
    if not isinstance(v, (bytes, bytearray)):
        return None
    f = parse_message_tolerant(bytes(v))
    x, y = f.get(1, [None])[0], f.get(2, [None])[0]
    if isinstance(x, float) and isinstance(y, float) and math.isfinite(x) and math.isfinite(y):
        return (x, y)
    return None


def _rotation(f) -> float:
    r = f.get(3, [0.0])[0]
    return r if isinstance(r, float) and math.isfinite(r) else 0.0


def _transform(local: List[Point], centre: Point, rot: float) -> List[Point]:
    ca, sa = math.cos(rot), math.sin(rot)
    cx, cy = centre
    return [(cx + x * ca - y * sa, cy + x * sa + y * ca) for x, y in local]


def _polygon(msg: bytes) -> Optional[Outline]:
    pts = [p for p in (_point(v) for v in parse_message_tolerant(msg).get(1, [])) if p]
    if len(pts) < 2:
        return None
    closed = len(pts) > 2 and math.dist(pts[0], pts[-1]) < 1e-3
    return Outline(pts, closed)


def _bezier(msg: bytes) -> Optional[Outline]:
    f = parse_message_tolerant(msg)
    p0, p1, p2 = (_point(f.get(k, [None])[0]) for k in (1, 2, 3))
    if not (p0 and p1 and p2):
        return None
    pts = []
    for i in range(_BEZIER_STEPS + 1):
        t = i / _BEZIER_STEPS
        a, b, c = (1 - t) ** 2, 2 * (1 - t) * t, t * t
        pts.append((a * p0[0] + b * p1[0] + c * p2[0], a * p0[1] + b * p1[1] + c * p2[1]))
    return Outline(pts, False)


def _rectangle(msg: bytes) -> Optional[Outline]:
    f = parse_message_tolerant(msg)
    centre, size = _point(f.get(1, [None])[0]), _point(f.get(2, [None])[0])
    if not (centre and size):
        return None
    hw, hh = size[0] / 2, size[1] / 2
    local = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh), (-hw, -hh)]
    return Outline(_transform(local, centre, _rotation(f)), True)


def _ellipse(msg: bytes) -> Optional[Outline]:
    f = parse_message_tolerant(msg)
    centre, axes = _point(f.get(1, [None])[0]), _point(f.get(2, [None])[0])
    if not (centre and axes):
        return None
    rx, ry = axes
    local = [(rx * math.cos(2 * math.pi * i / _ELLIPSE_STEPS),
              ry * math.sin(2 * math.pi * i / _ELLIPSE_STEPS))
             for i in range(_ELLIPSE_STEPS + 1)]
    return Outline(_transform(local, centre, _rotation(f)), True)


_DECODERS = {1: _polygon, 2: _bezier, 3: _rectangle, 4: _ellipse}


def decode_outline(geometry: bytes) -> Optional[Outline]:
    """Decode a shape geometry message (``f7.f9`` or ``f9.f4``)."""
    f = parse_message_tolerant(geometry)
    for field_no, decode in _DECODERS.items():
        v = f.get(field_no, [None])[0]
        if isinstance(v, (bytes, bytearray)):
            out = decode(bytes(v))
            if out:
                return out
    return None


def outline_width(geometry: bytes) -> Optional[float]:
    """Outline width (canvas units) from the geometry's ``f15`` float."""
    w = parse_message_tolerant(geometry).get(15, [None])[0]
    return w if isinstance(w, float) and 0.05 < w < 200.0 else None
