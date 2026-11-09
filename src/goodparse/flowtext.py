"""Lay out GoodNotes "start typing" text (document-flow paragraphs).

The Text tool's *start typing* mode fills the page with flowing, markdown-like
text.  Each paragraph is one page object of kind 12: Cocoa RTF in ``f3`` and
**no geometry** — GoodNotes lays the paragraphs out itself:

* order: ``f13.f1.f1`` is the paragraph index (absent = 0);
* indent level: ``f27.f1`` (absent = 0);
* list items keep a literal marker (``"•\\t"`` / ``"1.\\t"``) at the start of
  their text, but GoodNotes draws its own marker per level and numbers each
  level itself, so the stored marker only tells bullet from numbered.

Layout, measured on the flattened export of Test_13 (the regular export drops
the indentation — a GoodNotes bug) and on a landscape real-world page, in
canvas units: left and right margin 44; 31 per indent level (``\\li620`` /
20); a list marker sits at the level's indent and its text 31 further; every
line — whatever its font size — is exactly ``|\\slN| / 20`` below the previous
one (29.3 for ``\\sl-586``); the first baseline is at 91.63.  Markers by
level: ● ○ ◆ ◇ ■ □ ▲ △ (filled/outlined, cycling), drawn as vector shapes
because the base-14 fonts have no such glyphs.
"""

from __future__ import annotations

import math
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .base14_widths import base14_key, run_width
from .model import Stroke, TextBox
from .protobuf import parse_message_tolerant
from .records import PageObject
from .text_rtf import TextLine, TextRun, parse_rtf_runs

MARGIN = 44.0
INDENT = 31.0
FIRST_BASELINE = 91.63
_DEFAULT_SIZE = 18.0       # \fs36, the body size of every sample
_MARKER_SIZE = 18.0        # size the marker geometry below was measured at
_OUTLINE = 0.7             # outline thickness of the hollow markers

_LIST_MARKER = re.compile(r"^(?:\u2022|\d+\.)\t")
_SL = re.compile(r"\\sl(-?\d+)")

Point = Tuple[float, float]


def _first(f: Dict[int, List[object]], k: int):
    return f.get(k, [None])[0]


def _int_at(msg, *path: int) -> int:
    """Integer at a nested field path (absent -> 0)."""
    v = msg
    for k in path:
        if not isinstance(v, (bytes, bytearray)):
            return 0
        v = _first(parse_message_tolerant(bytes(v)), k)
    return v if isinstance(v, int) else 0


class _Paragraph:
    def __init__(self, obj: PageObject):
        self.index = _int_at(_first(obj.fields, 13), 1, 1)
        self.level = _int_at(_first(obj.fields, 27), 1)
        rtf = _first(obj.fields, 3)
        rtf = bytes(rtf) if isinstance(rtf, (bytes, bytearray)) else b""
        m = _SL.search(rtf.decode("cp1252", "replace"))
        self.pitch = abs(int(m.group(1))) / 20.0 if m else 0.0
        self.lines: List[TextLine] = [ln for ln in parse_rtf_runs(rtf)]
        self.kind: Optional[str] = None
        first = next((r for ln in self.lines for r in ln.runs), None)
        if first is not None:
            mm = _LIST_MARKER.match(first.text)
            if mm:
                self.kind = "bullet" if mm.group(0).startswith("\u2022") else "numbered"
                first.text = first.text[mm.end():]
        self.first_run = first

    def size(self) -> float:
        sizes = [r.size for ln in self.lines for r in ln.runs if r.size]
        return max(sizes) if sizes else _DEFAULT_SIZE


def _wrap(lines: Sequence[TextLine], width: float) -> List[TextLine]:
    """Greedy word wrap of styled lines at ``width`` (canvas units)."""
    out: List[TextLine] = []
    for line in lines:
        cur = TextLine(align=line.align)
        used = 0.0
        for run in line.runs:
            size = run.size or _DEFAULT_SIZE
            for seg in re.findall(r"\s*\S+\s*|\s+", run.text):
                w = run_width(seg, size, base14_key(run))
                if cur.runs and used + run_width(seg.rstrip(), size, base14_key(run)) > width:
                    out.append(cur)
                    cur = TextLine(align=line.align)
                    used = 0.0
                    seg = seg.lstrip()
                    w = run_width(seg, size, base14_key(run))
                if cur.runs and _same_style(cur.runs[-1], run):
                    cur.runs[-1].text += seg
                else:
                    cur.runs.append(TextRun(seg, run.bold, run.italic, run.underline,
                                            run.strike, run.color, run.font_family, run.size))
                used += w
        out.append(cur)
    return out or [TextLine()]


