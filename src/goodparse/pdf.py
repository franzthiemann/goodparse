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
from types import SimpleNamespace
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


# Helvetica glyph advance widths (Adobe WinAnsi), in thousandths of an em.  Used
# only to place underlines/strikethroughs under styled runs; a 0 entry means
# "use the space width".
_HELVETICA_ADV = {
    0x20: 278, 0x21: 278, 0x22: 355, 0x23: 556, 0x24: 556, 0x25: 889,
    0x26: 667, 0x27: 191, 0x28: 333, 0x29: 333, 0x2a: 389, 0x2b: 584,
    0x2c: 278, 0x2d: 333, 0x2e: 278, 0x2f: 278, 0x30: 556, 0x31: 556,
    0x32: 556, 0x33: 556, 0x34: 556, 0x35: 556, 0x36: 556, 0x37: 556,
    0x38: 556, 0x39: 556, 0x3a: 278, 0x3b: 278, 0x3c: 584, 0x3d: 584,
    0x3e: 584, 0x3f: 389, 0x40: 1015, 0x5b: 278, 0x5c: 278, 0x5d: 278,
    0x5e: 469, 0x5f: 556, 0x60: 333, 0x7b: 278, 0x7c: 260, 0x7d: 278,
    0x7e: 584,
}
for _c in range(0x41, 0x5B):  # A-Z
    _HELVETICA_ADV[_c] = 667
for _c in range(0x61, 0x7B):  # a-z
    _HELVETICA_ADV[_c] = 556
# a few real exceptions in the base-1000 Helvetica metrics
for _c, _w in [(0x44, 722), (0x57, 944), (0x46, 611), (0x54, 611),
               (0x56, 667), (0x59, 667), (0x67, 556), (0x6c, 278),
               (0x69, 278), (0x6a, 278), (0x66, 333), (0x75, 556),
               (0x2e, 278), (0x2c, 278)]:
    _HELVETICA_ADV[_c] = _w


def _run_width(text: str, fs: float) -> float:
    """Approximate advance width of ``text`` in PDF points at font size ``fs``."""
    total = 0.0
    for ch in text:
        total += _HELVETICA_ADV.get(ord(ch), 556)
    return total / 1000.0 * fs


def _font_name(bold: bool, italic: bool,
               font_map: Dict[Tuple[bool, bool], str]) -> str:
    return "/" + font_map.get((bold, italic), "F1")


def _pdf_form_xobject(data: bytes) -> Optional[Tuple[float, float, bytes]]:
    """Turn an embedded *vector PDF* (e.g. a GoodNotes sticker) into a Form
    XObject body, returning ``(bbox_w, bbox_h, body)``.

    GoodNotes stores die-cut stickers as small vector PDFs (``%PDF``), not
    rasters.  ``_raster_to_jpeg`` can't decode them, so they were dropped and
    the sticker never appeared.  We lift the page's content stream and re-emit
    it as a self-contained Form XObject.  The only resource the content
    references is an ICC colour space (``/Name cs``) set before each colour;
    the colour values are plain 3-float RGB, so we strip those ``/Name cs``
    operators and let the colours render in DeviceRGB (identical for flat
    fills).  The die-cut (rounded) corners stay transparent because the
    content never paints outside the rounded-rectangle path.
    """
    if not data or data[:4] != b"%PDF":
        return None
    try:
        import re
        txt = data.decode("latin-1")
        # The *page* (singular) MediaBox, not the Pages tree's: locate the
        # object whose dict says /Type /Page (a following space/char keeps it
        # from matching /Pages) and read its MediaBox.
        pw = ph = None
        pg = re.search(r"/Type\s*/Page[^\w]", txt)
        if pg:
            seg = txt[pg.start():pg.start() + 400]
            mb = re.search(r"/MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)\s*\]",
                           seg)
            if mb:
                pw, ph = float(mb.group(1)), float(mb.group(2))
        # The content stream of the (first) page: the object referenced by the
        # page's /Contents.  GoodNotes stickers have exactly one stream object.
        sm = re.search(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S)
        if not sm:
            return None
        raw_cs = sm.group(1)
        try:
            content = zlib.decompress(raw_cs)
        except Exception:
            content = raw_cs
        if not content.strip():
            return None
        if pw is None or ph is None:
            pw, ph = 254.0, 214.0
        bw, bh = pw, ph
        # The content references a colour space by name (e.g. "/Cs1 cs") and
        # sets flat 3-float colours with "sc".  With no colour space in the
        # form's resources, Skia defaults to DeviceGray and keeps only the
        # first component (green -> gray).  Map every colour-space name the
        # content uses to /DeviceRGB: the stored RGB values already equal the
        # reference output (the source ICC profile is sRGB-equivalent), so
        # DeviceRGB reproduces the sticker's exact colours.
        cs_names = set(re.findall(rb"/([A-Za-z0-9_]+)\s+(?:cs|CS)\b", content))
        if cs_names:
            cs_res = " ".join(
                f"/{n.decode('latin-1')} /DeviceRGB" for n in cs_names)
            resources = f" /Resources << /ColorSpace << {cs_res} >> >>"
        else:
            resources = " /Resources << /ColorSpace << /Cs1 /DeviceRGB >> >>"
        body = (f"<< /Type /XObject /Subtype /Form /BBox [0 0 "
                f"{_num(bw, 2)} {_num(bh, 2)}]{resources} /Length "
                f"{len(content)} >>\nstream\n"
                .encode("latin-1") + content + b"\nendstream")
        return bw, bh, body
    except Exception:
        return None


