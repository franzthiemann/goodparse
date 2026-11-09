"""Parse GoodNotes ``.goodnotes`` archives into a simple drawing model.

Format (reverse-engineered; see README for the full write-up):

* A ``.goodnotes`` file is a ZIP archive. Each ``notes/<UUID>`` member is one
  page, stored as length-delimited protobuf records. Records alternate between
  a small metadata record and a content record whose field ``#7`` carries a
  pen or highlighter stroke.
* In a stroke's ``#7`` message: field ``#2`` is the Apple-LZ4 geometry blob and
  field ``#4`` is the RGBA colour (float32 sub-fields ``1=R 2=G 3=B 4=A``).
* Pen geometry (field ``#21 == 24``) holds float32 ``(x, y, pressure)`` points
  at stride 12.  Highlighter / eraser geometry (``#21 == 25``) is a smooth
  curve: a style template, a long run of 0/1 flags, then ``[x0][y0][u32
  count][count * (x, y)]`` float32 pairs.

Coordinate space.  Strokes, images and text boxes are stored in the document's
*canvas* space.  The exported page is a viewport of that canvas.  The
canvas-to-page mapping is a uniform scale ``page_size / canvas_size`` about
the origin (0, 0).  The scale is recovered from ``index.events.pb`` (which
stores the canvas dimensions as float32) and the paper PDF's MediaBox (which
stores the page size).  When either is absent the scale falls back to 1.0.

Images.  A page's embedded raster lives in a top-level record whose field
``#4`` is the attachment UUID; the matching ``attachments/<UUID>`` zip member
holds the PNG/JPEG bytes.  A companion record carries a placement matrix in
field ``#1``: two nested messages (``f2`` and ``f3``), each holding two
``(x, y)`` float32 points.  ``f2.f1`` is the top-left corner (canvas space);
``f2.f2`` is the (width, height) size vector.

Text boxes.  Stored as a top-level record whose field ``#8`` is a nested
message: ``f2`` = the placement matrix (top-left + size), one field is the
RTF source (Cocoa RTF), and another is the RGBA colour.
"""

from __future__ import annotations

import os
import re
import struct
import warnings
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .applelz4 import apple_decompress, is_apple_lz4
from .ink import TemplateError, decode_ink
from .protobuf import (
    get_bytes,
    iter_fields,
    iter_fields_tolerant,
    parse_message_tolerant,
    read_length_delimited_records,
)
from .records import (
    KIND_FILL,
    KIND_IMAGE,
    KIND_STROKE,
    KIND_TEXT,
    KIND_TEXTBOX,
    PageObject,
    read_page_objects,
)
from .richtext import decode_text_object
from .shapes import decode_outline, outline_width
from .text_rtf import TextLine, parse_rtf_runs

# A4 in PDF points (72 dpi) — fallback for documents without an explicit page
# size (samples 1-4 are A4).
A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89

# Shape outline width (canvas units) when f9.f15 is absent: the value every
# shape in the samples carries.
_SHAPE_DEFAULT_WIDTH = 10.3937

# GoodNotes draws pencil as a grain texture, not flat ink.  Flat-colour output
# approximates its average coverage with this opacity: a single pencil pass
# covers 4-8 % of a solid stroke in the references (Test8, Test_11), dense
# self-overlapping scribbles 30-40 % (Test6), because the texture accumulates.
PENCIL_OPACITY = 0.2


@dataclass
class Stroke:
    """One pen, highlighter, or eraser stroke.

    ``points`` is a list of ``(x, y, width)`` triples in canvas space.
    ``kind`` is ``"pen"`` or ``"pencil"`` (a textured stroke whose colour
    already carries the pencil opacity approximation).
    ``color`` is RGBA (0..1).  Eraser strokes are always white.
    ``fill`` is the RGBA interior colour of a filled (closed) shape, else None;
    writers paint it underneath the outline.
    """

    points: List[Tuple[float, float, float]]  # (x, y, width)
    color: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    kind: str = "pen"
    fill: Optional[Tuple[float, float, float, float]] = None

    @property
    def is_dot(self) -> bool:
        return len(self.points) <= 1 or _bbox_diagonal(self.points) < 1.0


