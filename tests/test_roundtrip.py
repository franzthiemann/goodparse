"""End-to-end tests against the reverse-engineered sample files."""

import gzip
import json
import os
import xml.etree.ElementTree as ET

import pytest

from goodnotes2xournal import convert_file, parse_goodnotes
from goodnotes2xournal.applelz4 import apple_decompress, lz4_block_decompress
from goodnotes2xournal.excalidraw import color_to_rgb_hex
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