def _raster_to_jpeg(data: bytes, place_w: float = 0.0,
                    place_h: float = 0.0) -> Optional[Tuple[int, int, bytes]]:
    """Decode an embedded raster (PNG/JPEG) and re-encode it as RGB JPEG.

    When the placed box (``place_w`` x ``place_h`` points) has a *portrait*
    aspect but the source raster is *landscape* (or vice versa), GoodNotes
    has rotated the image 90 degrees on the page.  We reproduce that rotation
    (counter-clockwise, matching the reference) so the photo isn't squished.
    A zero ``place_w``/``place_h`` means "no box info" and disables detection.
    """
    if _PILImage is None or not data:
        return None
    import io
    try:
        with _PILImage.open(io.BytesIO(data)) as im:
            rgb = im.convert("RGB")
    except Exception:
        return None
    if place_w > 0 and place_h > 0:
        native_ar = rgb.width / rgb.height
        placed_ar = place_w / place_h
        # |native - placed| large AND native*placed ~ 1 -> the axes are swapped,
        # i.e. a 90-degree rotation.  0.15 catches 4:3/3:4, 16:9/9:16, etc.
        if abs(native_ar - placed_ar) > 0.15 and abs(native_ar * placed_ar - 1.0) < 0.5:
            rgb = rgb.rotate(270, expand=True)  # CCW 90 degrees
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


def _emit_text(parts: List[str], t: TextBox, s: float, h: float,
               font_map: Dict[Tuple[bool, bool], str]) -> None:
    """Append ``Tj``/line ops for a text box (top-left origin -> PDF).

    Each styled run is emitted with its own font (``/F1``.. = plain,
    bold, italic, bold-italic); underlines and strikethroughs are thin vector
    lines positioned under/through the run using the Helvetica advance widths.
    Runs are reflowed word-by-word so they wrap to the box width (matching the
    reference, which wraps a long line) instead of overflowing the page.
    """
    fs = _text_font_size(t) * s
    x0 = t.position[0] * s
    top = t.position[1] * s
    box_w = max(fs * 0.5, t.size[0] * s)
    leading = fs * 1.2

    lines = t.lines if t.lines else [SimpleNamespace(
        runs=[SimpleNamespace(text=t.plain, bold=False, italic=False,
                              underline=False, strike=False, color=t.color)])]

    # Flatten every run into (word, run-attrs) tokens, keeping each word's
    # trailing space so styled runs join with the exact spacing of the original
    # (GoodNotes packs words tight: "Test123italic" with no added spaces).
    tokens = []
    for line in lines:
        for run in line.runs:
            if not run.text:
                continue
            for w in run.text.split(" "):
                if not w:
                    continue
                tokens.append(SimpleNamespace(
                    text=w, bold=run.bold, italic=run.italic,
                    underline=run.underline, strike=run.strike,
                    color=run.color))

    # Greedy word wrap across visual lines.
    vlines: List[List] = [[]]
    used = 0.0
    for tok in tokens:
        w = _run_width(tok.text, fs)
        space = _run_width(" ", fs)
        add = w + (space if vlines[-1] else 0.0)
        if vlines[-1] and used + add > box_w and used > 0:
            vlines.append([tok])
            used = w
        else:
            vlines[-1].append(tok)
            used += add

    for j, vline in enumerate(vlines):
        if not vline:
            continue
        baseline = h - (top + fs * 0.8 + j * leading)
        cursor = x0
        for k, tok in enumerate(vline):
            r, g, b, _a = tok.color
            fname = _font_name(bool(tok.bold), bool(tok.italic), font_map)
            seg = tok.text
            if k < len(vline) - 1:
                seg += " "
            parts.append(f"{_num(r)} {_num(g)} {_num(b)} rg")
            parts.append(f"{fname} {fs:.2f} Tf")
            parts.append(
                f"BT {cursor:.2f} {baseline:.2f} Td ({_pdf_escape(seg)}) Tj ET")
            w = _run_width(seg, fs)
            if tok.underline:
                uy = baseline - fs * 0.12
                parts.append(f"{_num(r)} {_num(g)} {_num(b)} RG")
                parts.append(f"{max(0.5, fs * 0.05):.2f} w "
                             f"{cursor:.2f} {uy:.2f} m "
                             f"{cursor + w:.2f} {uy:.2f} l S")
            if tok.strike:
                sy = baseline + fs * 0.33
                parts.append(f"{_num(r)} {_num(g)} {_num(b)} RG")
                parts.append(f"{max(0.5, fs * 0.05):.2f} w "
                             f"{cursor:.2f} {sy:.2f} m "
                             f"{cursor + w:.2f} {sy:.2f} l S")
            cursor += w


