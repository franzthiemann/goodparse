"""Parse GoodNotes ``.goodnotes`` archives into a simple drawing model.

Format overview (reverse-engineered; README has the full write-up):

* The file is a ZIP archive.  ``notes/<UUID>`` members are pages,
  ``attachments/<UUID>`` hold paper PDFs and images, and ``index.events.pb``
  is the document event log (page order, size, paper, deletions) —
  :mod:`goodparse.archive`.
* A page file is an object log of header/body record pairs; deleted objects
  stay in it as tombstones — :mod:`goodparse.records`.
* Objects: strokes (path in a self-describing ``tpl`` blob,
  :mod:`goodparse.ink`), vector shapes and fills (:mod:`goodparse.shapes`),
  images, RTF text boxes (:mod:`goodparse.text_rtf`) and text objects
  (:mod:`goodparse.richtext`) — assembled by :mod:`goodparse.page`.

Coordinates are canvas units with a top-left origin; each page's canvas maps
to PDF points by a uniform scale (6/11 in every sample).

This module keeps the historical public names: the model classes,
:func:`parse_page` and :func:`parse_goodnotes`.
"""

from __future__ import annotations

import os
import zipfile

from .archive import _collect_attachments, _read_page_members
from .model import (  # noqa: F401  (re-exported public API)
    A4_HEIGHT_PT,
    A4_WIDTH_PT,
    GoodNotesDocument,
    Image,
    Page,
    Stroke,
    TextBox,
)
from .page import parse_page  # noqa: F401  (re-exported)

__all__ = ["A4_HEIGHT_PT", "A4_WIDTH_PT", "GoodNotesDocument", "Image", "Page",
           "Stroke", "TextBox", "parse_goodnotes", "parse_page"]


def parse_goodnotes(path: str) -> GoodNotesDocument:
    """Parse a ``.goodnotes`` archive (or an already-extracted directory)."""
    title = os.path.splitext(os.path.basename(path.rstrip("/")))[0]
    if os.path.isdir(path):
        names = [os.path.relpath(os.path.join(root, f), path)
                 for root, _dirs, files in os.walk(path) for f in files]

        def opener(name):
            with open(os.path.join(path, name), "rb") as fh:
                return fh.read()

        return GoodNotesDocument(
            pages=_read_page_members(opener, names, _collect_attachments(opener, names)),
            title=title)
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        return GoodNotesDocument(
            pages=_read_page_members(zf.read, names, _collect_attachments(zf.read, names)),
            title=title)
