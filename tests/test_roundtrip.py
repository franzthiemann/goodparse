"""End-to-end tests against the reverse-engineered sample files."""

import gzip
import os
import xml.etree.ElementTree as ET

import pytest

from goodnotes2xournal import convert_file, parse_goodnotes
from goodnotes2xournal.applelz4 import apple_decompress, lz4_block_decompress
from goodnotes2xournal.goodnotes import extract_points
from goodnotes2xournal.xournal import color_to_hex

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