def _same_style(a: TextRun, b: TextRun) -> bool:
    return (a.bold, a.italic, a.underline, a.strike, a.color, a.font_family, a.size) == \
           (b.bold, b.italic, b.underline, b.strike, b.color, b.font_family, b.size)


# --------------------------------------------------------------------------- #
# Markers (measured ink, relative to the marker origin and the baseline; y
# negative = above the baseline; at text size 18)
# --------------------------------------------------------------------------- #

def _circle(cx: float, cy: float, r: float, n: int = 36) -> List[Point]:
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
            for i in range(n + 1)]


def _marker_shape(level: int) -> Tuple[List[Point], bool]:
    """``(outline points, filled)`` of the bullet for ``level``."""
    kind = level % 8
    filled = kind % 2 == 0
    h = 0.0 if filled else _OUTLINE / 2  # outlines are stroked on the ink edge
    if kind == 0:
        return _circle(5.35, -5.13, 3.82), True
    if kind == 1:
        return _circle(8.875, -6.90, 7.79 - h), False
    if kind in (2, 3):
        cx, cy, rx, ry = 8.965, -6.865, 8.635 - h, 8.555 - h
        return [(cx - rx, cy), (cx, cy - ry), (cx + rx, cy), (cx, cy + ry), (cx - rx, cy)], filled
    if kind in (4, 5):
        x0, x1, y0, y1 = 2.19 + h, 15.64 - h, -13.68 + h, -0.08 - h
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)], filled
    x0, x1, top, base = 1.00 + h, 16.89 - h, -14.37 + h, -0.47 - h
    return [(x0, base), ((x0 + x1) / 2, top), (x1, base), (x0, base)], filled


def _bullet(level: int, x: float, baseline: float, size: float,
            color) -> Stroke:
    pts, filled = _marker_shape(level)
    k = size / _MARKER_SIZE
    width = 0.1 if filled else _OUTLINE * k
    return Stroke(points=[(x + px * k, baseline + py * k, width) for px, py in pts],
                  color=color, kind="pen", fill=color if filled else None)


# --------------------------------------------------------------------------- #

def layout_flow(objects: Sequence[PageObject], canvas_width: float
                ) -> Tuple[List[TextBox], List[Stroke]]:
    """Lay out a page's flow paragraphs into text boxes and marker shapes."""
    paragraphs = sorted((_Paragraph(o) for o in objects), key=lambda p: p.index)
    texts: List[TextBox] = []
    marks: List[Stroke] = []
    counters: Dict[int, int] = {}
    baseline = FIRST_BASELINE
    for p in paragraphs:
        size = p.size()
        pitch = p.pitch or size * 1.2
        marker_x = MARGIN + INDENT * p.level
        text_x = marker_x + (INDENT if p.kind else 0.0)
        lines = _wrap(p.lines, max(INDENT, canvas_width - MARGIN - text_x))
        color = p.first_run.color if p.first_run else (0.0, 0.0, 0.0, 1.0)

        if p.kind == "numbered":
            counters[p.level] = counters.get(p.level, 0) + 1
        for lvl in [k for k in counters if k > p.level or (k == p.level and p.kind != "numbered")]:
            del counters[lvl]
        if p.kind == "bullet":
            marks.append(_bullet(p.level, marker_x, baseline, size, color))
        elif p.kind == "numbered":
            label = f"{counters[p.level]}."
            texts.append(_box(marker_x, baseline, [TextLine(runs=[TextRun(label, color=color, size=size)])],
                              size, pitch, INDENT))

        if any(ln.runs for ln in lines):
            texts.append(_box(text_x, baseline, lines, size, pitch, canvas_width - MARGIN - text_x))
        baseline += pitch * len(lines)
    return texts, marks


def _box(x: float, baseline: float, lines: List[TextLine], size: float,
         pitch: float, width: float) -> TextBox:
    plain = "\n".join(ln.text() for ln in lines)
    color = next((r.color for ln in lines for r in ln.runs), (0.0, 0.0, 0.0, 1.0))
    return TextBox(position=(x, baseline - size), size=(width, pitch * len(lines)),
                   rtf="", plain=plain, color=color, lines=lines, font_size=size,
                   line_height=pitch, baseline_offset=size)
