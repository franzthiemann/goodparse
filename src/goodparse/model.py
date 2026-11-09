"""The drawing model every writer consumes.

Coordinates are GoodNotes *canvas* units with a top-left origin; each
:class:`Page` carries the uniform ``scale`` that maps canvas units to PDF
points, and its own size in points.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .text_rtf import TextLine

# A4 in PDF points (72 dpi) — fallback for documents without an explicit page
# size (samples 1-4 are A4).
A4_WIDTH_PT = 595.28
A4_HEIGHT_PT = 841.89


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
    # Pre-laid-out text (document-flow paragraphs): ``lines`` are final visual
    # lines, spaced exactly ``line_height`` apart, the first baseline
    # ``baseline_offset`` below ``position``'s y (canvas units).  0 = the
    # writer lays the text out itself.
    line_height: float = 0.0
    baseline_offset: float = 0.0
    # Box background fill (RGBA) when the box has one, else None.
    background: Optional[Tuple[float, float, float, float]] = None


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
    background_page: int = 0  # 0-based page of ``background`` to show


@dataclass
class GoodNotesDocument:
    pages: List[Page] = field(default_factory=list)
    title: str = "GoodNotes"


def _bbox_diagonal(points) -> float:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return ((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2) ** 0.5
