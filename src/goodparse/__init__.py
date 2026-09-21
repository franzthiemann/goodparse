"""Convert GoodNotes files into editable Xournal++ or Excalidraw documents.

Public API::

    from goodparse import convert_file, parse_goodnotes

    doc = parse_goodnotes("notes.goodnotes")   # -> GoodNotesDocument
    convert_file("notes.goodnotes", "notes.xopp")
    convert_file("notes.goodnotes", "notes.excalidraw")  # format from extension
"""

from __future__ import annotations

import os

from .excalidraw import build_scene, write_excalidraw
from .goodnotes import GoodNotesDocument, Page, Stroke, parse_goodnotes
from .xournal import DEFAULT_WIDTH_SCALE, build_xml, write_xopp

__all__ = [
    "GoodNotesDocument",
    "Page",
    "Stroke",
    "parse_goodnotes",
    "build_xml",
    "write_xopp",
    "build_scene",
    "write_excalidraw",
    "convert_file",
]

__version__ = "0.1.0"

FORMATS = ("xopp", "excalidraw")

_WRITERS = {"xopp": write_xopp, "excalidraw": write_excalidraw}


def _infer_format(output_path: str | None, fmt: str | None) -> str:
    """Resolve the output format: explicit ``fmt`` wins, else the output
    extension, else xopp."""
    if fmt is not None:
        if fmt not in FORMATS:
            raise ValueError(f"unknown format {fmt!r}; expected one of {FORMATS}")
        return fmt
    if output_path is not None:
        ext = os.path.splitext(output_path)[1].lstrip(".").lower()
        if ext in FORMATS:
            return ext
    return "xopp"


def convert_file(input_path: str, output_path: str | None = None,
                 width_scale: float = DEFAULT_WIDTH_SCALE,
                 fmt: str | None = None) -> str:
    """Convert a ``.goodnotes`` file to ``.xopp`` or ``.excalidraw``.

    The format is taken from ``fmt`` if given, otherwise inferred from the
    output extension (defaulting to xopp). Returns the output path.
    """
    fmt = _infer_format(output_path, fmt)
    if output_path is None:
        stem = os.path.splitext(input_path.rstrip("/"))[0]
        output_path = f"{stem}.{fmt}"
    doc = parse_goodnotes(input_path)
    _WRITERS[fmt](doc, output_path, width_scale=width_scale)
    return output_path
