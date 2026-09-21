"""Write the parsed stroke model to a PDF (``.pdf``) file.

GoodNotes stores geometry in PDF points (@72 dpi) with a **top-left** origin, so
the coordinate space is 1:1 with PDF's — except that PDF's origin is at the
**bottom-left**. Every point is therefore Y-flipped per page:
``y_pdf = page.height - y_goodnotes``. Coordinates and page size need no other
scaling.

PDF paths carry a *constant* line width per path, so a pressure-varying stroke
can't be drawn as a single polyline. Instead each stroke is emitted as one
segment at a time with round line caps; segment *k* uses the mean of its two
endpoint widths. Consecutive round-capped segments share an endpoint and overlap,
so they blend into a single smooth stroke whose thickness tapers with the
original per-point widths (the same data Xournal++ uses for its pressure width
attribute). Colour alpha maps to a shared ``/ExtGState`` (``/ca`` and ``/CA``).

The file is a hand-built, minimally-valid **PDF 1.4** (pure Python, no third-party
dependencies, matching the hand-written LZ4/protobuf decoders): uncompressed
object dictionaries, ``/FlateDecode`` (stdlib ``zlib``) content streams, a single
linear ``xref`` table, and a ``trailer``.
"""

from __future__ import annotations

import zlib
from typing import Dict, List, Tuple

from .goodnotes import GoodNotesDocument, Stroke

CREATOR = "goodparse"

# Floor mirroring xournal.py/excalidraw.py: a zero-width point still renders.
_MIN_WIDTH = 0.1


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


def _page_content(page, width_scale: float, alpha_name: Dict[float, str]) -> str:
    """Build the PDF content stream (text) for one page, Y-flipped."""
    h = page.height
    out = ["J 1", "j 1"]  # round line cap, round line join
    for stroke in page.strokes:
        r, g, b, a = stroke.color
        out.append(f"{_num(r)} {_num(g)} {_num(b)} RG")
        if a < 1.0:
            out.append(f"/{alpha_name[a]} gs")
        pts = _stroke_points(stroke)
        widths = [max(_MIN_WIDTH, w * width_scale) for _x, _y, w in pts]
        for k in range(len(pts) - 1):
            w = (widths[k] + widths[k + 1]) / 2.0
            x0, y0, _w0 = pts[k]
            x1, y1, _w1 = pts[k + 1]
            out.append(f"{_num(w)} w")
            out.append(f"{_num(x0, 2)} {_num(h - y0, 2)} m")
            out.append(f"{_num(x1, 2)} {_num(h - y1, 2)} l")
            out.append("S")
    return "\n".join(out) + "\n"


def build_pdf(doc: GoodNotesDocument, width_scale: float = 1.0) -> bytes:
    """Render the document model to a minimal valid PDF 1.4 byte string."""
    pages = doc.pages
    n_pages = len(pages)

    # Unique non-opaque alphas across the whole doc -> shared ExtGState objects.
    alphas = sorted({
        round(s.color[3], 4)
        for page in pages for s in page.strokes if s.color[3] < 1.0
    })
    # Resource name per alpha (GS0, GS1, ...) keyed by the rounded alpha value.
    alpha_name = {a: f"GS{i}" for i, a in enumerate(alphas)}

    # Fixed object layout.
    CATALOG = 1
    PAGES = 2
    page_obj = lambda i: 3 + 2 * i          # 3, 5, 7, ...
    content_obj = lambda i: 4 + 2 * i       # 4, 6, 8, ...
    extg_first = 3 + 2 * n_pages
    extg_obj = lambda j: extg_first + j     # one per unique alpha

    # ExtGState resource dict, shared by every page.
    if alphas:
        extg_res = " ".join(f"/GS{j} {extg_obj(j)} 0 R" for j in range(len(alphas)))
        resources = f"<< /ExtGState << {extg_res} >> >>"
    else:
        resources = "<< >>"

    # Build every object's body first (num, payload) in final order.
    objs: List[Tuple[int, bytes]] = [
        (CATALOG, b"<< /Type /Catalog /Pages 2 0 R >>"),
        (PAGES, f"<< /Type /Pages /Kids [{' '.join(f'{page_obj(i)} 0 R' for i in range(n_pages))}] /Count {n_pages} >>".encode()),
    ]
    for i, page in enumerate(pages):
        objs.append((page_obj(i), (
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {_num(page.width, 2)} {_num(page.height, 2)}] "
            f"/Resources {resources} "
            f"/Contents {content_obj(i)} 0 R >>"
        ).encode()))
        stream = _page_content(page, width_scale, alpha_name).encode("latin-1")
        compressed = zlib.compress(stream)
        objs.append((content_obj(i),
                     f"<< /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n".encode()
                     + compressed + b"\nendstream"))
    for j, a in enumerate(alphas):
        objs.append((extg_obj(j), f"<< /ca {_num(a)} /CA {_num(a)} >>".encode()))

    # Serialize: record each object's byte offset, then emit the xref table.
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: Dict[int, int] = {}
    for num, payload in objs:
        offsets[num] = len(body)
        body.extend(f"{num} 0 obj\n".encode("latin-1"))
        body.extend(payload)
        body.extend(b"\nendobj\n")

    # xref table: entry 0 (free) + one per object, each exactly 20 bytes.
    # Objects are numbered 1..max(offsets), so there are len(offsets) objects
    # plus the free entry 0 -> size = len(offsets) + 1.
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
