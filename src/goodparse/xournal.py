"""Write the parsed stroke model to a Xournal++ ``.xopp`` file.

A ``.xopp`` file is gzip-compressed XML (Xournal++ file version 4).  The
element types used:

* ``<stroke tool="pen|highlighter" color="#RRGGBBAA" width="...">x1 y1 ...</stroke>``
  for pen, highlighter and eraser strokes.  ``width`` is the base width
  followed by one width per segment (N points -> N-1 extra values), which
  Xournal++ uses to render pressure-varying strokes.
* ``<image left top right bottom naturalSize="w h">BASE64</image>`` for
  embedded images (BASE64 is the raw PNG/JPEG, no data-URI prefix).
  Xournal++ only decodes rasters and *aborts loading the whole file* on
  anything else, so vector (PDF) stickers are rasterised to PNG first.
* ``<text font size color x y>TEXT</text>`` for text: one element per
  styled span of each laid-out line (see :func:`_text_xml`).

Both GoodNotes and Xournal++ use a *top-left* origin with +y downward, so
coordinates need no flip — only the canvas->page uniform scale is applied
(see :class:`~goodparse.goodnotes.Page.scale`).  Page size comes from the
embedded paper PDF when present, else A4.

The paper itself is written beside the document as ``<name>.xopp.bg.pdf``
— Xournal++'s own *attached background PDF* — and each page's
``<background type="pdf" pageno="N"/>`` shows its page of it (vector, not
rasterised; see :func:`background_pdf`).
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import math
import struct
import warnings
from typing import Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

from .base14_widths import base14_key
from .raster import image_raster
from .textlayout import Segment, base14_width, layout_text
from .model import (
    GoodNotesDocument,
    Image,
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
    """Emit a ``<stroke>``; widths are canvas units, so they take the page
    scale as well as the user's ``width_scale``.

    A filled shape gets Xournal++'s ``fill`` attribute (fill opacity 0..255);
    Xournal++ fills with the stroke colour, which is what GoodNotes uses too.

    Xournal++ ignores the colour's alpha byte (checked with 1.3.8): a pen
    always paints opaque and a highlighter at its own fixed opacity (~0.47).
    So a translucent pen stroke (GoodNotes highlighters are alpha 0.5)
    becomes ``tool="highlighter"``, and pencil — alpha 0.1-0.3 — stays a pen
    whose colour is blended onto white paper at its opacity: exact on white,
    lighter than the original where it overlaps other ink or coloured paper.
    """
    points = _stroke_points(stroke, scale)
    tool = "pen"
    r, g, b, a = stroke.color
    if a < 1.0 and stroke.kind == "pencil":
        color = color_to_hex((1 - a * (1 - r), 1 - a * (1 - g), 1 - a * (1 - b), 1.0))
    else:
        color = color_to_hex(stroke.color)
        if a < 1.0:
            tool = "highlighter"
    coords = " ".join(f"{_fmt(x)} {_fmt(y)}" for x, y, _w in points)
    width = _width_attr(points, scale * width_scale)
    fill = ""
    if stroke.fill is not None:
        fill = f' fill="{round(max(0.0, min(1.0, stroke.fill[3])) * 255)}"'
    return f'<stroke tool="{tool}" color="{color}"{fill} width="{width}">{coords}</stroke>'


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
    raster = image_raster(image, scale)
    if raster is None:
        return ""
    natural = (raster.width, raster.height) if raster.width else _natural_size(raster.data)
    b64 = base64.b64encode(raster.data).decode("ascii")
    return (f'<image left="{_fmt(left)}" top="{_fmt(top)}" '
            f'right="{_fmt(right)}" bottom="{_fmt(bottom)}" '
            f'naturalSize="{natural[0]} {natural[1]}">{b64}</image>')


# Distance from a <text> element's y to its baseline: Xournal++ (Pango, hinted
# metrics) puts it at ceil(ascent * size) points.  Ascents measured in
# Xournal++ 1.3.8 with the URW base-35 fonts that fontconfig resolves the
# base-14 names to (Nimbus Sans / Roman / Mono PS); keyed by (class, italic,
# bold-italic) since bold alone does not change them.
_ASCENT = {"Helvetica": (0.730, 0.895, 0.895),
           "Times": (0.680, 0.680, 0.695),
           "Courier": (0.606, 0.606, 0.606)}


def _font(key) -> Tuple[str, float]:
    """Pango font name and ascent for a base-14 key ``(class, bold, italic)``."""
    cls, bold, italic = key
    name = cls + (" Bold" if bold else "") + (" Italic" if italic else "")
    ascent = _ASCENT[cls][2 if bold and italic else 1 if italic else 0]
    return name, ascent


def _spans(segments: List[Segment]) -> List[List[Segment]]:
    """Consecutive word pieces of one font, size and colour (whitespace-only
    pieces join the current span); spans without visible text dropped."""
    def style(sg):
        return (base14_key(sg.run), round(sg.size, 3), tuple(sg.run.color))
    spans: List[List[Segment]] = []
    for sg in segments:
        if spans and (not sg.text.strip() or style(sg) == style(spans[-1][0])):
            spans[-1].append(sg)
        else:
            spans.append([sg])
    return [sp for sp in spans if any(sg.text.strip() for sg in sp)]


def _text_xml(text: TextBox, scale: float) -> List[str]:
    """``<text>`` elements for a text box, one per styled span of each line.

    Placement comes from :func:`goodparse.textlayout.layout_text` (as for the
    PDF); a Xournal++ text element has a single font, size and colour, and its
    own line spacing could not reproduce GoodNotes', hence one element per
    span and line.  Fonts are the base-14 classes (Helvetica / Times /
    Courier) with bold and italic, which every fontconfig maps to
    metric-compatible fonts; underline and strikethrough are thin strokes.
    """
    out: List[str] = []
    for line in layout_text(text, scale):
        for span in _spans(line.segments):
            first = span[0]
            raw = "".join(sg.text for sg in span)
            lead = raw[:len(raw) - len(raw.lstrip())]
            x = first.x + base14_width(lead, first.run, first.size)
            name, ascent = _font(base14_key(first.run))
            y = line.baseline - math.ceil(ascent * first.size)
            out.append(f'<text font="{name}" size="{_fmt(first.size)}" '
                       f'color="{color_to_hex(first.run.color)}" x="{_fmt(x)}" '
                       f'y="{_fmt(y)}">{escape(raw.strip())}</text>')
        for sg in line.segments:
            for flag, dy in ((sg.run.underline, 0.12), (sg.run.strike, -0.33)):
                if flag and sg.text.strip():
                    w = base14_width(sg.text.rstrip(), sg.run, sg.size)
                    ly = line.baseline + sg.size * dy
                    out.append(f'<stroke tool="pen" color="{color_to_hex(sg.run.color)}" '
                               f'width="{_fmt(max(0.5, sg.size * 0.05))}">'
                               f'{_fmt(sg.x)} {_fmt(ly)} {_fmt(sg.x + w)} {_fmt(ly)}</stroke>')
    return out


def _text_background_xml(text: TextBox, scale: float) -> str:
    """A text box's fill colour as a filled, closed rectangle stroke."""
    r, g, b, a = text.background
    x0, y0 = text.position[0] * scale, text.position[1] * scale
    x1, y1 = x0 + text.size[0] * scale, y0 + text.size[1] * scale
    coords = " ".join(_fmt(v) for v in (x0, y0, x1, y0, x1, y1, x0, y1, x0, y0))
    return (f'<stroke tool="pen" color="{color_to_hex((r, g, b, 1.0))}" '
            f'fill="{round(max(0.0, min(1.0, a)) * 255)}" width="0.1">{coords}</stroke>')


