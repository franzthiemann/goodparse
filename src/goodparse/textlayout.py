"""Place a text box's runs on the page: wrapping, line heights, alignment.

Shared by the writers so text sits in the same place in every output format.
Positions are in **page points with a top-left origin** (canvas units times
the page scale); each writer only maps them to its own coordinates and fonts.

* Each :class:`TextLine` starts a new line; within it, runs keep their exact
  text (spaces included), so adjacent styled runs join exactly as in the
  original.
* Lines wrap at the box width.  For text objects (explicit ``font_size``)
  that width is GoodNotes' own; since our fonts are close clones, not the
  originals, a line only wraps once it exceeds it by more than
  ``TEXT_WRAP_SLACK`` (single-line labels measure 0.94-1.06x their stored
  width, the lines GoodNotes really wrapped 1.34x and 1.66x).  Pre-laid-out
  text (``line_height`` set, e.g. "start typing" paragraphs) keeps its lines.
* Text objects store their laid-out content height, i.e. the real font's line
  height summed over the lines (29 for one 24 pt Helvetica Neue line, 74 for
  55 pt Futura); it is spread in proportion to each line's size instead of
  guessing font metrics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .base14_widths import base14_key, run_width
from .model import TextBox
from .text_rtf import TextLine, TextRun

TEXT_WRAP_SLACK = 1.07

# (segment text, run, font size in points) -> advance width in points
WidthFn = Callable[[str, TextRun, float], float]


def base14_width(seg: str, run: TextRun, size: float) -> float:
    return run_width(seg, size, base14_key(run))


@dataclass
class Segment:
    text: str
    run: TextRun
    x: float          # left edge (points)
    width: float      # advance width (points)
    size: float       # font size (points)


@dataclass
class PlacedLine:
    baseline: float   # points from the page top
    size: float       # largest font size on the line
    pitch: float      # distance to the next line's baseline
    align: str
    segments: List[Segment]


def default_font_size(t: TextBox) -> float:
    """Canvas-unit size for runs without their own: explicit for text
    objects, else the RTF's ``\\fs`` (half-points), else from the box height
    (1.2 x the size per line)."""
    if t.font_size > 0:
        return t.font_size
    m = re.search(r"\\fs(\d+)", t.rtf)
    if m:
        return int(m.group(1)) / 2.0
    nlines = max(1, len(t.plain.split("\n")))
    return max(8.0, t.size[1] / (1.2 * nlines))


def layout_text(t: TextBox, scale: float, width: Optional[WidthFn] = None
                ) -> List[PlacedLine]:
    """Lay out text box ``t`` on a page with canvas->page ``scale``."""
    width = width or base14_width
    box_fs = default_font_size(t) * scale
    x0 = t.position[0] * scale
    box_w = t.size[0] * scale
    if t.font_size > 0:
        wrap_w = box_w * TEXT_WRAP_SLACK if box_w > 0 else float("inf")
    else:
        wrap_w = max(box_fs * 0.5, box_w)
    lines = t.lines or [TextLine(runs=[TextRun(text=t.plain, color=t.color)])]

    def size_of(run: TextRun) -> float:
        return run.size * scale if run.size else box_fs

    def w(seg: str, run: TextRun) -> float:
        return width(seg, run, size_of(run))

    # 1) Greedy wrap on word pieces (each keeps its surrounding whitespace).
    rows: List[Tuple[str, List[Tuple[str, TextRun]]]] = []
    if t.line_height > 0:
        rows = [(ln.align, [(r.text, r) for r in ln.runs]) for ln in lines]
    for line in (lines if t.line_height <= 0 else []):
        vlines: List[List[Tuple[str, TextRun]]] = [[]]
        used = 0.0
        for run in line.runs:
            for seg in re.findall(r"\s*\S+\s*|\s+", run.text):
                if vlines[-1] and used + w(seg.rstrip(), run) > wrap_w:
                    vlines.append([])
                    used = 0.0
                    seg = seg.lstrip()
                vlines[-1].append((seg, run))
                used += w(seg, run)
        rows.extend((line.align, vl) for vl in vlines)

    # 2) Line heights.
    sizes = [max((size_of(run) for _seg, run in vl), default=box_fs) for _a, vl in rows]
    ratio = 0.0
    if t.font_size > 0 and t.size[1] > 0 and sum(sizes) > 0 and t.line_height <= 0:
        ratio = t.size[1] * scale / sum(sizes)

    # 3) Positions.
    out: List[PlacedLine] = []
    y = t.position[1] * scale
    for j, ((align, vl), fs_line) in enumerate(zip(rows, sizes)):
        if t.line_height > 0:
            pitch = t.line_height * scale
            baseline = y + t.baseline_offset * scale + j * pitch
        else:
            pitch = fs_line * (ratio or 1.2)
            baseline = y + pitch * (0.8 if ratio else 2 / 3)
            y += pitch
        segs: List[Segment] = []
        if vl:
            widths = [w(seg, run) for seg, run in vl]
            line_w = sum(widths[:-1]) + w(vl[-1][0].rstrip(), vl[-1][1])
            cursor = x0
            if box_w > 0 and align == "center":
                cursor = x0 + (box_w - line_w) / 2.0
            elif box_w > 0 and align == "right":
                cursor = x0 + box_w - line_w
            for (seg, run), sw in zip(vl, widths):
                segs.append(Segment(seg, run, cursor, sw, size_of(run)))
                cursor += sw
        out.append(PlacedLine(baseline, fs_line, pitch, align, segs))
    return out
