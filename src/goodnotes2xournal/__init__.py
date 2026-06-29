"""Convert GoodNotes files into editable Xournal++ documents.

Public API::

    from goodnotes2xournal import convert_file, parse_goodnotes

    doc = parse_goodnotes("notes.goodnotes")   # -> GoodNotesDocument
    convert_file("notes.goodnotes", "notes.xopp")
"""

from __future__ import annotations

import os

from .goodnotes import GoodNotesDocument, Page, Stroke, parse_goodnotes
from .xournal import DEFAULT_WIDTH_SCALE, build_xml, write_xopp

__all__ = [
    "GoodNotesDocument",
    "Page",
    "Stroke",
    "parse_goodnotes",
    "build_xml",
    "write_xopp",
    "convert_file",
]

__version__ = "0.1.0"


def convert_file(input_path: str, output_path: str | None = None,
                 width_scale: float = DEFAULT_WIDTH_SCALE) -> str:
    """Convert a ``.goodnotes`` file to ``.xopp``; return the output path."""
    if output_path is None:
        stem = os.path.splitext(input_path.rstrip("/"))[0]
        output_path = stem + ".xopp"
    doc = parse_goodnotes(input_path)
    write_xopp(doc, output_path, width_scale=width_scale)
    return output_path
