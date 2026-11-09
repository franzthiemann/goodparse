"""Decode GoodNotes *text objects* (page-object kind 21) into styled lines.

A text object's body holds the placement in ``f20`` (``f1`` = top-left point,
canvas units) and the content in ``f32``:

* ``f32.f1.f2`` — Apple-LZ4 blob; decompressed it is a protobuf whose repeated
  ``f1`` messages are the **runs**: ``{1: text, 2: character attributes,
  3: paragraph attributes}``;
* ``f32.f2`` — content size ``{1: w, 2: h}`` (canvas units);
* ``f32.f5.f1`` — the **default** character attributes, which a run attribute
  set to the sentinel ``-404`` (or omits) inherits;
* ``f32.f10`` — text inset ``{1: top, 2: left, 3: bottom, 4: right}``.

Character attributes (``run.f2``), as observed in Test9 / Test_11 / Test_12:

======  ====================================================================
``f1``  strikethrough (1 = single)
``f2``  underline (1 = single)
``f3``  colour RGBA (float32 sub-fields 1..4)
``f30`` font family name (e.g. "Helvetica Neue", "Georgia", "Courier New")
``f40`` size (float32, canvas units)
``f60`` weight (signed varint): default -60, "Bold" -30
``f50`` italic (1)
``f60`` weight (signed varint): default -60, "Bold" -30
``f70`` unknown (default -20); ignored
======  ====================================================================

Paragraph attributes (``run.f3``): ``f4`` alignment — absent = left, 2 =
centre, 3 = right (Test_11, Test_12) — and ``f3`` list style: ``f3.f1`` = 1
numbered list, ``f3.f1`` absent (``f3`` present but empty) = bullet list,
``-1`` = no list.  A run's paragraph attributes belong to the paragraph its
text is in: a run that is just the ``\n`` ending a paragraph carries that
paragraph's style, not the next one's.

Assumptions (flagged; only these values have been observed): weight > -60
renders bold; alignment 1 is taken as left and 4 (if it occurs) as justify.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .applelz4 import apple_decompress, is_apple_lz4
from .protobuf import get_bytes, parse_message_tolerant
from .text_rtf import TextLine, TextRun

RGBA = Tuple[float, float, float, float]

_INHERIT = -404
_DEFAULT_WEIGHT = -60
_ALIGN = {0: "left", 1: "left", 2: "center", 3: "right", 4: "justify"}


@dataclass
class TextObject:
    position: Tuple[float, float]   # top-left of the text area, canvas units
    size: Tuple[float, float]       # content (w, h), canvas units
    lines: List[TextLine]
    font_size: float                # default size, canvas units


def _signed(v: int) -> int:
    return v - (1 << 64) if v >= 1 << 63 else v


def _first(f: Dict[int, List[object]], k: int):
    return f.get(k, [None])[0]


def _point(msg) -> Optional[Tuple[float, float]]:
    if not isinstance(msg, (bytes, bytearray)):
        return None
    f = parse_message_tolerant(bytes(msg))
    x, y = _first(f, 1), _first(f, 2)
    if isinstance(x, float) and isinstance(y, float):
        return x, y
    return None


def _rgba(msg) -> Optional[RGBA]:
    if not isinstance(msg, (bytes, bytearray)):
        return None
    c = [0.0, 0.0, 0.0, 1.0]
    for k, v in parse_message_tolerant(bytes(msg)).items():
        if 1 <= k <= 4 and isinstance(v[0], float):
            c[k - 1] = v[0]
    return (c[0], c[1], c[2], c[3])


@dataclass
class _Attrs:
    color: RGBA = (0.0, 0.0, 0.0, 1.0)
    family: Optional[str] = None
    size: float = 24.0
    weight: int = _DEFAULT_WEIGHT
    underline: bool = False
    italic: bool = False
    strike: bool = False

    def apply(self, msg) -> "_Attrs":
        """A copy of these attributes overridden by a character-attribute msg."""
        out = _Attrs(self.color, self.family, self.size, self.weight,
                     self.underline, self.italic, self.strike)
        if not isinstance(msg, (bytes, bytearray)):
            return out
        f = parse_message_tolerant(bytes(msg))
        out.color = _rgba(_first(f, 3)) or out.color
        fam = _first(f, 30)
        if isinstance(fam, (bytes, bytearray)) and fam:
            out.family = bytes(fam).decode("utf-8", "replace")
        size = _first(f, 40)
        if isinstance(size, float) and size > 0:
            out.size = size
        weight = _first(f, 60)
        if isinstance(weight, int) and _signed(weight) != _INHERIT:
            out.weight = _signed(weight)
        for field_no, name in ((2, "underline"), (1, "strike"), (50, "italic")):
            v = _first(f, field_no)
            if isinstance(v, int):
                setattr(out, name, v != 0)
        return out


def _paragraph(msg) -> Tuple[str, Optional[str]]:
    """``(alignment, list kind)`` of a paragraph-attribute message."""
    if not isinstance(msg, (bytes, bytearray)):
        return "left", None
    f = parse_message_tolerant(bytes(msg))
    align = _first(f, 4)
    alignment = _ALIGN.get(align, "left") if isinstance(align, int) else "left"
    lst = _first(f, 3)
    kind = None
    if isinstance(lst, (bytes, bytearray)):
        style = _first(parse_message_tolerant(bytes(lst)), 1)
        if style is None:
            kind = "bullet"
        elif isinstance(style, int) and _signed(style) == 1:
            kind = "numbered"
    return alignment, kind


def _runs(content: bytes) -> List[Tuple[str, object, object]]:
    raw = content
    if is_apple_lz4(content):
        raw = apple_decompress(content)
    out = []
    for run in parse_message_tolerant(raw).get(1, []):
        if not isinstance(run, (bytes, bytearray)):
            continue
        f = parse_message_tolerant(bytes(run))
        text = _first(f, 1)
        if isinstance(text, (bytes, bytearray)):
            out.append((bytes(text).decode("utf-8", "replace"), _first(f, 2), _first(f, 3)))
    return out


def decode_text_object(body: bytes) -> Optional[TextObject]:
    """Decode a kind-21 body; ``None`` when it carries no readable text."""
    f20 = get_bytes(body, 20)
    origin = _point(get_bytes(f20, 1)) if f20 else None
    f32 = get_bytes(body, 32)
    if not origin or not f32:
        return None
    f = parse_message_tolerant(f32)
    container = _first(f, 1)
    blob = get_bytes(bytes(container), 2) if isinstance(container, (bytes, bytearray)) else None
    if not blob:
        return None
    defaults = _Attrs()
    dmsg = _first(f, 5)
    if isinstance(dmsg, (bytes, bytearray)):
        defaults = defaults.apply(_first(parse_message_tolerant(bytes(dmsg)), 1))
    inset = parse_message_tolerant(get_bytes(f32, 10) or b"")
    top = _first(inset, 1) if isinstance(_first(inset, 1), float) else 0.0
    left = _first(inset, 2) if isinstance(_first(inset, 2), float) else 0.0
    size = _point(_first(f, 2)) or (0.0, 0.0)

    lines: List[TextLine] = [TextLine()]
    numbers = 0      # running number of a numbered list
    started = False  # the current line has received its paragraph style
    for text, cattr, pattr in _runs(blob):
        attrs = defaults.apply(cattr)
        alignment, kind = _paragraph(pattr)
        for i, piece in enumerate(text.split("\n")):
            if i:
                lines.append(TextLine())
                started = False
            if not piece:
                continue
            line = lines[-1]
            if not started:
                # The first text of a paragraph carries its style.
                started = True
                line.align = alignment
                numbers = numbers + 1 if kind == "numbered" else 0
                if kind == "numbered":
                    piece = f"{numbers}. {piece}"
                elif kind == "bullet":
                    piece = "\u2022 " + piece
            line.runs.append(TextRun(
                text=piece, bold=attrs.weight > _DEFAULT_WEIGHT,
                italic=attrs.italic, underline=attrs.underline,
                strike=attrs.strike, color=attrs.color,
                font_family=attrs.family, size=attrs.size))
    # Keep empty interior lines (blank paragraphs take vertical space).
    while lines and not lines[-1].runs:
        lines.pop()
    if not lines:
        return None
    return TextObject(position=(origin[0] + left, origin[1] + top), size=size,
                      lines=lines, font_size=defaults.size)
