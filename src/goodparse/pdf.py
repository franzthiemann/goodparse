"""Write the parsed stroke model to a PDF (``.pdf``) file.

Geometry lives in the document's *canvas* space; each page scales it to its
exported size by ``page.scale`` and then maps to PDF's **bottom-left** origin
(``y_pdf = page.height - y_page``).  Strokes, images and text are all emitted
after that uniform scale.

Strokes are drawn as vector paths.  A pressure-varying stroke can't be a single
polyline (PDF line width is constant per path), so each segment is emitted on
its own with round caps; segment *k* uses the mean of its endpoint widths, and
the overlapping round caps blend into one smooth, tapering stroke.  Colour alpha
maps to a shared ``/ExtGState`` (``/ca`` / ``/CA``).

Full-fidelity media (page *backgrounds* and *embedded photos*) are embedded as
JPEG image XObjects, and text boxes are rendered as Type1 ``/Helvetica`` text.
Those features need :mod:`pypdfium2` (to rasterise the background paper PDF) and
:mod:`Pillow` (JPEG encoding); both are **optional** imports so the converter
still produces a valid stroke-only PDF when they are not installed.

The file is a hand-built, minimally-valid **PDF 1.4** (stdlib ``zlib`` for the
Flate content streams, a single linear ``xref`` table and a ``trailer``).
"""

from __future__ import annotations

import re
import zlib
from typing import Dict, List, Optional, Tuple

from .goodnotes import GoodNotesDocument, Stroke, TextBox

# Optional, heavier dependencies for raster media + text.  Kept optional so the
# core library imports with zero third-party packages (the stroke-only path is
# always available).
try:
    import pypdfium2 as _pdfium
except Exception:  # pragma: no cover - depends on install
    _pdfium = None
try:
    from PIL import Image as _PILImage
except Exception:  # pragma: no cover - depends on install
    _PILImage = None

CREATOR = "goodparse"

# Floor mirroring xournal.py/excalidraw.py: a zero-width point still renders.
_MIN_WIDTH = 0.1
# Rasterise the (vector) background paper PDFs at ~2x page size (~144 dpi).
_BG_SCALE = 2.0
# JPEG quality for embedded photos / backgrounds.
_JPEG_QUALITY = 86


def _has_raster() -> bool:
    return _pdfium is not None and _PILImage is not None


def _num(v: float, nd: int = 4) -> str:
    """Format a float, trimming trailing zeros (avoids ``-0`` and long tails)."""
    s = f"{v:.{nd}f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _stroke_points(stroke: Stroke) -> List[Tuple[float, float, float]]:
    """Return at least two points so single-point dots still render."""
    pts = stroke.points
    if len(pts) == 1:
        x, y, w = pts[0]
        return [(x, y, w), (x + 0.1, y + 0.1, w)]
    return pts


def _pdf_escape(s: str) -> str:
    """Escape a string for a PDF ``(...)`` literal."""
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text_font_size(t: TextBox) -> float:
    """Guess the box's canvas-space point size from its RTF (``\\fs`` half-pts)."""
    m = re.search(r"\\fs(\d+)", t.rtf)
    if m:
        return int(m.group(1)) / 2.0
    nlines = max(1, len(t.plain.split("\n")))
    return max(8.0, t.size[1] / (1.2 * nlines))


def _raster_to_jpeg(data: bytes) -> Optional[Tuple[int, int, bytes]]:
    """Decode an embedded raster (PNG/JPEG) and re-encode it as RGB JPEG."""
    if _PILImage is None or not data:
        return None
    import io
    try:
        with _PILImage.open(io.BytesIO(data)) as im:
            rgb = im.convert("RGB")
    except Exception:
        return None
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return rgb.width, rgb.height, buf.getvalue()


def _background_to_jpeg(bg: bytes) -> Optional[Tuple[int, int, bytes]]:
    """Render a background paper PDF's first page to an RGB JPEG XObject."""
    if not _has_raster() or not bg:
        return None
    try:
        doc = _pdfium.PdfDocument(bg)
        try:
            im = doc[0].render(scale=_BG_SCALE).to_pil().convert("RGB")
        finally:
            doc.close()
    except Exception:
        return None
    import io
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return im.width, im.height, buf.getvalue()


