"""End-to-end tests against the reverse-engineered sample files."""

import gzip
import json
import os
import re
import zlib
import xml.etree.ElementTree as ET

import pytest

from goodparse import convert_file, parse_goodnotes
from goodparse.applelz4 import apple_decompress, lz4_block_decompress
from goodparse.excalidraw import color_to_rgb_hex
from goodparse.goodnotes import GoodNotesDocument, Page, Stroke, extract_points
from goodparse.pdf import build_pdf
from goodparse.xournal import color_to_hex

SAMPLES = os.path.join(os.path.dirname(__file__), os.pardir, "samples")


def sample(name):
    return os.path.join(SAMPLES, name)


# --------------------------------------------------------------------------- #
# Unit-level: decoders
# --------------------------------------------------------------------------- #

def test_lz4_block_roundtrip_literals_only():
    # token 0x50 = 5 literals, 0 match -> "hello"
    assert lz4_block_decompress(b"\x50hello") == b"hello"


def test_apple_lz4_uncompressed_chunk():
    payload = b"abcdef"
    blob = b"bv4-" + len(payload).to_bytes(4, "little") + payload + b"bv4$"
    assert apple_decompress(blob) == payload


def test_color_to_hex():
    assert color_to_hex((1.0, 0.0, 0.0, 1.0)) == "#ff0000ff"
    assert color_to_hex((0.0, 0.4784, 1.0, 1.0)) == "#007affff"


# --------------------------------------------------------------------------- #
# Integration: parsing samples
# --------------------------------------------------------------------------- #

def test_test2_single_dot():
    doc = parse_goodnotes(sample("test2.goodnotes"))
    strokes = [s for p in doc.pages for s in p.strokes]
    assert len(strokes) == 1
    x, y, _w = strokes[0].points[0]
    assert x == pytest.approx(484.3, abs=1.0)
    assert y == pytest.approx(362.7, abs=1.0)


def test_test3_two_dots():
    doc = parse_goodnotes(sample("test3.goodnotes"))
    strokes = [s for p in doc.pages for s in p.strokes]
    assert len(strokes) == 2


def test_test1_line_geometry():
    # test1 is an already-extracted directory, not a zip
    doc = parse_goodnotes(sample("test1"))
    strokes = [s for p in doc.pages for s in p.strokes]
    line = max(strokes, key=lambda s: len(s.points))
    assert len(line.points) >= 10
    first, last = line.points[0], line.points[-1]
    assert first[0] == pytest.approx(505, abs=3) and first[1] == pytest.approx(438, abs=3)
    assert last[0] == pytest.approx(486, abs=3) and last[1] == pytest.approx(712, abs=3)


def test_test4_five_colored_strokes():
    doc = parse_goodnotes(sample("Test4.goodnotes"))
    strokes = [s for p in doc.pages for s in p.strokes]
    assert len(strokes) == 5
    colors = {color_to_hex(s.color) for s in strokes}
    expected = {"#d20000ff", "#007affff", "#f59a23ff", "#007355ff", "#ff9797ff"}
    assert colors == expected
    # every stroke must recover its full path, not collapse to a dot
    assert all(len(s.points) >= 40 for s in strokes)


def test_test4_thick_pens_detected():
    doc = parse_goodnotes(sample("Test4.goodnotes"))
    strokes = {color_to_hex(s.color): s for p in doc.pages for s in p.strokes}
    blue_w = max(w for _x, _y, w in strokes["#007affff"].points)
    red_w = max(w for _x, _y, w in strokes["#d20000ff"].points)
    assert blue_w > 4.0          # thick pen (diameter; ref ~7.25pt on page)
    assert red_w < 3.0           # thin pen  (diameter; ref ~2.5pt on page)
    assert blue_w > red_w * 2    # blue is clearly the thick pen


# --------------------------------------------------------------------------- #
# Integration: writing valid .excalidraw
# --------------------------------------------------------------------------- #