@dataclass
class Image:
    """One embedded raster placed on the page.

    ``position`` is the top-left corner in canvas space; ``size`` is
    ``(width, height)`` in canvas space.  ``data`` is the raw raster bytes.
    """

    position: Tuple[float, float]
    size: Tuple[float, float]
    data: bytes
    fmt: str = "png"

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        x, y = self.position
        w, h = self.size
        return x, y, x + w, y + h


@dataclass
class TextBox:
    """One text box with RTF content.

    ``lines`` holds the styled run breakdown (per-line, per-run bold/italic/
    underline/strike/colour) parsed from the RTF; exporters use it to render
    formatting, while ``plain``/``rtf`` are kept for simple consumers.
    """

    position: Tuple[float, float]  # top-left, canvas space
    size: Tuple[float, float]      # (w, h), canvas space
    rtf: str
    plain: str
    color: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    lines: List["TextLine"] = field(default_factory=list)
    # Field-21 sticker text carries an explicit *canvas*-unit size (still
    # scaled by ``s`` at emit time).  0 = not present -> the emitter falls back
    # to guessing the size from the RTF (the field-8 text-box path).
    font_size: float = 0.0


@dataclass
class Page:
    strokes: List[Stroke] = field(default_factory=list)  # in paint order
    images: List[Image] = field(default_factory=list)
    texts: List[TextBox] = field(default_factory=list)
    width: float = A4_WIDTH_PT
    height: float = A4_HEIGHT_PT
    scale: float = 1.0  # canvas → page uniform scale
    # Raw bytes of this page's background ("paper") PDF, or None.  Background
    # PDFs are stored as attachments and define the page size.
    background: Optional[bytes] = None


@dataclass
class GoodNotesDocument:
    pages: List[Page] = field(default_factory=list)
    title: str = "GoodNotes"
    canvas: Optional[Tuple[float, float]] = None  # (w, h) in points


# --------------------------------------------------------------------------- #
# Geometry decoding
# --------------------------------------------------------------------------- #

def _bbox_diagonal(points) -> float:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5


def _stroke_origin(content_fields) -> Optional[Tuple[float, float]]:
    """Read an object's translation from field ``#6`` (``{1: dx, 2: dy}``).

    Moving a stroke or shape in GoodNotes does not rewrite its geometry; it
    stores the move as this translation, which is *added* to every stored
    point (Test_11 "Moved" page).  Objects never moved carry an empty ``#6``.
    """
    blobs = content_fields.get(6)
    if not blobs:
        return None
    if not isinstance(blobs[0], (bytes, bytearray)):
        return None
    try:
        vals = {fno: val for fno, _wt, val in iter_fields(blobs[0])}
    except (ValueError, struct.error):
        return None
    x, y = vals.get(1), vals.get(2)
    if isinstance(x, float) and isinstance(y, float):
        return x, y
    return None


def _rgba(msg) -> Optional[Tuple[float, float, float, float]]:
    """Decode an RGBA message (float32 sub-fields ``1..4``).

    An omitted R/G/B is 0.0; an omitted alpha is taken as opaque.
    """
    if not isinstance(msg, (bytes, bytearray)):
        return None
    color = [0.0, 0.0, 0.0, 1.0]
    for sub_field, _wt, val in iter_fields_tolerant(bytes(msg)):
        if 1 <= sub_field <= 4 and isinstance(val, float):
            color[sub_field - 1] = val
    return (color[0], color[1], color[2], color[3])


def _extract_color(content_fields) -> Tuple[float, float, float, float]:
    """Read a stroke's RGBA colour from field ``#4`` (default opaque black)."""
    return _rgba(content_fields.get(4, [None])[0]) or (0.0, 0.0, 0.0, 1.0)


def _find_geometry_blob(content_fields) -> Optional[bytes]:
    for chunk in content_fields.get(2, []):
        if isinstance(chunk, (bytes, bytearray)) and is_apple_lz4(chunk):
            return bytes(chunk)
    return None


# --------------------------------------------------------------------------- #
# Placement matrix helpers (shared by images and text boxes)
# --------------------------------------------------------------------------- #

def _point_xy(blob: bytes) -> Optional[Tuple[float, float]]:
    """Parse a point message: ``{1: f32 x, 2: f32 y}``."""
    d = {}
    try:
        for fno, _wt, v in iter_fields(blob):
            d[fno] = v
    except (ValueError, struct.error):
        return None
    if isinstance(d.get(1), float) and isinstance(d.get(2), float):
        return (d[1], d[2])
    return None


