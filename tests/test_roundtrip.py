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
    assert blue_w > 2.0          # thick pen
    assert red_w < 1.5           # thin pen
    assert blue_w > red_w * 2


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
    assert len(frames) == 1 and frames[0]["locked"]

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

    # single A4 page
    pages = _objects(data)[2]
    assert b"/Count 1" in pages
    assert b"/MediaBox [0 0 595.28 841.89]" in _objects(data)[3]

    # all five stroke colours present in the (decompressed) content stream
    c = zlib.decompress(_stream(_objects(data)[4])).decode()
    expected = {  # #d20000 #007aff #f59a23 #007355 #ff9797 -> RGB floats
        "0.8235 0 0 RG",   # d2
        "0 0.4784 1 RG",   # 007aff
        "0.9608 0.6039 0.1373 RG",  # f59a23
        "0 0.451 0.3333 RG",        # 007355
        "1 0.5922 0.5922 RG",       # ff9797
    }
    for frag in expected:
        assert frag in c, f"missing colour {frag!r} in PDF content"