# Xournal++'s "attached" background: the PDF lives next to the document as
# ``<name>.xopp.bg.pdf`` and the first PDF page background names it "bg.pdf".
BACKGROUND_SUFFIX = ".bg.pdf"


def background_pdf(doc: GoodNotesDocument) -> Tuple[Optional[bytes], List[Optional[int]]]:
    """Collect the pages' paper into one PDF for Xournal++.

    Xournal++ takes a single background PDF per document, while GoodNotes
    pages each show a page of their own paper PDF; so every distinct
    (paper, page) is copied — as vectors — into one merged PDF.  Returns its
    bytes (``None`` when no page has paper, or pypdfium2 is missing) and,
    per page, the 1-based page number in it (``None``: no paper).
    """
    if not any(p.background for p in doc.pages):
        return None, [None] * len(doc.pages)
    try:
        import pypdfium2 as pdfium
    except ImportError:
        warnings.warn("pypdfium2 missing: Xournal++ pages get no paper background")
        return None, [None] * len(doc.pages)
    merged = pdfium.PdfDocument.new()
    sources: Dict[str, object] = {}
    placed: Dict[Tuple[str, int], int] = {}
    numbers: List[Optional[int]] = []
    try:
        for page in doc.pages:
            if not page.background:
                numbers.append(None)
                continue
            key = hashlib.sha1(page.background).hexdigest()
            try:
                if key not in sources:
                    sources[key] = pdfium.PdfDocument(page.background)
                src = sources[key]
                index = page.background_page if page.background_page < len(src) else 0
                if (key, index) not in placed:
                    merged.import_pages(src, [index])
                    placed[(key, index)] = len(merged)
                numbers.append(placed[(key, index)])
            except pdfium.PdfiumError as exc:
                warnings.warn(f"unreadable paper PDF, page left plain: {exc}")
                numbers.append(None)
        if not placed:
            return None, numbers
        buf = io.BytesIO()
        merged.save(buf)
        return buf.getvalue(), numbers
    finally:
        merged.close()
        for src in sources.values():
            src.close()