def _image_obj(w: int, h: int, jpg: bytes) -> bytes:
    """An image XObject object body (DCTDecode JPEG, DeviceRGB)."""
    return (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
            f"/Filter /DCTDecode /Length {len(jpg)} >>\nstream\n".encode()
            + jpg + b"\nendstream")


def _emit_text(parts: List[str], t: TextBox, s: float, h: float) -> None:
    """Append Helvetica ``Tj`` ops for a text box (top-left origin -> PDF)."""
    r, g, b, _a = t.color
    fs = _text_font_size(t) * s
    x = t.position[0] * s
    top = t.position[1] * s
    leading = fs * 1.2
    parts.append(f"{_num(r)} {_num(g)} {_num(b)} rg")
    parts.append(f"/F1 {fs:.2f} Tf")
    for j, line in enumerate(t.plain.split("\n")):
        baseline = h - (top + fs * 0.8 + j * leading)
        parts.append(
            f"BT {x:.2f} {baseline:.2f} Td ({_pdf_escape(line)}) Tj ET")


def _page_content(page, width_scale: float, alpha_name: Dict[float, str],
                  has_font: bool, next_num: int) -> Tuple[str,
                                                          List[str],
                                                          Dict[int, bytes]]:
    """Build a page's content stream plus its image XObject objects.

    Returns ``(content_text, xobject_refs, image_bodies)`` where ``xobject_refs``
    are ``/Name objnum R`` strings (in draw order) and ``image_bodies`` maps the
    freshly-assigned image object numbers to their bodies.  ``next_num`` is the
    first free object number to hand out for images.
    """
    s = page.scale
    h = page.height
    out: List[str] = ["J 1", "j 1"]  # round line cap, round line join
    xrefs: List[str] = []
    img_bodies: Dict[int, bytes] = {}
    counter = [next_num]

    def take_num() -> int:
        n = counter[0]
        counter[0] += 1
        return n

    # --- background (drawn first, so everything sits on top) ---------------- #
    if _has_raster() and page.background:
        bg = _background_to_jpeg(page.background)
        if bg:
            w, hh, jpg = bg
            num = take_num()
            img_bodies[num] = _image_obj(w, hh, jpg)
            xrefs.append(f"/ImBg {num} 0 R")
            out.append(f"q {_num(page.width, 2)} 0 0 {_num(h, 2)} 0 0 cm /ImBg Do Q")

    # --- embedded photos ---------------------------------------------------- #
    if _has_raster():
        for img in page.images:
            rj = _raster_to_jpeg(img.data)
            if not rj:
                continue
            w, hh, jpg = rj
            num = take_num()
            img_bodies[num] = _image_obj(w, hh, jpg)
            name = f"Im{len(xrefs)}"
            xrefs.append(f"/{name} {num} 0 R")
            px = img.position[0] * s
            pw = img.size[0] * s
            ph = img.size[1] * s
            ypdf = h - (img.position[1] + img.size[1]) * s
            out.append(f"q {_num(pw, 2)} 0 0 {_num(ph, 2)} "
                       f"{_num(px, 2)} {_num(ypdf, 2)} cm /{name} Do Q")

    # --- strokes ------------------------------------------------------------ #
    for stroke in page.strokes:
        r, g, b, a = stroke.color
        out.append(f"{_num(r)} {_num(g)} {_num(b)} RG")
        if a < 1.0:
            out.append(f"/{alpha_name[a]} gs")
        pts = _stroke_points(stroke)
        widths = [max(_MIN_WIDTH, w * width_scale * s) for _x, _y, w in pts]
        for k in range(len(pts) - 1):
            w = (widths[k] + widths[k + 1]) / 2.0
            x0, y0, _w0 = pts[k]
            x1, y1, _w1 = pts[k + 1]
            out.append(f"{_num(w)} w")
            out.append(f"{_num(x0 * s, 2)} {_num(h - y0 * s, 2)} m")
            out.append(f"{_num(x1 * s, 2)} {_num(h - y1 * s, 2)} l")
            out.append("S")

    # --- text --------------------------------------------------------------- #
    if has_font:
        for t in page.texts:
            _emit_text(out, t, s, h)

    return "\n".join(out) + "\n", xrefs, img_bodies