def _page_content(page, width_scale: float, alpha_name: Dict[float, str],
                  has_font: bool, next_num: int,
                  font_map: Dict[Tuple[bool, bool], str]) -> Tuple[str,
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
    # NOTE: the round cap/join (J/j) is emitted *per stroke*, just before that
    # stroke's width, not here.  pypdfium2/Skia drops the stroke colour to
    # black when a J or j operator appears *immediately before* an ``RG``;
    # keeping a ``w`` between them avoids that (see the per-stroke emission).
    out: List[str] = []
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

    # --- embedded photos / vector stickers --------------------------------- #
    if _has_raster():
        for img in page.images:
            px = img.position[0] * s
            pw = img.size[0] * s
            ph = img.size[1] * s
            ypdf = h - (img.position[1] + img.size[1]) * s
            name = f"Im{len(xrefs)}"
            # Vector PDF sticker (die-cut): embed as a Form XObject so its
            # vectors and transparent rounded corners survive (a JPEG would
            # fill the corners with white over the grid).
            form = _pdf_form_xobject(img.data)
            if form is not None:
                bw, bh, body = form
                num = take_num()
                img_bodies[num] = body
                xrefs.append(f"/{name} {num} 0 R")
                # A Form XObject uses its BBox (bw x bh) as its own coordinate
                # space (unlike an Image XObject, which is always the unit
                # square).  So scale BBox -> placement box (pw x ph), not by
                # pw/ph directly.
                out.append(f"q {_num(pw / bw, 4)} 0 0 {_num(ph / bh, 4)} "
                           f"{_num(px, 2)} {_num(ypdf, 2)} cm /{name} Do Q")
                continue
            rj = _raster_to_jpeg(img.data, img.size[0], img.size[1])
            if not rj:
                continue
            w, hh, jpg = rj
            num = take_num()
            img_bodies[num] = _image_obj(w, hh, jpg)
            xrefs.append(f"/{name} {num} 0 R")
            out.append(f"q {_num(pw, 2)} 0 0 {_num(ph, 2)} "
                       f"{_num(px, 2)} {_num(ypdf, 2)} cm /{name} Do Q")

    # --- strokes ------------------------------------------------------------ #
    # GoodNotes stores each page's strokes in *reverse* paint order (the
    # topmost, last-drawn stroke comes first in the file), so render them in
    # reverse to restore the intended layering (e.g. the basketball's black
    # seams sit on top of the orange fill).
    for stroke in reversed(page.strokes):
        r, g, b, a = stroke.color
        pts = _stroke_points(stroke)
        widths = [max(_MIN_WIDTH, w * width_scale * s) for _x, _y, w in pts]
        if len(pts) < 2:
            continue
        # Emission order matters: caps (J/j) must be separated from ``RG`` by
        # a ``w``.  pypdfium2/Skia renders the stroke black when a cap/join
        # operator appears immediately before the colour, so go
        # caps -> width -> colour -> path.
        #
        # Each stroke is wrapped in a graphics-state save/restore (q … Q) so
        # the alpha ExtGState set for a translucent stroke (pencil / tape /
        # highlighter at alpha < 1) cannot *leak* to the next stroke.  Without
        # it, every subsequent opaque stroke inherits the 0.5 alpha and renders
        # as a faint gray band over the grid (the "gray ovals / too-transparent
        # triangle" bug).  q/Q also isolates the per-stroke width/caps.
        out.append("q")
        out.append("J 1")
        out.append("j 1")
        # A uniform-width stroke is one continuous path: 131 overlapping
        # per-segment round caps would blend to a muddy grey under alpha (and
        # produce the "highlighter rectangles" artifact).  Only a pressure
        # (width-varying) stroke needs per-segment paths, and even then only
        # where the width actually changes.
        uniform = max(widths) - min(widths) < 1e-6
        # Always emit a width before ``RG`` so the caps above are separated
        # from the colour; for a pressure stroke this is just a lead-in
        # (each segment below sets its own width).
        out.append(f"{_num(widths[0])} w")
        out.append(f"{_num(r)} {_num(g)} {_num(b)} RG")
        if a < 1.0:
            out.append(f"/{alpha_name[a]} gs")
        if uniform:
            out.append(f"{_num(pts[0][0] * s, 2)} {_num(h - pts[0][1] * s, 2)} m")
            for k in range(1, len(pts)):
                out.append(f"{_num(pts[k][0] * s, 2)} {_num(h - pts[k][1] * s, 2)} l")
            out.append("S")
        else:
            for k in range(len(pts) - 1):
                w = (widths[k] + widths[k + 1]) / 2.0
                x0, y0, _w0 = pts[k]
                x1, y1, _w1 = pts[k + 1]
                out.append(f"{_num(w)} w")
                out.append(f"{_num(x0 * s, 2)} {_num(h - y0 * s, 2)} m")
                out.append(f"{_num(x1 * s, 2)} {_num(h - y1 * s, 2)} l")
                out.append("S")
        out.append("Q")

    # --- text --------------------------------------------------------------- #
    if has_font:
        for t in page.texts:
            _emit_text(out, t, s, h, font_map)

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
    # Number the remaining objects without gaps: fonts (if text) then images.
    # /F1..Fn = Helvetica plain / bold / italic / bold-italic; only the
    # variants actually referenced by a styled run get an object.
    font_map: Dict[Tuple[bool, bool], str] = {}
    next_free = ext_first + len(alphas)
    font_obj = next_free
    if has_font:
        used = set()
        for page in pages:
            for t in page.texts:
                runs = [r for ln in (t.lines or []) for r in ln.runs]
                if not runs:
                    runs = [SimpleNamespace(bold=False, italic=False, text="")]
                for r in runs:
                    used.add((bool(r.bold), bool(r.italic)))
        for fi, (bold, italic) in enumerate(
                sorted(used, key=lambda b: (b[0], b[1]))):
            font_map[(bold, italic)] = f"F{fi + 1}"
        next_free += len(font_map)
    img_base = next_free                     # image objects numbered from here

    bodies: Dict[int, bytes] = {
        CATALOG: b"<< /Type /Catalog /Pages 2 0 R >>",
        PAGES: f"<< /Type /Pages /Kids [{' '.join(f'{page_obj(i)} 0 R' for i in range(n_pages))}] /Count {n_pages} >>".encode(),
    }
    for j, a in enumerate(alphas):
        bodies[extg_obj(j)] = f"<< /ca {_num(a)} /CA {_num(a)} >>".encode()
    if has_font:
        _base = {(False, False): "/Helvetica", (True, False): "/Helvetica-Bold",
                 (False, True): "/Helvetica-Oblique",
                 (True, True): "/Helvetica-BoldOblique"}
        for fi, ((bold, italic), fname) in enumerate(font_map.items()):
            bodies[font_obj + fi] = (
                b"<< /Type /Font /Subtype /Type1 /BaseFont "
                + _base[(bold, italic)].encode()
                + b" /Encoding /WinAnsiEncoding >>")

    img_counter = img_base
    for i, page in enumerate(pages):
        content, xrefs, img_bodies = _page_content(
            page, width_scale, alpha_name, has_font, img_counter, font_map)
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
            font_refs = " ".join(
                f"/{fname} {font_obj + fi} 0 R"
                for fi, ((bold, italic), fname) in enumerate(font_map.items()))
            res_parts.append(f"/Font << {font_refs} >>")
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
