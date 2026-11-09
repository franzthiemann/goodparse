"""Decode stroke geometry: the self-describing ``tpl`` container.

A stroke's ``f7.f2`` Apple-LZ4 blob decompresses to::

    b"tpl\\0" | u32 total length | type signature (ASCII) | b"\\0" | values

The **type signature** describes the packed little-endian values that follow,
with no padding or alignment:

* ``v`` — uint16, ``u`` — 32-bit word (a float32 everywhere it matters here),
  ``f`` — float32;
* ``A(T)`` — a uint32 count followed by that many ``T``;
* ``S(T...)`` — a struct of the listed members.

Only five signatures occur across all samples (old and current GoodNotes):

==========================================================  =================
signature                                                    layout
==========================================================  =================
``vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)``                pressure pen
``vuA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)``               pressure (newer,
                                                             default width)
``vuA(v)A(S(uu))A(S(uuuu))vA(f)``                            uniform pen
``vuA(v)A(S(uu))A(S(uuuu))``                                 uniform (older)
``vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)...A(u)``    pencil
==========================================================  =================

All describe one path as **node flags** plus point data.  Flag bit 0 is the
node type — 0 *move to* (starts a sub-path), 1 *quadratic Bézier to*
(control point + end point); in the newer pressure layout bit 1 (value 2)
marks a node that carries its own width (see :func:`_pressure`); bit 2
(value 4) adds trailing floats (1 on a move, 3 on a segment; tape).
Each sub-path left by a partial erase starts with its own move node.
The segments are *midpoint quadratic smoothing* of the raw pen samples: each
control point is an input sample and each end point is exactly the midpoint
of two consecutive controls (8,170 segments across the samples, p95 error
2e-4), so the drawn curve is the quadratic Bézier, not a polyline through
the stored points.

* **Pressure pen** — ``A(u)`` start floats ``(x, y, r)`` and ``A(u)`` segment
  floats ``(cx, cy, cr, ex, ey, er)``; ``r`` is the per-point *half*-width in
  canvas units.  The later arrays (outline / high-resolution samples) are not
  needed for rendering.
* **Uniform pen** (ballpoint, marker, highlighter, shape template) — a single
  width ``u`` then ``A(S(x, y))`` starts and ``A(S(cx, cy, ex, ey))`` segments.
* **Pencil** — width ``u``, then 5-float points ``(x, y, azimuth, altitude,
  pressure)``: starts are ``S(5)``, segments ``S(1 + 5 + 5)`` (a leading
  word, then control and end point).  Strokes drawn without an Apple Pencil
  store altitude pi/3 (1.0472) and pressure 0.  See :func:`_pencil` for
  how tilt and pressure render.

Widths, measured against the reference exports: a pressure pen renders at
twice its half-width (red tape 9.0 -> 9.83 pt at scale 6/11); a uniform
stroke renders at **0.5 pt per unit** (ballpoints 1.56 / 4.68 / 10.39 ->
0.88 / 2.5 / 5.25 pt, highlighters 12 / 24 / 32.2 -> 6.0 / 12.0 / 16.25 pt).
Every sample uses the canvas scale 6/11, so this is stored as 11/12 canvas
units per unit (= 0.5 pt at that scale); whether the true rule is "0.5 pt"
or "11/12 canvas units" cannot be told apart from the available samples.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

Point = Tuple[float, float, float]  # x, y, full width (canvas units)

UNIFORM_WIDTH_FACTOR = 11.0 / 12.0

# Pencil tilt (see _pencil): width factor 1 + PENCIL_TILT * (cot^2(alt) - 1/3),
# fitted on Test_14; altitudes below the most tilted measured one are clamped.
PENCIL_TILT = 1.27
_PENCIL_MIN_ALTITUDE = 0.51
_QUAD_STEPS = 6


class TemplateError(ValueError):
    """The blob is not a well-formed ``tpl`` container."""


# --------------------------------------------------------------------------- #
# Generic container reader
# --------------------------------------------------------------------------- #

def _parse_signature(sig: str, i: int = 0) -> Tuple[list, int]:
    out: list = []
    while i < len(sig):
        c = sig[i]
        if c in "AS":
            if i + 1 >= len(sig) or sig[i + 1] != "(":
                raise TemplateError(f"expected '(' after {c} in {sig!r}")
            sub, i = _parse_signature(sig, i + 2)
            out.append((c, sub))
        elif c == ")":
            return out, i + 1
        elif c in "vuf":
            out.append(c)
            i += 1
        else:
            raise TemplateError(f"unknown type {c!r} in {sig!r}")
    return out, i


def _read(raw: bytes, types: list, off: int) -> Tuple[list, int]:
    vals: list = []
    for t in types:
        if isinstance(t, tuple):
            kind, sub = t
            if kind == "S":
                v, off = _read(raw, sub, off)
                vals.append(tuple(v))
            else:
                (n,) = struct.unpack_from("<I", raw, off)
                off += 4
                arr = []
                for _ in range(n):
                    v, off = _read(raw, sub, off)
                    arr.append(v[0] if len(sub) == 1 else tuple(v))
                vals.append(arr)
        elif t == "v":
            vals.append(struct.unpack_from("<H", raw, off)[0])
            off += 2
        else:  # "u" / "f": 32-bit; every "u" used for geometry is a float
            vals.append(struct.unpack_from("<f", raw, off)[0])
            off += 4
    return vals, off


def read_template(raw: bytes) -> Tuple[str, list]:
    """Return ``(signature, values)`` of a decompressed ``tpl`` blob."""
    if raw[:4] != b"tpl\0":
        raise TemplateError("missing tpl magic")
    end = raw.find(b"\0", 8)
    if end < 0:
        raise TemplateError("unterminated signature")
    sig = raw[8:end].decode("ascii")
    types, _ = _parse_signature(sig)
    try:
        vals, off = _read(raw, types, end + 1)
    except struct.error as exc:
        raise TemplateError(f"truncated values for {sig!r}") from exc
    if off != len(raw):
        raise TemplateError(f"{len(raw) - off} trailing bytes after {sig!r}")
    return sig, vals


# --------------------------------------------------------------------------- #
# Path assembly
# --------------------------------------------------------------------------- #

@dataclass
class Ink:
    kind: str                  # "pressure" | "uniform" | "pencil"
    subpaths: List[List[Point]]
    pressure: float = 0.0      # pencil: mean recorded pressure (0 = none)


def _quad(p0: Point, c: Point, p1: Point, out: List[Point]) -> None:
    """Append samples of the quadratic Bézier p0 -> p1 (control c), sans p0."""
    for i in range(1, _QUAD_STEPS + 1):
        t = i / _QUAD_STEPS
        a, b, d = (1 - t) ** 2, 2 * (1 - t) * t, t * t
        out.append((a * p0[0] + b * c[0] + d * p1[0],
                    a * p0[1] + b * c[1] + d * p1[1],
                    a * p0[2] + b * c[2] + d * p1[2]))


def _assemble(flags: Sequence[int], moves: List[Point],
              quads: List[Tuple[Point, Point]]) -> List[List[Point]]:
    paths: List[List[Point]] = []
    mi = qi = 0
    for flag in flags:
        if flag > 7:
            raise TemplateError(f"unknown node flag {flag}")
        if flag & 1 == 0:
            if mi >= len(moves):
                raise TemplateError("more move nodes than start points")
            paths.append([moves[mi]])
            mi += 1
        else:
            if qi >= len(quads) or not paths:
                raise TemplateError("segment without data or start point")
            c, e = quads[qi]
            qi += 1
            _quad(paths[-1][-1], c, e, paths[-1])
    return [p for p in paths if p]


def _pressure(flags: Sequence[int], start: List[float], seg: List[float],
              default_r: Optional[float] = None) -> Ink:
    """Pressure-pen nodes.

    Older layout (``default_r`` None): every node carries its half-width,
    interleaved — move ``(x, y, r)``, segment ``(cx, cy, cr, ex, ey, er)``.
    Newer layout: a node carries its own half-width only when flag bit 1 is
    set — move ``(x, y[, r])``, segment ``(cx, cy, ex, ey[, cr, er])`` — and
    otherwise uses the stroke's leading half-width ``default_r`` (stored as -1
    when every node has its own).  Bit 2 appends extra floats in both.  The
    flags must consume both arrays exactly.
    """
    newer = default_r is not None
    moves: List[Point] = []
    quads: List[Tuple[Point, Point]] = []
    si = gi = 0
    for flag in flags:
        extra = bool(flag & 4)
        own = not newer or bool(flag & 2)
        if flag & 1 == 0:
            if newer:
                x, y = start[si:si + 2]
                r = start[si + 2] if own else default_r
            else:
                x, y, r = start[si:si + 3]
            moves.append((x, y, 2.0 * r))
            si += (3 if own else 2) + (1 if extra else 0)
        else:
            if not newer:
                cx, cy, cr, ex, ey, er = seg[gi:gi + 6]
            elif own:
                cx, cy, ex, ey, cr, er = seg[gi:gi + 6]
            else:
                cx, cy, ex, ey = seg[gi:gi + 4]
                cr = er = default_r
            quads.append(((cx, cy, 2.0 * cr), (ex, ey, 2.0 * er)))
            gi += (6 if own else 4) + (3 if extra else 0)
    if si != len(start) or gi != len(seg):
        raise TemplateError(
            f"node flags consume {si}/{gi} of {len(start)}/{len(seg)} floats")
    return Ink("pressure", _assemble(flags, moves, quads))


def _uniform(vals: list) -> Ink:
    w = vals[1] * UNIFORM_WIDTH_FACTOR
    moves = [(x, y, w) for x, y in vals[3]]
    quads = [((cx, cy, w), (ex, ey, w)) for cx, cy, ex, ey in vals[4]]
    return Ink("uniform", _assemble(vals[2], moves, quads))


def _pencil(vals: list) -> Ink:
    """Pencil nodes ``(x, y, azimuth, altitude, pressure)``.

    Measured on Test_14 (light / medium / heavy / upright / tilted lines at
    two pencil sizes, 10 strokes): the **altitude** sets the width — tilting
    the pencil shades a wide band — while **pressure** barely changes the
    width but darkens the grain (handled by the caller via ``Ink.pressure``).
    The rendered width is ``w * (1 + PENCIL_TILT * (cot^2(alt) - 1/3))``:
    exactly ``w`` at the finger default altitude pi/3, about 4.5 ``w`` at
    alt 0.51 and 0.6 ``w`` upright.  An empirical fit (relative RMS 24 %; the
    grain texture makes single-stroke widths noisy), not a known formula.
    """
    w = vals[1] * UNIFORM_WIDTH_FACTOR
    pressures: List[float] = []

    def pt(x, y, _azi, alt, pressure) -> Point:
        if pressure > 0:
            pressures.append(pressure)
        cot = 1.0 / math.tan(min(max(alt, _PENCIL_MIN_ALTITUDE), math.pi / 2))
        return (x, y, w * (1.0 + PENCIL_TILT * (cot * cot - 1.0 / 3.0)))

    moves = [pt(*s) for s in vals[3]]
    quads = [(pt(*s[1:6]), pt(*s[6:11])) for s in vals[4]]
    mean = sum(pressures) / len(pressures) if pressures else 0.0
    return Ink("pencil", _assemble(vals[2], moves, quads), mean)


def decode_ink(raw: bytes) -> Optional[Ink]:
    """Decode a decompressed stroke blob; ``None`` when it holds no path
    (a vector shape's blob is a bare style template)."""
    sig, vals = read_template(raw)
    if sig.startswith("vA(v)A(u)A(u)"):
        ink = _pressure(vals[1], vals[2], vals[3])
    elif sig.startswith("vuA(v)A(u)A(u)"):  # newer: default half-width first
        ink = _pressure(vals[2], vals[3], vals[4], default_r=vals[1])
    elif sig.startswith("vuA(v)A(S(uu))A(S(uuuu))"):
        ink = _uniform(vals)
    elif sig.startswith("vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))"):
        ink = _pencil(vals)
    else:
        raise TemplateError(f"unknown stroke layout {sig!r}")
    return ink if ink.subpaths else None
