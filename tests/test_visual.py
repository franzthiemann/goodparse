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

TOLERANCE = 0.05

CEILINGS = {
    "Test4": [0.10, 3.55],
    "Test5": [0.10, 4.95, 8.00],
    "Test6": [9.25, 3.35, 8.50, 4.40, 1.15],
    "Test7": [7.05, 12.75, 1.55, 10.20],
    "Test8": [5.50, 5.65, 0.45, 1.15],
    "Test9": [0.15, 5.95, 4.45, 0.65, 0.55, 255.0, 255.0],
    "Test_10": [0.10, 0.25, 0.30, 0.20, 0.25, 0.20, 0.45],
    "Test_11": [0.10, 5.40, 0.80, 4.95, 5.35, 1.85, 1.25],
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
