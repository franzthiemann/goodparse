"""Write the parsed stroke model to a Xournal++ ``.xopp`` file.

A ``.xopp`` file is gzip-compressed XML (Xournal++ file version 4).  The
element types used:

* ``<stroke tool="pen" color="#RRGGBBAA" width="...">x1 y1 x2 y2 ...</stroke>``
  for pen, highlighter and eraser strokes.  ``width`` is the base width
  followed by one width per segment (N points -> N-1 extra values), which
  Xournal++ uses to render pressure-varying strokes.
* ``<image left top right bottom naturalSize="w h">BASE64</image>`` for
  embedded rasters (BASE64 is the raw PNG/JPEG, no data-URI prefix).
* ``<text font size color x y wrap align>TEXT</text>`` for text boxes.

Both GoodNotes and Xournal++ use a *top-left* origin with +y downward, so
coordinates need no flip — only the canvas->page uniform scale is applied
(see :class:`~goodparse.goodnotes.Page.scale`).  Page size comes from the
embedded paper PDF when present, else A4.
"""

from __future__ import annotations

import base64
import gzip
import re
import struct
from typing import List, Tuple
from xml.sax.saxutils import escape

from .goodnotes import (
    GoodNotesDocument,
    Image,
    Page,
    Stroke,
    TextBox,
)

CREATOR = "goodparse"
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


def _fmt(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _stroke_points(stroke: Stroke, scale: float) -> List[Tuple[float, float, float]]:
    """Return at least two scaled points so single-point dots still render."""
    pts = [(x * scale, y * scale, w) for x, y, w in stroke.points]
    if len(pts) == 1:
        x, y, w = pts[0]
        return [(x, y, w), (x + 0.1, y + 0.1, w)]
    return pts


def _width_attr(points, scale: float) -> str:
    """Build the Xournal++ ``width`` attribute.

    Format is ``<nominal> <w0> <w1> ... <w(N-2)>``: a nominal width followed by
    one rendered width per segment (``N`` points -> ``N-1`` segments).
    """
    widths = [max(_MIN_WIDTH, w * scale) for _x, _y, w in points]
    nominal = max(widths)
    seg_widths = widths[:-1] if len(widths) > 1 else widths
    return " ".join(_fmt(w) for w in [nominal, *seg_widths])


def _stroke_xml(stroke: Stroke, scale: float, width_scale: float) -> str:
    points = _stroke_points(stroke, scale)
    color = color_to_hex(stroke.color)
    coords = " ".join(f"{_fmt(x)} {_fmt(y)}" for x, y, _w in points)
    width = _width_attr(points, width_scale)
    return f'<stroke tool="pen" color="{color}" width="{width}">{coords}</stroke>'


def _natural_size(data: bytes) -> Tuple[int, int]:
    """Return a raster's intrinsic pixel (width, height) from its header.

    PNG: the IHDR chunk (bytes 16..24).  JPEG: walk the marker segments to the
    SOF0/SOF2 header.  Falls back to 0 0 (Xournal++ then uses the placed box).
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w = struct.unpack(">I", data[16:20])[0]
        h = struct.unpack(">I", data[20:24])[0]
        return (int(w), int(h))
    if data[:2] == b"\xff\xd8":
        i = 2
        n = len(data)
        while i + 4 <= n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                h = struct.unpack(">H", data[i + 5:i + 7])[0]
                w = struct.unpack(">H", data[i + 7:i + 9])[0]
                return (int(w), int(h))
            if 0xD0 <= marker <= 0xD9 or marker in (0x01, 0x00):
                i += 2
                continue
            seglen = struct.unpack(">H", data[i + 2:i + 4])[0]
            i += 2 + seglen
    return (0, 0)


def _image_xml(image: Image, scale: float) -> str:
    """Emit an ``<image>`` element with the raster base64-encoded.

    ``left/top/right/bottom`` are the placed box in page points (top-left
    origin); ``naturalSize`` is the image's intrinsic pixel dimensions, which
    Xournal++ scales from to the placed box.
    """
    x, y = image.position
    w, h = image.size
    left, top = x * scale, y * scale
    right, bottom = (x + w) * scale, (y + h) * scale
    natural = _natural_size(image.data)
    b64 = base64.b64encode(image.data).decode("ascii")
    return (f'<image left="{_fmt(left)}" top="{_fmt(top)}" '
            f'right="{_fmt(right)}" bottom="{_fmt(bottom)}" '
            f'naturalSize="{natural[0]} {natural[1]}">{b64}</image>')


def _estimate_font_size(text: TextBox) -> float:
    """Guess a point size from the RTF's ``\\fs`` (half-points) if present."""
    m = re.search(r"\\fs(\d+)", text.rtf)
    if m:
        return int(m.group(1)) / 2.0
    # otherwise: box height is roughly 1.4x the font size for a single line
    return max(8.0, text.size[1] / 1.4)


def _text_xml(text: TextBox, scale: float) -> str:
    """Emit a ``<text>`` element from a text box.

    ``x``/``y`` are the top-left in page points; ``wrap`` is the box width so
    multi-line text wraps like the original.  The content is the plain
    (RTF-stripped) text.
    """
    x, y = text.position
    size = _estimate_font_size(text)
    color = color_to_hex(text.color)
    content = escape(text.plain)
    return (f'<text font="Sans" size="{_fmt(size)}" color="{color}" '
            f'x="{_fmt(x * scale)}" y="{_fmt(y * scale)}" '
            f'wrap="{_fmt(text.size[0] * scale)}" align="left">{content}</text>')


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
        # images first, so the ink renders on top
        for image in page.images:
            lines.append(_image_xml(image, page.scale))
        for stroke in page.strokes:
            lines.append(_stroke_xml(stroke, page.scale, width_scale))
        for text in page.texts:
            lines.append(_text_xml(text, page.scale))
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