def test_convert_file_produces_valid_excalidraw(tmp_path):
    out = convert_file(sample("Test4.goodnotes"), str(tmp_path / "out.excalidraw"))
    with open(out, encoding="utf-8") as fh:
        scene = json.load(fh)
    assert scene["type"] == "excalidraw"
    assert scene["version"] == 2

    strokes = [e for e in scene["elements"] if e["type"] == "freedraw"]
    frames = [e for e in scene["elements"] if e["type"] == "rectangle"]
    assert len(strokes) == 5
    # Test4 has two pages (one empty) -> one locked frame rectangle per page.
    assert len(frames) == 2 and all(f["locked"] for f in frames)

    expected = {"#d20000", "#007aff", "#f59a23", "#007355", "#ff9797"}
    assert {s["strokeColor"] for s in strokes} == expected
    for s in strokes:
        # one pressure per point, first point at the element origin
        assert len(s["pressures"]) == len(s["points"]) >= 40
        assert all(0.0 < p <= 1.0 for p in s["pressures"])
        assert s["simulatePressure"] is False
        assert min(p[0] for p in s["points"]) == 0.0
        assert min(p[1] for p in s["points"]) == 0.0


def test_excalidraw_pages_stacked_and_dot_rendered(tmp_path):
    # test3 has two dot strokes; also checks single-point strokes get 2 points
    out = convert_file(sample("test3.goodnotes"), str(tmp_path / "out.excalidraw"))
    with open(out, encoding="utf-8") as fh:
        scene = json.load(fh)
    strokes = [e for e in scene["elements"] if e["type"] == "freedraw"]
    assert len(strokes) == 2
    assert all(len(s["points"]) >= 2 for s in strokes)


def test_excalidraw_color_and_format_dispatch():
    assert color_to_rgb_hex((1.0, 0.0, 0.0, 1.0)) == "#ff0000"
    assert color_to_rgb_hex((0.0, 0.4784, 1.0, 0.5)) == "#007aff"
    with pytest.raises(ValueError):
        convert_file(sample("test2.goodnotes"), fmt="svg")


def test_explicit_format_overrides_extension(tmp_path):
    out = convert_file(sample("test2.goodnotes"), str(tmp_path / "out.json"),
                       fmt="excalidraw")
    with open(out, encoding="utf-8") as fh:
        assert json.load(fh)["type"] == "excalidraw"


# --------------------------------------------------------------------------- #
# Integration: writing valid .xopp
# --------------------------------------------------------------------------- #

def test_convert_file_produces_valid_xopp(tmp_path):
    out = convert_file(sample("Test4.goodnotes"), str(tmp_path / "out.xopp"))
    with gzip.open(out, "rb") as fh:
        root = ET.fromstring(fh.read())
    assert root.tag == "xournal"
    strokes = root.findall(".//stroke")
    assert len(strokes) == 5
    for s in strokes:
        assert s.get("color", "").startswith("#")
        # width attr: nominal + one per segment == point count
        n_pts = len(s.text.split()) // 2
        n_widths = len(s.get("width").split())
        assert n_widths == n_pts


# --------------------------------------------------------------------------- #
# Integration: writing valid .pdf
# --------------------------------------------------------------------------- #

def _xref_offsets(data: bytes):
    """Parse the xref table into {objnum: (offset, kind)}.

    Returns (entries, size, startxref_value). Validates that `startxref`
    points at `xref` and reads the single subsection that starts at object 0.
    """
    m = re.search(rb"startxref\s+(\d+)\s+%%EOF", data)
    assert m, "missing startxref/%%EOF trailer"
    startxref = int(m.group(1))
    assert data[startxref:startxref + 4] == b"xref", "startxref does not point at xref"

    lines = data[startxref:].split(b"\n")
    assert lines[0] == b"xref", "xref table malformed"
    first, size = lines[1].split(b" ")
    assert first == b"0", f"first xref subsection must start at 0, got {first!r}"
    size = int(size)

    entries = {}
    for i in range(size):
        parts = lines[2 + i].split(b" ")
        entries[i] = (int(parts[0]), parts[2].decode())
    return entries, size, startxref


def _objects(data: bytes):
    """Map objnum -> raw object bytes ('N 0 obj ... endobj')."""
    objs = {}
    for m in re.finditer(rb"(\d+) 0 obj\n(.*?)\nendobj\n", data, re.S):
        objs[int(m.group(1))] = m.group(2)
    return objs