def _matrix_from(msg: bytes) -> Optional[Tuple[Tuple[float, float], Tuple[float, float]]]:
    """Parse the 2-point placement matrix from nested field ``#2``.

    Field ``#2`` is a message holding two point messages (sub-fields 1 and 2).
    Returns ``(point1, point2)`` — for images/text: (top-left, size).
    """
    f2 = get_bytes(msg, 2)
    if not f2:
        return None
    pts = []
    for pno in (1, 2):
        p = get_bytes(f2, pno)
        if p:
            xy = _point_xy(p)
            if xy:
                pts.append(xy)
    if len(pts) >= 2:
        return pts[0], pts[1]
    return None


def _raster_format(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:2] == b"\xff\xd8":
        return "jpeg"
    return "png"


def _rtf_to_plain(rtf: bytes) -> str:
    """Best-effort RTF → display text (strips control words, keeps text).

    Handles the common Cocoa RTF subset: drops the ``\\fonttbl`` /
    ``\\colortbl`` / ``\\stylesheet`` table groups, maps ``\\par``/``\\n`` to
    newlines, and removes every other control word.  Not a full RTF parser.
    """
    s = rtf.decode("cp1252", errors="replace")
    # drop the table groups ({\fonttbl ...}, {\colortbl ...}, ...) by finding
    # the opening brace *before* the marker and matching it
    for tbl in ("fonttbl", "colortbl", "expandedcolortbl", "stylesheet"):
        marker = "\\" + tbl
        i = s.find(marker)
        while i != -1:
            # opening brace is the one immediately preceding the marker
            j = s.rfind("{", 0, i)
            if j == -1:
                break
            depth = 0
            k = j
            while k < len(s):
                if s[k] == "{":
                    depth += 1
                elif s[k] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                k += 1
            if k < len(s):
                s = s[:j] + s[k + 1:]
            else:
                break
            i = s.find(marker)
    s = re.sub(r"\\'([0-9a-fA-F]{2})", "", s)

    def _sub(m):
        word = m.group(1)
        if word in ("n", "par"):
            return "\n"
        return ""

    s = re.sub(r"\\([a-zA-Z]+)(-?\d*) ?", _sub, s)
    s = s.replace("{", "").replace("}", "")
    s = s.replace("\\~", " ").replace("\\*", "*")
    lines = [ln.strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


# --------------------------------------------------------------------------- #
# Page / archive parsing
# --------------------------------------------------------------------------- #

def _parse_stroke(cf) -> List[Stroke]:
    """Build the stroke(s) of a kind-7 object (one per sub-path).

    A shape-recognised stroke keeps its exact geometry in ``f9`` (its ``f2``
    blob is then a bare style template); a freehand stroke has an empty
    ``f9`` and its path in the ``f2`` blob (see :mod:`goodparse.ink`).  Either
    way the ``f6`` translation of a moved object is added to every point.
    """
    color = _extract_color(cf)
    dx, dy = _stroke_origin(cf) or (0.0, 0.0)

    f9 = cf.get(9, [b""])[0]
    if isinstance(f9, (bytes, bytearray)) and f9:
        outline = decode_outline(bytes(f9))
        if outline:
            w = outline_width(bytes(f9)) or _SHAPE_DEFAULT_WIDTH
            return [Stroke(points=[(x + dx, y + dy, w) for x, y in outline.points],
                           color=color, kind="pen")]

    blob = _find_geometry_blob(cf)
    if blob is None:
        return []
    try:
        ink = decode_ink(apple_decompress(blob))
    except TemplateError as exc:
        warnings.warn(f"skipping undecodable stroke: {exc}")
        return []
    if ink is None:
        return []
    kind = "pen"
    if ink.kind == "pencil":
        kind = "pencil"
        color = (color[0], color[1], color[2], color[3] * PENCIL_OPACITY)
    return [Stroke(points=[(x + dx, y + dy, w) for x, y, w in path],
                   color=color, kind=kind)
            for path in ink.subpaths]


def parse_page(data: bytes, attachments: Dict[str, bytes],
              canvas: Optional[Tuple[float, float]],
              page_size: Optional[Tuple[float, float]]) -> Page:
    """Parse one ``notes/<UUID>`` page file into a :class:`Page`.

    Only live objects are drawn (see :mod:`goodparse.records`): deleted ones
    stay in the file with a tombstone flag.
    """
    page = Page()
    if page_size:
        page.width, page.height = page_size
    if canvas and page_size:
        page.scale = page_size[0] / canvas[0]

    by_id: Dict[str, Stroke] = {}
    fills: List[PageObject] = []
    for obj in read_page_objects(data):
        if obj.kind == KIND_STROKE:
            strokes = _parse_stroke(obj.fields)
            page.strokes.extend(strokes)
            if strokes:
                by_id[obj.id] = strokes[0]
        elif obj.kind == KIND_FILL:
            fills.append(obj)
        elif obj.kind == KIND_IMAGE:
            img = _parse_image(obj, attachments)
            if img:
                page.images.append(img)
        elif obj.kind == KIND_TEXTBOX:
            box = _parse_textbox(obj.body)
            if box:
                page.texts.append(box)
        elif obj.kind == KIND_TEXT:
            box = _parse_text_object(obj.body)
            if box:
                page.texts.append(box)
    for obj in fills:
        _apply_fill(obj, by_id)
    # The file lists strokes topmost-first; expose them in paint order
    # (bottom-most first) so every writer can simply draw them in sequence.
    page.strokes.reverse()
    return page


def _apply_fill(obj: PageObject, strokes: Dict[str, "Stroke"]) -> None:
    """Attach a fill object's colour to the shape outline it fills.

    A fill (body kind 9) references its outline stroke by UUID in ``f5`` and
    stores the fill RGBA in ``f7``; its ``f4`` repeats the outline geometry, so
    the outline's points already describe the filled area.
    """
    target = obj.fields.get(5, [None])[0]
    if not isinstance(target, (bytes, bytearray)):
        return
    stroke = strokes.get(bytes(target).decode("ascii", "ignore").strip())
    if stroke is None:
        return
    rgba = _rgba(obj.fields.get(7, [None])[0])
    if rgba is not None:
        stroke.fill = rgba


def _parse_image(obj: PageObject, attachments: Dict[str, bytes]) -> Optional[Image]:
    """Image object: attachment UUID in the header, placement matrix in the body."""
    data = attachments.get(obj.attachment or "")
    m = _matrix_from(obj.body)
    if not data or not m:
        return None
    pos, size = m
    return Image(position=pos, size=size, data=data, fmt=_raster_format(data))


def _parse_textbox(f8: bytes) -> Optional[TextBox]:
    """Field-#8 text box: placement matrix plus a Cocoa RTF document."""
    m = _matrix_from(f8)
    if not m:
        return None
    top_left, size_vec = m
    rtf = None
    for _k, _wt, v in iter_fields_tolerant(f8):
        if isinstance(v, (bytes, bytearray)) and b"\\rtf1" in v:
            rtf = bytes(v)
    if rtf is None:
        return None
    lines = parse_rtf_runs(rtf)
    plain = "\n".join(ln.text().strip() for ln in lines if ln.text().strip())
    # The box colour is the first run's RTF colour (an empty \colortbl = black).
    color = next((ln.runs[0].color for ln in lines if ln.runs), (0.0, 0.0, 0.0, 1.0))
    return TextBox(position=top_left, size=size_vec,
                   rtf=rtf.decode("cp1252", "replace"),
                   plain=plain, color=color, lines=lines)


def _parse_text_object(f21: bytes) -> Optional[TextBox]:
    """Kind-21 text object (see :mod:`goodparse.richtext`)."""
    obj = decode_text_object(f21)
    if obj is None:
        return None
    plain = "\n".join(ln.text() for ln in obj.lines)
    color = obj.lines[0].runs[0].color
    size = max((r.size or obj.font_size) for ln in obj.lines for r in ln.runs)
    return TextBox(position=obj.position, size=obj.size, rtf="", plain=plain,
                   color=color, lines=obj.lines, font_size=size)


def _page_members(names) -> List[str]:
    return sorted(n for n in names if "/notes/" in n or n.startswith("notes/"))


def _read_deleted_pages(opener, names) -> List[str]:
    """Return the full 36-char ``notes/`` member names of *deleted* pages.

    ``index.events.pb`` is the op log.  A page is created by an op carrying
    field #54 and later deleted/hidden by an op carrying field #56 (the same
    page UUID).  Deleted members are kept in the archive but must not produce
    a page (the reference export omits them).  Note the op log stores an
    *internal* page id that differs from the member name by one nibble in the
    final hex byte, so matching is on the first 32 chars.
    """
    if "index.events.pb" not in names:
        return []
    try:
        data = opener("index.events.pb")
    except Exception:
        return []
    deleted = set()
    for rec in read_length_delimited_records(data):
        try:
            top = parse_message_tolerant(rec)
        except Exception:
            continue
        f1 = top.get(1, [None])[0]
        if not isinstance(f1, (bytes, bytearray)):
            continue
        fields = {k for k in top if k != 1}
        if 56 in fields:
            deleted.add(bytes(f1).decode("ascii", "ignore").strip()[:32])
    if not deleted:
        return []
    out = []
    for n in names:
        if "/notes/" in n or n.startswith("notes/"):
            if n.split("/")[-1][:32] in deleted:
                out.append(n)
    return out


def _read_page_order(opener, names, fallback) -> List[str]:
    """Return page member names in document order.

    ``index.notes.pb`` lists the pages in their display order (each record's
    field #1 is a page UUID); we map those to ``notes/<UUID>`` members.  When
    the index is absent we fall back to the sorted member list.
    """
    if "index.notes.pb" not in names:
        return list(fallback)
    try:
        data = opener("index.notes.pb")
    except Exception:
        return list(fallback)
    nameset = set(names)
    ordered = []
    for rec in read_length_delimited_records(data):
        top = parse_message_tolerant(rec)
        if top.get(1) and isinstance(top[1][0], (bytes, bytearray)):
            member = "notes/" + bytes(top[1][0]).decode("ascii", "ignore").strip()
            if member in nameset and member not in ordered:
                ordered.append(member)
    for n in fallback:  # keep any pages missing from the index (defensive)
        if n not in ordered:
            ordered.append(n)
    return ordered


def _read_page_backgrounds(opener, names) -> Dict[str, str]:
    """Map page member UUID -> background paper attachment UUID.

    ``index.events.pb`` is an operation log.  A page is *created* by an op whose
    field #1 is the page UUID and which carries field #54; a *paper* is applied
    by an op whose field #1 is the paper (background PDF) UUID and which carries
    field #105.  Creations and paper-assignments are interleaved in document
    order, so we record both sequences and zip them: the i-th created page gets
    the i-th assigned paper, and a page with no explicit assignment inherits the
    most recent paper (a freshly created page keeps the current paper).  When the
    events file is absent we return an empty mapping (no backgrounds).
    """
    if "index.events.pb" not in names:
        return {}
    member_names = _page_members(names)
    try:
        data = opener("index.events.pb")
    except Exception:
        return {}
    creations: List[str] = []   # page UUIDs, creation order
    papers: List[str] = []      # paper UUIDs, assignment order
    for rec in read_length_delimited_records(data):
        top = parse_message_tolerant(rec)
        f1 = top.get(1, [None])[0]
        if not isinstance(f1, (bytes, bytearray)):
            continue
        uuid = bytes(f1).decode("ascii", "ignore").strip()
        if not uuid or uuid.count("-") != 4:
            continue
        fields = {k for k in top if k != 1}
        if 54 in fields:
            if uuid not in creations:
                creations.append(uuid)
        elif 105 in fields:
            papers.append(uuid)
    if not papers:
        return {}
    # NOTE: the page-creation ops store an *internal* page-object id that differs
    # from the ``notes/`` member name by exactly one nibble in the final hex byte
    # (e.g. ``...EC7A`` vs member ``...EC7B``).  The first 32 chars are identical,
    # so we join on that prefix.
    def p32(u):
        return u[:32]

    member_prefix = {p32(name.split("/")[-1]): name.split("/")[-1]
                     for name in member_names}
    bg: Dict[str, str] = {}
    last = None
    for i, created in enumerate(creations):
        if i < len(papers):
            last = papers[i]
        if last:
            member = member_prefix.get(p32(created))
            if member:
                bg[member] = last
    return bg


def _read_page_members(opener, names, attachments, canvas,
                       page_size) -> List[Page]:
    """Parse every ``notes/<UUID>`` member, in document order, keeping empty
    pages (a page may hold only a background).

    Per-page geometry (size + scale) is read from the event log: in current
    GoodNotes builds each page has its own canvas (``f54`` -> layer ->
    create-info ``f8``), so a single global page size is wrong for documents
    mixing page sizes (A4 + landscape + strips).  Old builds, which carry no
    such per-page canvas, fall back to the global ``canvas``/``page_size``.
    """
    member_names = _page_members(names)
    meta = _read_page_meta(opener, names, member_names)

    # bare-member-name (no "notes/" prefix) -> (canvas, paper)
    per_page = {m.split("/")[-1]: (c, p) for m, _p, c, p in meta}

    order: List[str] = []
    if meta:
        for m, _layer, _c, _p in meta:
            if m and m not in order:
                order.append(m)
    for m in _read_page_order(opener, names, member_names):  # defensive fill
        if m not in order:
            order.append(m)

    bg_map = _read_page_backgrounds(opener, names)
    deleted = set(_read_deleted_pages(opener, names))
    paper_att = _read_paper_to_attachment(opener, names)

    def resolve_paper(paper):
        if not paper:
            return None
        # a paper UUID may be an internal id; map to its real attachment
        return paper_att.get(paper, paper)

    pages = []
    for name in order:
        if name in deleted:
            continue  # page was deleted; keep it out of the output
        member = name.split("/")[-1]
        data = opener(name)
        p_canvas, p_paper = per_page.get(member, (None, None))
        if p_canvas:
            ps = _paper_size(opener, names, resolve_paper(p_paper))
            page = parse_page(data, attachments, p_canvas, ps)
        else:
            page = parse_page(data, attachments, canvas, page_size)
        paper = resolve_paper(p_paper) or resolve_paper(bg_map.get(member))
        if paper and paper in attachments:
            page.background = attachments[paper]
        pages.append(page)
    return pages


_UUID_RE = re.compile(
    rb"([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})")


def _uuid_from(v) -> Optional[str]:
    if not isinstance(v, (bytes, bytearray)):
        return None
    m = _UUID_RE.search(bytes(v))
    return m.group(1).decode("ascii").upper() if m else None


def _paper_size(opener, names, paper) -> Optional[Tuple[float, float]]:
    """Page size from a paper PDF attachment's MediaBox ``[0 0 W H]``."""
    if not paper or f"attachments/{paper}" not in names:
        return None
    try:
        data = opener(f"attachments/{paper}")
    except Exception:
        return None
    if not data.startswith(b"%PDF"):
        return None
    m = re.search(
        rb"/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]", data)
    if not m:
        m = re.search(rb"/MediaBox\s*\[0\s+0\s+([\d.]+)\s+([\d.]+)\]", data)
        if m:
            return (float(m.group(1)), float(m.group(2)))
        return None
    return (float(m.group(3)), float(m.group(4)))


def _read_page_meta(opener, names, member_names) -> List[Tuple[str, str,
                                                                Optional[Tuple[float, float]], Optional[str]]]:
    """Derive per-page metadata from ``index.events.pb``.

    Returns ``[(member_name, layer, canvas_wh_or_None, paper_uuid_or_None)]`` in
    display order.  Each entry resolves a page's ``f54`` create op (page UUID in
    field 2, layer in field 3) to that layer's create-info op (top field 1 =
    layer, nested field ``f2.f4`` = paper, ``f2.f8`` = canvas dims).  When the
    event log is absent or carries no canvas dims this returns ``[]`` and the
    caller falls back to the single global canvas/page size.
    """
    if "index.events.pb" not in names:
        return []
    try:
        data = opener("index.events.pb")
    except Exception:
        return []
    try:
        recs = list(read_length_delimited_records(data))
    except Exception:
        return []

    # 1) layer create-info ops: top f1 = layer UUID, f2 = info message.
    layer_info: Dict[str, Tuple[Optional[str], Optional[Tuple[float, float]]]] = {}
    for rec in recs:
        t = parse_message_tolerant(rec)
        if 1 not in t or 2 not in t:
            continue
        layer = _uuid_from(t[1][0])
        info = t[2][0]
        if not layer or not isinstance(info, (bytes, bytearray)):
            continue
        paper: Optional[str] = None
        cxy: Optional[Tuple[float, float]] = None
        for f, _wt, v in iter_fields_tolerant(info):
            if f == 4:
                paper = _uuid_from(v) or paper
            elif f == 8 and isinstance(v, (bytes, bytearray)):
                cf = parse_message_tolerant(v)
                if 1 in cf and 2 in cf:
                    w, h = cf[1][0], cf[2][0]
                    if 100 < w < 5000 and 100 < h < 5000:
                        cxy = (float(w), float(h))
        if cxy:
            layer_info[layer] = (paper, cxy)
        elif paper:
            # paper known but no canvas yet; keep the best (canvas) if seen
            prev = layer_info.get(layer)
            if not prev or not prev[1]:
                layer_info[layer] = (paper, prev[1] if prev else None)

    # 2) pages: f54 create ops (page UUID f2, layer f3, position key f4).
    #    Display order is NOT creation order: each page carries a
    #    fractional-index position key (``f4 = {1: key, 2: {1: version}}``,
    #    e.g. "4mgFG1" < "Am5Pm" < "L2FtP" < "O9t!N"), and moving a page
    #    rewrites it through an f55 update op (``f3`` = new key, same layout).
    #    Pages sort by their latest key, bytewise.
    member_prefix = {p32: m for p32, m in
                     ((m[-36:][:32], m) for m in member_names)}
    created: List[Tuple[str, Optional[str]]] = []  # (page UUID, layer)
    position: Dict[str, Tuple[int, bytes]] = {}    # page -> (version, key)
    for rec in recs:
        t = parse_message_tolerant(rec)
        for op, key_field in ((54, 4), (55, 3)):
            if op not in t or not isinstance(t[op][0], (bytes, bytearray)):
                continue
            body = parse_message_tolerant(t[op][0])
            page = _uuid_from(body.get(2, [None])[0])
            if not page:
                continue
            if op == 54:
                created.append((page, _uuid_from(body.get(3, [None])[0])))
            pk = _position_key(body.get(key_field, [None])[0])
            if pk and pk[0] >= position.get(page, (-1, b""))[0]:
                position[page] = pk
    out: List[Tuple[str, str, Optional[Tuple[float, float]], Optional[str]]] = []
    for page, layer in created:
        member = member_prefix.get(page[:32])
        paper, cxy = layer_info.get(layer, (None, None)) if layer else (None, None)
        out.append((member, layer, cxy, paper))
    # Stable sort: pages without a key keep their creation order at the front.
    order = {page: position.get(page, (0, b""))[1] for page, _l in created}
    keyed = sorted(range(len(out)), key=lambda i: order[created[i][0]])
    return [out[i] for i in keyed]


def _position_key(msg) -> Optional[Tuple[int, bytes]]:
    """Decode a page position ``{1: key bytes, 2: {1: version}}``."""
    if not isinstance(msg, (bytes, bytearray)):
        return None
    f = parse_message_tolerant(bytes(msg))
    key = f.get(1, [None])[0]
    if not isinstance(key, (bytes, bytearray)) or not key:
        return None
    ver = f.get(2, [None])[0]
    version = 0
    if isinstance(ver, (bytes, bytearray)):
        v = parse_message_tolerant(bytes(ver)).get(1, [0])[0]
        version = v if isinstance(v, int) else 0
    return version, bytes(key)


def _collect_attachments(opener, names) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    for n in names:
        if n.startswith("attachments/"):
            data = opener(n)
            if data:
                out[n.split("/")[-1]] = data
    return out


def _read_paper_to_attachment(opener, names) -> Dict[str, str]:
    """Map an *internal* paper UUID to its backing ``attachments/`` UUID.

    Most papers are stored under their own UUID, but some (e.g. imported or
    template papers) have an internal id that differs from the attachment
    filename.  An op whose field #1 is the paper UUID and whose field #6
    sub-message's field #2 is the attachment UUID records that mapping.
    Papers that map to themselves are the common case; the mapping is only
    needed to resolve the rest.
    """
    if "index.events.pb" not in names:
        return {}
    try:
        data = opener("index.events.pb")
    except Exception:
        return {}
    out: Dict[str, str] = {}
    try:
        recs = list(read_length_delimited_records(data))
    except Exception:
        return {}
    for rec in recs:
        t = parse_message_tolerant(rec)
        if 1 not in t or 6 not in t:
            continue
        paper = _uuid_from(t[1][0])
        v6 = t[6][0]
        if not paper or not isinstance(v6, (bytes, bytearray)):
            continue
        sub = parse_message_tolerant(v6)
        if 2 in sub:
            att = _uuid_from(sub[2][0])
            if att:
                out[paper] = att
    return out


def _read_page_size(opener, names) -> Optional[Tuple[float, float]]:
    """Recover the page point size from a paper PDF attachment.

    Paper PDFs are stored in ``attachments/<UUID>`` with no extension; they
    start with ``%PDF`` and carry a ``/MediaBox [0 0 W H]``.  When all paper
    PDFs share the same size we use it; otherwise we fall back to A4.
    """
    import re as _re
    sizes = set()
    for n in names:
        if not n.startswith("attachments/"):
            continue
        data = opener(n)
        if not data or not data.startswith(b"%PDF"):
            continue
        m = _re.search(rb"/MediaBox\s*\[0\s+0\s+([\d.]+)\s+([\d.]+)\]", data)
        if m:
            sizes.add((float(m.group(1)), float(m.group(2))))
    if len(sizes) == 1:
        return sizes.pop()
    return None


def _read_canvas_size(opener, names,
                      page_size: Optional[Tuple[float, float]]
                      ) -> Optional[Tuple[float, float]]:
    """Recover the canvas point size from ``index.events.pb``.

    The event log stores the canvas width and height as float32 fields
    (834.24 × 1078.82 for sample 5), nested deep in protobuf messages.  A raw
    f32 byte-scan is noisy, so we disambiguate with the *known* page size:
    the canvas and the page share the same aspect ratio, so the true
    ``(canvas_w, canvas_h)`` pair is the one for which
    ``page_w / canvas_w == page_h / canvas_h`` (a uniform viewport scale).
    Returns ``None`` when no such pair exists (samples 1-4 have no events
    file, and their canvas is the page itself, scale 1.0).
    """
    if "index.events.pb" not in names or not page_size:
        return None
    try:
        data = opener("index.events.pb")
    except Exception:
        return None
    pw, ph = page_size
    vals = set()
    for i in range(0, len(data) - 3):
        v = struct.unpack_from("<f", data, i)[0]
        if 200.0 < v < 5000.0:  # plausible note-canvas dimension in points
            vals.add(round(v, 1))
    best = None
    for w in vals:
        for h in vals:
            if w >= h * 2.0 or h >= w * 2.0:
                continue
            sx = pw / w
            sy = ph / h
            if 0.1 <= sx <= 3.0 and abs(sx - sy) < 1e-3:
                # prefer the largest canvas (smallest scale) among matches
                if best is None or w * h > best[0] * best[1]:
                    best = (w, h)
    if best:
        return best
    return None


def parse_goodnotes(path: str) -> GoodNotesDocument:
    """Parse a ``.goodnotes`` archive (or an already-extracted directory)."""
    title = os.path.splitext(os.path.basename(path.rstrip("/")))[0]

    if os.path.isdir(path):
        names = []
        for root, _dirs, files in os.walk(path):
            for f in files:
                names.append(os.path.relpath(os.path.join(root, f), path))

        def opener(name):
            with open(os.path.join(path, name), "rb") as fh:
                return fh.read()

        attachments = _collect_attachments(opener, names)
        page_size = _read_page_size(opener, names)
        canvas = _read_canvas_size(opener, names, page_size)
        pages = _read_page_members(opener, names, attachments, canvas, page_size)
        return GoodNotesDocument(pages=pages, title=title, canvas=canvas)
    else:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            attachments = _collect_attachments(zf.read, names)
            page_size = _read_page_size(zf.read, names)
            canvas = _read_canvas_size(zf.read, names, page_size)
            pages = _read_page_members(zf.read, names, attachments, canvas, page_size)
            return GoodNotesDocument(pages=pages, title=title, canvas=canvas)
