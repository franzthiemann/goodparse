"""Regression ratchet: rendered output vs. the GoodNotes reference export.

``CEILINGS`` holds, per sample, the worst acceptable per-page score from
``tests/visual.py`` (mean abs pixel difference, 0..255; 255 = page missing or
wrong size).  The values are the measured scores rounded up, so any rendering
regression fails the test.  When a fix lowers a score, lower its ceiling too.
"""

import pytest

pytest.importorskip("pypdfium2")
pytest.importorskip("PIL")

from visual import compare, reference_samples  # noqa: E402  (tests/ is on sys.path)

# Headroom for differences between machines, not between code versions: a
# system without the URW font clones falls back to base-14 text (+0.06 on
# Test9 p3 in a python:3.12-slim container), and rasteriser versions differ
# slightly (+0.05).  The smallest real regression seen so far was +0.15.
TOLERANCE = 0.10

CEILINGS = {
    "Test4": [0.10, 3.55],
    "Test5": [0.10, 3.45, 5.45],
    "Test6": [3.55, 1.30, 1.30, 4.30, 1.10],
    "Test7": [0.90, 3.60, 1.00, 1.60],
    "Test8": [0.35, 0.45, 0.15, 0.05],
    "Test9": [0.15, 4.70, 4.25, 0.15, 0.55, 4.60, 0.60],
    "Test_10": [0.10, 0.15, 0.20, 0.10, 0.15, 0.10, 0.20],
    "Test_11": [0.10, 0.25, 0.20, 0.25, 0.70, 0.30, 0.70],
    "Test_12": [0.10, 2.35, 0.45, 1.75],
}


def test_every_reference_sample_has_a_ceiling():
    assert sorted(reference_samples()) == sorted(CEILINGS)


@pytest.mark.parametrize("name", sorted(CEILINGS))
def test_render_matches_reference(name):
    scores = compare(name)
    ceilings = CEILINGS[name]
    assert len(scores) == len(ceilings), f"page count changed: {scores}"
    worse = [(i + 1, round(s, 2), c) for i, (s, c) in enumerate(zip(scores, ceilings))
             if s > c + TOLERANCE]
    assert not worse, f"pages rendered worse than ceiling (page, score, ceiling): {worse}"