def build_xml(doc: GoodNotesDocument, width_scale: float = DEFAULT_WIDTH_SCALE,
              background_pages: Optional[List[Optional[int]]] = None) -> str:
    """Render the document model to Xournal++ XML.

    ``background_pages`` (from :func:`background_pdf`) gives each page's page
    in the attached background PDF; without it every page is plain white.
    """
    lines = [
        '<?xml version="1.0" standalone="no"?>',
        f'<xournal creator="{CREATOR}" fileversion="{FILE_VERSION}">',
        f"<title>{escape(doc.title)}</title>",
    ]
    attached = False
    for i, page in enumerate(doc.pages):
        lines.append(f'<page width="{_fmt(page.width)}" height="{_fmt(page.height)}">')
        pageno = background_pages[i] if background_pages else None
        if pageno is None:
            lines.append('<background type="solid" color="white" style="plain"/>')
        elif not attached:
            attached = True
            lines.append(f'<background type="pdf" domain="attach" filename="bg.pdf" '
                         f'pageno="{pageno}"/>')
        else:
            lines.append(f'<background type="pdf" pageno="{pageno}"/>')
        lines.append("<layer>")
        # images first, so the ink renders on top
        for image in page.images:
            xml = _image_xml(image, page.scale)
            if xml:
                lines.append(xml)
        for text in page.texts:
            if text.background is not None:
                lines.append(_text_background_xml(text, page.scale))
        for stroke in page.strokes:
            lines.append(_stroke_xml(stroke, page.scale, width_scale))
        for text in page.texts:
            lines.extend(_text_xml(text, page.scale))
        lines.append("</layer>")
        lines.append("</page>")
    lines.append("</xournal>")
    return "\n".join(lines) + "\n"


def write_xopp(doc: GoodNotesDocument, path: str,
               width_scale: float = DEFAULT_WIDTH_SCALE) -> None:
    """Write the document to a gzip-compressed ``.xopp`` file, plus its paper
    as ``<path>.bg.pdf`` (Xournal++'s attached background PDF) when any page
    has one.  Keep the two files together."""
    bg, numbers = background_pdf(doc)
    if bg is not None:
        with open(path + BACKGROUND_SUFFIX, "wb") as fh:
            fh.write(bg)
    xml = build_xml(doc, width_scale, numbers if bg is not None else None)
    with gzip.open(path, "wb") as fh:
        fh.write(xml.encode("utf-8"))