def _stream(obj_body: bytes) -> bytes:
    """Extract the raw (compressed) stream bytes between ``stream`` and
    ``endstream`` keywords inside a content-stream object body."""
    start = obj_body.index(b"stream\n") + len(b"stream\n")
    end = obj_body.rindex(b"\nendstream")
    return obj_body[start:end]


def test_pdf_synthetic_structure_and_yflip(tmp_path):
    doc = GoodNotesDocument(pages=[
        Page(width=100.0, height=50.0, strokes=[
            Stroke(points=[(10.0, 20.0, 1.0), (20.0, 30.0, 2.0)],
                   color=(1.0, 0.0, 0.0, 1.0)),
        ]),
        Page(width=100.0, height=50.0, strokes=[
            Stroke(points=[(5.0, 5.0, 1.0), (6.0, 6.0, 1.0), (7.0, 7.0, 1.0)],
                   color=(0.0, 1.0, 0.0, 0.5)),
        ]),
    ])
    data = build_pdf(doc, width_scale=1.0)
    out = tmp_path / "out.pdf"
    out.write_bytes(data)

    # --- trailer / xref self-consistency -----------------------------------
    assert data.startswith(b"%PDF-1.4"), "missing PDF header"
    assert data.rstrip().endswith(b"%%EOF"), "missing %%EOF"
    entries, size, _ = _xref_offsets(data)
    objs = _objects(data)
    assert size == 1 + len(objs), "xref size != object count"
    for num in range(1, size):
        off, kind = entries[num]
        assert kind == "n", f"obj {num} not an in-use entry"
        assert data[off:].startswith(f"{num} 0 obj".encode()), \
            f"xref offset for obj {num} is wrong"
        assert num in objs, f"obj {num} present in xref but not in file"

    # --- page count / kids --------------------------------------------------
    pages = _objects(data)[2]
    assert b"/Count 2" in pages and b"/Kids [" in pages

    # --- per-page MediaBox --------------------------------------------------
    p1 = _objects(data)[3]
    assert b"/MediaBox [0 0 100 50]" in p1, "page 1 MediaBox wrong"

    # --- content streams: Y-flip + colour ----------------------------------
    c1 = zlib.decompress(_stream(_objects(data)[4]))
    # red stroke, opaque -> no ExtGState
    assert b"1 0 0 RG" in c1
    # first point (10,20) on a 50pt-tall page -> y flipped to 30
    assert b"10 30 m" in c1
    # second point (20,30) -> y flipped to 20
    assert b"20 20 l" in c1
    # segment width = mean(1,2) = 1.5
    assert b"1.5 w" in c1
    # round caps/joins set
    assert b"J 1" in c1 and b"j 1" in c1

    # --- ExtGState for the semi-transparent green stroke -------------------
    # page 2 content is obj 6; its resources must reference GS0
    p2 = _objects(data)[5]
    assert b"/ExtGState" in p2
    c2 = zlib.decompress(_stream(_objects(data)[6]))
    assert b"0 1 0 RG" in c2
    assert b"/GS0 gs" in c2
    # the ExtGState object (obj 7) sets ca/CA to 0.5
    assert b"/ca 0.5 /CA 0.5" in _objects(data)[7]


def test_pdf_real_sample(tmp_path):
    out = convert_file(sample("Test4.goodnotes"), str(tmp_path / "out.pdf"))
    data = open(out, "rb").read()
    assert data.startswith(b"%PDF-1.4")
    assert data.rstrip().endswith(b"%%EOF")

    # xref offsets are self-consistent
    entries, size, _ = _xref_offsets(data)
    objs = _objects(data)
    assert size == 1 + len(objs)
    for num in range(1, size):
        off, kind = entries[num]
        assert kind == "n"
        assert data[off:].startswith(f"{num} 0 obj".encode())

    # Test4 has two pages (one empty, one with the five strokes), matching the
    # reference Test4.pdf; each is 455.04 x 588.45 pt (from its background PDF).
    pages = _objects(data)[2]
    assert b"/Count 2" in pages
    assert b"/MediaBox [0 0 455.04 588.45]" in _objects(data)[3]

    # all five stroke colours present in the (decompressed) content stream of
    # the second page (obj 6: catalog, pages, then [page,content] per page)
    c = zlib.decompress(_stream(_objects(data)[6])).decode()
    expected = {  # #d20000 #007aff #f59a23 #007355 #ff9797 -> RGB floats
        "0.8235 0 0 RG",   # d2
        "0 0.4784 1 RG",   # 007aff
        "0.9608 0.6039 0.1373 RG",  # f59a23
        "0 0.451 0.3333 RG",        # 007355
        "1 0.5922 0.5922 RG",       # ff9797
    }
    for frag in expected:
        assert frag in c, f"missing colour {frag!r} in PDF content"


