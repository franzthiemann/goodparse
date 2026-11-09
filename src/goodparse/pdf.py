"""Write the parsed stroke model to a PDF (``.pdf``) file.

Geometry lives in the document's *canvas* space; each page scales it to its
exported size by ``page.scale`` and then maps to PDF's **bottom-left** origin
(``y_pdf = page.height - y_page``).  Strokes, images and text are all emitted
after that uniform scale.

Strokes are drawn as vector paths.  A pressure-varying stroke can't be a single
polyline (PDF line width is constant per path), so each segment is emitted on
its own with round caps; segment *k* uses the mean of its endpoint widths, and
the overlapping round caps blend into one smooth, tapering stroke (inside a
transparency group when translucent, so overlaps don't darken).  Colour alpha
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

import zlib
from typing import Dict, List, Optional, Tuple

from .model import GoodNotesDocument, Stroke, TextBox
from .base14_widths import base14_font, base14_key, run_width
from .text_rtf import TextRun
from .textlayout import layout_text

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
# Optional font embedding (needs fontTools).  The module imports cleanly even
# without fontTools; it only returns None / falls back then.  Kept optional so
# the core library stays importable with zero third-party packages.
try:
    from . import font_embed as _font_embed
except Exception:  # pragma: no cover - depends on install
    _font_embed = None

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


def _background_to_jpeg(bg: bytes, index: int = 0) -> Optional[Tuple[int, int, bytes]]:
    """Render page ``index`` of a background paper PDF to an RGB JPEG."""
    if not _has_raster() or not bg:
        return None
    try:
        doc = _pdfium.PdfDocument(bg)
        try:
            page = doc[index if index < len(doc) else 0]
            bitmap = page.render(scale=_BG_SCALE)
            im = bitmap.to_pil().convert("RGB")
            bitmap.close()
            page.close()
        finally:
            doc.close()
    except Exception:
        return None
    import io
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return im.width, im.height, buf.getvalue()


def _group_xobject(content: str, width: float, height: float) -> bytes:
    """A page-sized transparency-group Form XObject painting ``content``."""
    data = zlib.compress(content.encode("cp1252", "replace"))
    return (f"<< /Type /XObject /Subtype /Form /BBox [0 0 {_num(width, 2)} "
            f"{_num(height, 2)}] /Group << /S /Transparency >> /Length {len(data)} "
            "/Filter /FlateDecode >>\nstream\n").encode() + data + b"\nendstream"


def _image_obj(w: int, h: int, jpg: bytes) -> bytes:
    """An image XObject object body (DCTDecode JPEG, DeviceRGB)."""
    return (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
            f"/ColorSpace /DeviceRGB /BitsPerComponent 8 "
            f"/Filter /DCTDecode /Length {len(jpg)} >>\nstream\n".encode()
            + jpg + b"\nendstream")


def _emit_text(parts: List[str], t: TextBox, s: float, h: float,
               font_map: Dict[Tuple[str, bool, bool], str],
               embed_map: Optional[Dict] = None) -> None:
    """Append text ops for a text box (top-left origin -> PDF).

    Placement comes from :func:`goodparse.textlayout.layout_text`.  A run is
    drawn with its own size, colour and font: an embedded subset of the named
    family when its clone is installed (2-byte glyph-ID ``<hex>`` strings,
    real advance widths), else the closest base-14 family (Helvetica / Times
    / Courier).  Underlines and strikethroughs are thin vector lines
    under/through the run.
    """
    def embedded(run):
        if embed_map and run.font_family:
            ent = embed_map.get((run.font_family, bool(run.bold), bool(run.italic)))
            if ent:
                return ent[0], ent[1]
        return None

    def width(seg: str, run, size: float) -> float:
        e = embedded(run)
        if e:
            return e[1].width(seg) / 1000.0 * size
        return run_width(seg, size, base14_key(run))

    for line in layout_text(t, s, width):
        baseline = h - line.baseline
        for sg in line.segments:
            seg, run, fs, cursor = sg.text, sg.run, sg.size, sg.x
            r, g, b, _a = run.color
            parts.append(f"{_num(r)} {_num(g)} {_num(b)} rg")
            emb = embedded(run)
            if emb is not None:
                fname, ef = emb
                parts.append(f"/{fname} {fs:.2f} Tf")
                parts.append(f"BT {cursor:.2f} {baseline:.2f} Td <{ef.code(seg).hex()}> Tj ET")
            else:
                fname = font_map[base14_key(run)]
                parts.append(f"/{fname} {fs:.2f} Tf")
                parts.append(f"BT {cursor:.2f} {baseline:.2f} Td ({_pdf_escape(seg)}) Tj ET")
            for flag, dy in ((run.underline, -0.12), (run.strike, 0.33)):
                if flag:
                    ly = baseline + fs * dy
                    parts.append(f"{_num(r)} {_num(g)} {_num(b)} RG")
                    parts.append(f"{max(0.5, fs * 0.05):.2f} w "
                                 f"{cursor:.2f} {ly:.2f} m "
                                 f"{cursor + width(seg.rstrip(), run, fs):.2f} {ly:.2f} l S")


def _page_content(page, width_scale: float, alpha_name: Dict[float, str],
                  has_font: bool, next_num: int,
                  font_map: Dict[Tuple[str, bool, bool], str],
                  embed_map: Optional[Dict] = None
                  ) -> Tuple[str, List[str], Dict[int, bytes]]:
    """Build a page's content stream plus its image XObject objects.

    Returns ``(content_text, xobject_refs, image_bodies)`` where ``xobject_refs``
    are ``/Name objnum R`` strings (in draw order) and ``image_bodies`` maps the
    freshly-assigned image object numbers to their bodies.  ``next_num`` is the
    first free object number to hand out for images.
    """
    s = page.scale
    h = page.height
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
        bg = _background_to_jpeg(page.background, page.background_page)
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

    # --- text-box backgrounds (beneath the ink) ----------------------------- #
    for t in page.texts:
        if t.background is not None:
            r, g, b, a = t.background
            x, y = t.position[0] * s, h - (t.position[1] + t.size[1]) * s
            out.append("q")
            if a < 1.0:
                out.append(f"/{alpha_name[round(a, 4)]} gs")
            out.append(f"{_num(r)} {_num(g)} {_num(b)} rg "
                       f"{_num(x, 2)} {_num(y, 2)} {_num(t.size[0] * s, 2)} "
                       f"{_num(t.size[1] * s, 2)} re f Q")
    # --- strokes (already in paint order) ----------------------------------- #
    for stroke in page.strokes:
        r, g, b, a = stroke.color
        pts = _stroke_points(stroke)
        widths = [max(_MIN_WIDTH, w * width_scale * s) for _x, _y, w in pts]
        if len(pts) < 2:
            continue
        if stroke.fill is not None:
            # Shape interior, painted beneath its outline in its own q/Q so
            # the fill alpha cannot leak into the outline.
            fr, fg, fb, fa = stroke.fill
            out.append("q")
            out.append(f"{_num(fr)} {_num(fg)} {_num(fb)} rg")
            if fa < 1.0:
                out.append(f"/{alpha_name[round(fa, 4)]} gs")
            out.append(f"{_num(pts[0][0] * s, 2)} {_num(h - pts[0][1] * s, 2)} m")
            for x, y, _w in pts[1:]:
                out.append(f"{_num(x * s, 2)} {_num(h - y * s, 2)} l")
            out.append("h f")
            out.append("Q")
        # Each stroke is wrapped in q … Q so its alpha ExtGState, width and
        # caps cannot leak into the next stroke.  Round caps and joins (PDF
        # operators are postfix: ``1 J 1 j``) give GoodNotes' rounded ends and
        # let the per-segment pieces of a pressure stroke blend seamlessly.
        # A uniform-width stroke is one continuous path.  A width-varying
        # (pressure) stroke is drawn segment by segment; under alpha the
        # overlapping round caps would double up, so a translucent one is
        # painted opaque inside a transparency-group Form XObject and the
        # alpha applied once, to the whole group.
        uniform = max(widths) - min(widths) < 1e-6
        path = ["q 1 J 1 j", f"{_num(widths[0])} w", f"{_num(r)} {_num(g)} {_num(b)} RG"]
        if uniform:
            path.append(f"{_num(pts[0][0] * s, 2)} {_num(h - pts[0][1] * s, 2)} m")
            for k in range(1, len(pts)):
                path.append(f"{_num(pts[k][0] * s, 2)} {_num(h - pts[k][1] * s, 2)} l")
            path.append("S")
        else:
            for k in range(len(pts) - 1):
                w = (widths[k] + widths[k + 1]) / 2.0
                x0, y0, _w0 = pts[k]
                x1, y1, _w1 = pts[k + 1]
                path.append(f"{_num(w)} w")
                path.append(f"{_num(x0 * s, 2)} {_num(h - y0 * s, 2)} m")
                path.append(f"{_num(x1 * s, 2)} {_num(h - y1 * s, 2)} l")
                path.append("S")
        path.append("Q")
        if a >= 1.0:
            out.extend(path)
        elif uniform:
            out.extend(path[:3] + [f"/{alpha_name[round(a, 4)]} gs"] + path[3:])
        else:
            num = take_num()
            name = f"Gr{num}"
            img_bodies[num] = _group_xobject("\n".join(path), page.width, h)
            xrefs.append(f"/{name} {num} 0 R")
            out.append(f"q /{alpha_name[round(a, 4)]} gs /{name} Do Q")

    # --- text --------------------------------------------------------------- #
    if has_font:
        for t in page.texts:
            _emit_text(out, t, s, h, font_map, embed_map)

    return "\n".join(out) + "\n", xrefs, img_bodies


def build_pdf(doc: GoodNotesDocument, width_scale: float = 1.0) -> bytes:
    """Render the document model to a minimal valid PDF 1.4 byte string."""
    pages = doc.pages
    n_pages = len(pages)

    # Unique non-opaque alphas across the whole doc -> shared ExtGState objects.
    alphas = sorted({
        round(c[3], 4)
        for page in pages for s in page.strokes
        for c in (s.color, s.fill) if c is not None and c[3] < 1.0
    } | {
        round(t.background[3], 4)
        for page in pages for t in page.texts
        if t.background is not None and t.background[3] < 1.0
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
    font_map: Dict[Tuple[str, bool, bool], str] = {}
    next_free = ext_first + len(alphas)
    font_obj = next_free
    if has_font:
        used = set()
        for page in pages:
            for t in page.texts:
                runs = [r for ln in (t.lines or []) for r in ln.runs]
                if not runs:
                    runs = [TextRun(text="")]
                for r in runs:
                    used.add(base14_key(r))
        for fi, key in enumerate(sorted(used)):
            font_map[key] = f"F{fi + 1}"
        next_free += len(font_map)
    # Optional embedded fonts: for any run that names a non-base-14 family
    # (e.g. "Futura") whose clone is installed, subset it and embed it so the
    # glyphs match.  Each (family, bold, italic) group gets 5 objects (Type0,
    # CIDFontType0C, FontDescriptor, FontFile3, ToUnicode).  With no matching
    # font (or no fontTools) this stays empty and base-14 Helvetica is used.
    embed_map: Dict[Tuple[str, bool, bool], Tuple[str, "object", int]] = {}
    embed_objs: Dict[int, bytes] = {}
    if _font_embed is not None and _font_embed.fonttools_available():
        groups: Dict[Tuple[str, bool, bool], str] = {}
        for page in pages:
            for t in page.texts:
                for ln in (t.lines or []):
                    for r in ln.runs:
                        fam = getattr(r, "font_family", None)
                        if not fam:
                            continue
                        key = (fam, bool(r.bold), bool(r.italic))
                        # Always include a space so multi-word segments can
                        # code the trailing space added at emission.
                        groups[key] = groups.get(key, " ") + (r.text or "")
        for key, text in sorted(groups.items()):
            fam, bold, italic = key
            path = _font_embed.find_font_file(fam, bold, italic)
            if not path:
                continue
            # Subset fonts need distinct names: "ABCDEF+Family[Bold][Italic]".
            n = len(embed_map)
            tag = "GP" + "".join(chr(65 + (n // 26 ** k) % 26) for k in range(4))
            style = ("Bold" if bold else "") + ("Italic" if italic else "")
            ef = _font_embed.subset_and_embed(path, text, f"{tag}+{fam}{style}")
            if ef is None:
                continue
            res = "EF%d" % (len(embed_map) + 1)
            base_obj = next_free
            for num, b in _font_embed.build_font_objects(base_obj, ef).items():
                embed_objs[num] = b
            embed_map[key] = (res, ef, base_obj)
            next_free += 5
    img_base = next_free                     # image objects numbered from here

    bodies: Dict[int, bytes] = {
        CATALOG: b"<< /Type /Catalog /Pages 2 0 R >>",
        PAGES: f"<< /Type /Pages /Kids [{' '.join(f'{page_obj(i)} 0 R' for i in range(n_pages))}] /Count {n_pages} >>".encode(),
    }
    for j, a in enumerate(alphas):
        bodies[extg_obj(j)] = f"<< /ca {_num(a)} /CA {_num(a)} >>".encode()
    if has_font:
        for fi, key in enumerate(font_map):
            bodies[font_obj + fi] = (
                f"<< /Type /Font /Subtype /Type1 /BaseFont /{base14_font(key)}"
                " /Encoding /WinAnsiEncoding >>").encode()

    for num, payload in embed_objs.items():
        bodies[num] = payload
    img_counter = img_base
    for i, page in enumerate(pages):
        content, xrefs, img_bodies = _page_content(
            page, width_scale, alpha_name, has_font, img_counter,
            font_map, embed_map)
        img_counter += len(img_bodies)
        for num, payload in img_bodies.items():
            bodies[num] = payload

        stream = content.encode("cp1252", "replace")
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
                for fi, fname in enumerate(font_map.values()))
            if embed_map:
                font_refs += " " + " ".join(
                    f"/{res} {base_obj} 0 R"
                    for res, _ef, base_obj in embed_map.values())
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