def build_pdf(doc: GoodNotesDocument, width_scale: float = 1.0) -> bytes:
    """Render the document model to a minimal valid PDF 1.4 byte string."""
    pages = doc.pages
    n_pages = len(pages)

    # Unique non-opaque alphas across the whole doc -> shared ExtGState objects.
    alphas = sorted({
        round(s.color[3], 4)
        for page in pages for s in page.strokes if s.color[3] < 1.0
    })
    alpha_name = {a: f"GS{i}" for i, a in enumerate(alphas)}
    has_font = any(page.texts for page in pages)

    # Fixed object layout (compatible with the stroke-only case):
    #   1 catalog, 2 pages, then per page [page, content], then ExtGState,
    #   then one font (if any text), then all image XObjects.
    CATALOG = 1
    PAGES = 2
    page_obj = lambda i: 3 + 2 * i          # 3, 5, 7, ...
    content_obj = lambda i: 4 + 2 * i       # 4, 6, 8, ...
    ext_first = 3 + 2 * n_pages
    extg_obj = lambda j: ext_first + j      # one per unique alpha
    # Number the remaining objects without gaps: font (if text) then images.
    next_free = ext_first + len(alphas)
    font_obj = next_free
    if has_font:
        next_free += 1
    img_base = next_free                     # image objects numbered from here

    bodies: Dict[int, bytes] = {
        CATALOG: b"<< /Type /Catalog /Pages 2 0 R >>",
        PAGES: f"<< /Type /Pages /Kids [{' '.join(f'{page_obj(i)} 0 R' for i in range(n_pages))}] /Count {n_pages} >>".encode(),
    }
    for j, a in enumerate(alphas):
        bodies[extg_obj(j)] = f"<< /ca {_num(a)} /CA {_num(a)} >>".encode()
    if has_font:
        bodies[font_obj] = (b"<< /Type /Font /Subtype /Type1 "
                            b"/BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")

    img_counter = img_base
    for i, page in enumerate(pages):
        content, xrefs, img_bodies = _page_content(
            page, width_scale, alpha_name, has_font, img_counter)
        img_counter += len(img_bodies)
        for num, payload in img_bodies.items():
            bodies[num] = payload

        stream = content.encode("latin-1")
        compressed = zlib.compress(stream)
        bodies[content_obj(i)] = (
            f"<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n".encode()
            + compressed + b"\nendstream")

        res_parts = []
        if xrefs:
            res_parts.append("/XObject << " + " ".join(xrefs) + " >>")
        if has_font:
            res_parts.append(f"/Font << /F1 {font_obj} 0 R >>")
        if alphas:
            res_parts.append("/ExtGState << " +
                             " ".join(f"/GS{j} {extg_obj(j)} 0 R"
                                      for j in range(len(alphas))) + " >>")
        resources = "<< " + " ".join(res_parts) + " >>"
        bodies[page_obj(i)] = (
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {_num(page.width, 2)} {_num(page.height, 2)}] "
            f"/Resources {resources} "
            f"/Contents {content_obj(i)} 0 R >>"
        ).encode()

    # Serialize: record each object's byte offset, then emit the xref table.
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: Dict[int, int] = {}
    for num in sorted(bodies):
        offsets[num] = len(body)
        body.extend(f"{num} 0 obj\n".encode("latin-1"))
        body.extend(bodies[num])
        body.extend(b"\nendobj\n")

    # xref table: entry 0 (free) + one per object, each exactly 20 bytes.
    xref_start = len(body)
    size = len(offsets) + 1
    body.extend(b"xref\n")
    body.extend(f"0 {size}\n".encode())
    body.extend(b"0000000000 65535 f \n")
    for num in range(1, size):
        body.extend(b"%010d 00000 n \n" % offsets[num])
    body.extend(b"trailer\n")
    body.extend(f"<< /Size {size} /Root 1 0 R >>\n".encode())
    body.extend(b"startxref\n")
    body.extend(f"{xref_start}\n".encode())
    body.extend(b"%%EOF\n")
    return bytes(body)


def write_pdf(doc: GoodNotesDocument, path: str, width_scale: float = 1.0) -> None:
    """Write the document to a ``.pdf`` file."""
    with open(path, "wb") as fh:
        fh.write(build_pdf(doc, width_scale))