# --------------------------------------------------------------------------- #
# Sample 5: full-fidelity features (3 pages, backgrounds, image, text)
# --------------------------------------------------------------------------- #

def test_test5_three_pages_in_order():
    # The reference has three pages: a background-only page, a doodle page, and
    # a photo/text page.  All three must appear, in that order.
    doc = parse_goodnotes(sample("Test5.goodnotes"))
    assert len(doc.pages) == 3
    p0, p1, p2 = doc.pages
    # first page is empty (background only) but is kept
    assert not p0.strokes and not p0.images and not p0.texts
    assert p0.background
    # doodle page: strokes, no image
    assert p1.strokes and not p1.images
    # photo/text page
    assert p2.images and p2.texts
    # page size comes from the embedded paper PDF (not A4)
    assert p0.width == pytest.approx(455.04, abs=0.1)
    assert p0.height == pytest.approx(588.45, abs=0.1)


def test_test5_backgrounds_map_to_pages():
    # Page 1 uses the cyan paper; the doodle + photo pages use the graph paper.
    # We identify a paper by rendering-free proxy: the two background attachments
    # are distinct, and page 1 differs from the others.
    doc = parse_goodnotes(sample("Test5.goodnotes"))
    p0, p1, p2 = doc.pages
    assert p0.background and p1.background and p2.background
    assert p0.background != p1.background      # cyan != graph
    assert p1.background == p2.background       # graph == graph


def test_test5_text_parsed_black_and_plain():
    doc = parse_goodnotes(sample("Test5.goodnotes"))
    texts = [t for p in doc.pages for t in p.texts]
    assert len(texts) == 2
    plains = sorted(t.plain for t in texts)
    assert "Hallo" in plains
    assert any("Test" in p and "123" in p for p in plains)
    # text colour is black (the box's 15-byte fields are geometry, not colour)
    assert all(t.color == (0.0, 0.0, 0.0, 1.0) for t in texts)


def test_test5_xopp_has_image_and_text(tmp_path):
    out = convert_file(sample("Test5.goodnotes"), str(tmp_path / "out.xopp"))
    with gzip.open(out, "rb") as fh:
        root = ET.fromstring(fh.read())
    pages = root.findall("page")
    assert len(pages) == 3
    rich = [p for p in pages if p.findall(".//image")]
    assert len(rich) == 1
    img = rich[0].find(".//image")
    # base64 body is the re-encoded raster (large), with a naturalSize hint
    assert len(img.text or "") > 1000
    assert img.get("naturalSize")
    # the photo lands inside the page box
    w, h = float(pages[0].get("width")), float(pages[0].get("height"))
    assert 0 < float(img.get("left")) < w
    assert 0 < float(img.get("top")) < h
    # two text boxes on the same page
    assert len(rich[0].findall(".//text")) == 2


def test_test5_pdf_full_fidelity(tmp_path):
    out = convert_file(sample("Test5.goodnotes"), str(tmp_path / "out.pdf"))
    data = open(out, "rb").read()
    assert data.startswith(b"%PDF-1.4")
    assert data.rstrip().endswith(b"%%EOF")

    entries, size, _ = _xref_offsets(data)
    objs = _objects(data)
    assert size == 1 + len(objs)
    for num in range(1, size):
        off, kind = entries[num]
        assert kind == "n"
        assert data[off:].startswith(f"{num} 0 obj".encode())

    pages = _objects(data)[2]
    assert b"/Count 3" in pages

    # the empty first page still carries its background as an image XObject
    page0 = _objects(data)[3]
    assert b"/ImBg" in page0

    # the photo page carries the train photo + a font + text
    page2 = _objects(data)[7]
    assert b"/Im" in page2          # embedded photo XObject
    assert b"/F1" in page2          # Helvetica text font
    assert b"/Helvetica" in data


