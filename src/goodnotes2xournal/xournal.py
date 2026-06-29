"""Write the parsed stroke model to a Xournal++ ``.xopp`` file.

A ``.xopp`` file is gzip-compressed XML. Strokes are encoded as::

    <stroke tool="pen" color="#RRGGBBAA" width="W w1 w2 ...">x1 y1 x2 y2 ...</stroke>

The ``width`` attribute holds the base width followed by one width value per
segment (``N`` points -> ``N-1`` extra values), which Xournal++ uses to render
pressure-varying strokes.
"""

from __future__ import annotations

import gzip
from typing import List, Tuple
from xml.sax.saxutils import escape

from .goodnotes import GoodNotesDocument, Stroke

CREATOR = "goodnotes2xournal"
FILE_VERSION = "4"

# Multiplier applied to GoodNotes' rendered per-point widths. GoodNotes already
# stores absolute widths in points, so 1.0 reproduces them faithfully; the CLI
# ``--width-scale`` flag lets the user thicken/thin everything uniformly.
DEFAULT_WIDTH_SCALE = 1.0

# Floor so a fully-zero-width point still renders a hairline rather than nothing.
_MIN_WIDTH = 0.1


def color_to_hex(color: Tuple[float, float, float, float]) -> str:
    """Convert RGBA floats (0..1) to a Xournal++ ``#RRGGBBAA`` string."""
    r, g, b, a = (max(0.0, min(1.0, c)) for c in color)
    return "#{:02x}{:02x}{:02x}{:02x}".format(
        round(r * 255), round(g * 255), round(b * 255), round(a * 255)
    )


def _stroke_points(stroke: Stroke) -> List[Tuple[float, float, float]]:
    """Return at least two points so single-point dots still render."""
    pts = stroke.points
    if len(pts) == 1:
        x, y, w = pts[0]
        return [(x, y, w), (x + 0.1, y + 0.1, w)]
    return pts


def _width_attr(points, scale: float) -> str:
    """Build the Xournal++ ``width`` attribute.

    Format is ``<nominal> <w0> <w1> ... <w(N-2)>``: a nominal width followed by
    one rendered width per segment (``N`` points -> ``N-1`` segments). We use the
    GoodNotes per-point widths directly (scaled), so pressure tapering and pen
    thickness are preserved.
    """
    widths = [max(_MIN_WIDTH, w * scale) for _x, _y, w in points]
    nominal = max(widths)
    seg_widths = widths[:-1] if len(widths) > 1 else widths
    return " ".join(_fmt(w) for w in [nominal, *seg_widths])


def _fmt(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _stroke_xml(stroke: Stroke, scale: float) -> str:
    points = _stroke_points(stroke)
    color = color_to_hex(stroke.color)
    coords = " ".join(f"{_fmt(x)} {_fmt(y)}" for x, y, _w in points)
    width = _width_attr(points, scale)
    return f'<stroke tool="pen" color="{color}" width="{width}">{coords}</stroke>'


def build_xml(doc: GoodNotesDocument, width_scale: float = DEFAULT_WIDTH_SCALE) -> str:
    """Render the document model to Xournal++ XML."""
    lines = [
        '<?xml version="1.0" standalone="no"?>',
        f'<xournal creator="{CREATOR}" fileversion="{FILE_VERSION}">',
        f"<title>{escape(doc.title)}</title>",
    ]
    for page in doc.pages:
        lines.append(f'<page width="{_fmt(page.width)}" height="{_fmt(page.height)}">')
        lines.append('<background type="solid" color="white" style="plain"/>')
        lines.append("<layer>")
        for stroke in page.strokes:
            lines.append(_stroke_xml(stroke, width_scale))
        lines.append("</layer>")
        lines.append("</page>")
    lines.append("</xournal>")
    return "\n".join(lines) + "\n"


def write_xopp(doc: GoodNotesDocument, path: str,
               width_scale: float = DEFAULT_WIDTH_SCALE) -> None:
    """Write the document to a gzip-compressed ``.xopp`` file."""
    xml = build_xml(doc, width_scale)
    with gzip.open(path, "wb") as fh:
        fh.write(xml.encode("utf-8"))