def test_pdf_stroke_only_still_valid_without_raster(tmp_path, monkeypatch):
    # Even if the optional raster deps are unavailable, a valid stroke-only PDF
    # is produced (no backgrounds / photos / text), and the xref stays sound.
    import goodparse.pdf as pdf_mod
    monkeypatch.setattr(pdf_mod, "_pdfium", None)
    monkeypatch.setattr(pdf_mod, "_PILImage", None)
    out = convert_file(sample("Test5.goodnotes"), str(tmp_path / "out.pdf"))
    data = open(out, "rb").read()
    assert data.startswith(b"%PDF-1.4")
    assert data.rstrip().endswith(b"%%EOF")
    entries, size, _ = _xref_offsets(data)
    assert size == 1 + len(_objects(data))
    # no image XObjects / backgrounds without the raster deps (text is stdlib
    # and still appears)
    assert b"/DCTDecode" not in data
    assert b"/ImBg" not in data
    assert b"/F1" in data              # text font still present


# --------------------------------------------------------------------------- #
# Optional embedded fonts (Futura / Helvetica Neue -> installed clone)
# --------------------------------------------------------------------------- #

def _f21_families():
    """The font family names captured for Test9's sticker text, or []."""
    doc = parse_goodnotes(sample("Test9.goodnotes"))
    fams = set()
    for p in doc.pages:
        for t in p.texts:
            for ln in (t.lines or []):
                for r in ln.runs:
                    if getattr(r, "font_family", None):
                        fams.add(r.font_family)
    return fams


def test_field21_families_captured():
    # Test9's sticker text names Futura (FRIENDS/SUCH/GOOD) and Helvetica
    # Neue (Hallo); the parser must capture the family on each run.
    fams = _f21_families()
    assert "Futura" in fams
    assert "Helvetica Neue" in fams


def test_embedded_font_present_when_clone_installed(tmp_path):
    import goodparse.font_embed as fe
    if not fe.fonttools_available():
        pytest.skip("fontTools not installed")
    if fe.find_font_file("Futura", True, False) is None:
        pytest.skip("no Futura clone (URW Gothic) installed")
    out = convert_file(sample("Test9.goodnotes"), str(tmp_path / "out.pdf"))
    data = open(out, "rb").read()
    # embedded CFF fonts use /FontFile3 + a CIDFontType0C descendant and a
    # 2-byte hex string in the content stream; the base-14 fallback stays too
    assert data.count(b"/FontFile3") >= 1
    assert b"/CIDFontType0C" in data
    assert b"/Identity-H" in data
    # every embedded font must have a ToUnicode map (searchable/selectable)
    assert data.count(b"/ToUnicode") == data.count(b"/FontFile3")
    # the xref still stays self-consistent with the extra objects
    entries, size, _ = _xref_offsets(data)
    assert size == 1 + len(_objects(data))


def test_embedded_font_falls_back_to_base14_without_fonts(tmp_path, monkeypatch):
    import goodparse.font_embed as fe
    import goodparse.pdf as pdf_mod
    if not fe.fonttools_available():
        pytest.skip("fontTools not installed")
    # Force "no clone found" so the base-14 Helvetica path must be used.
    monkeypatch.setattr(fe, "find_font_file", lambda *a, **k: None)
    out = convert_file(sample("Test9.goodnotes"), str(tmp_path / "out.pdf"))
    data = open(out, "rb").read()
    # no embedded font objects, but the base-14 bold is still there for text
    assert b"/FontFile3" not in data
    assert b"/CIDFontType0C" not in data
    assert b"/Helvetica-Bold" in data
    entries, size, _ = _xref_offsets(data)
    assert size == 1 + len(_objects(data))
