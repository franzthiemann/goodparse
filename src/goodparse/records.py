"""The object log inside a ``notes/<UUID>`` page file.

A page file is a stream of length-delimited records that come in pairs, one
pair per page object:

* **header** — ``f1`` object UUID, ``f2`` version ``{f1 counter, f2 hash}``,
  ``f3 = 1`` when the object has been **deleted**, ``f9`` a Lamport-style
  sequence number, ``f16`` an object-class code (24 ink/shape, 25 pencil-layer
  ink, 35 text), and — for images only — ``f4`` / ``f7`` attachment UUIDs;
* **body** — a message with exactly one field whose number is the object
  *kind* and whose value starts with the same UUID in its own ``f1``:

  ====  =============================================================
  kind  body
  ====  =============================================================
  1     image placement (matrix; the attachment is in the header)
  7     ink stroke or vector shape
  8     text box (matrix + RTF)
  9     shape fill (bounding box, geometry copy, ``f5`` target stroke)
  21    text object (Cocoa run blob)
  22    connector (arrow between two points, see ``goodparse.page``)
  12    document-flow paragraph (RTF, no geometry; not rendered yet)
  ====  =============================================================

Editing an object rewrites its pair in place (no object id occurs twice in
any sample), and deleting it keeps the pair but sets the header's ``f3``
(mirrored by ``f14`` in the body).  Deleted objects must not be drawn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .protobuf import parse_message_tolerant, read_length_delimited_records

KIND_IMAGE = 1
KIND_STROKE = 7
KIND_TEXTBOX = 8
KIND_FILL = 9
KIND_TEXT = 21
KIND_CONNECTOR = 22


@dataclass
class PageObject:
    """One live object of a page, in file (= creation) order."""

    id: str
    kind: int
    body: bytes                      # the kind field's message
    fields: Dict[int, List[object]]  # ``body`` parsed (top level)
    header: Dict[int, List[object]] = field(default_factory=dict)

    @property
    def attachments(self) -> List[str]:
        """Candidate attachment UUIDs of an image object, most specific first.

        Header ``f7`` (a derived, version-5 UUID) names the stored file when
        present — e.g. images in pages copied from another notebook, whose own
        id (``f4``) has no file of that name; otherwise header ``f4`` does.
        """
        out = []
        for k in (7, 4):
            v = _uuid(self.header.get(k, [None])[0])
            if v:
                out.append(v)
        return out


def _uuid(v) -> Optional[str]:
    if isinstance(v, (bytes, bytearray)):
        s = bytes(v).decode("ascii", "ignore").strip()
        return s or None
    return None


def _is_deleted(header: Dict[int, List[object]], fields: Dict[int, List[object]]) -> bool:
    return header.get(3, [0])[0] == 1 or fields.get(14, [0])[0] == 1


def read_page_objects(data: bytes) -> List[PageObject]:
    """Return the live (non-deleted) objects of a page file in file order.

    A body without a matching header (never seen, handled defensively) is kept
    as a live object with an empty header.
    """
    objects: List[PageObject] = []
    header: Dict[int, List[object]] = {}
    for rec in read_length_delimited_records(data):
        top = parse_message_tolerant(rec)
        if len(top) == 1:
            kind = next(iter(top))
            body = top[kind][0]
            if isinstance(body, (bytes, bytearray)):
                body = bytes(body)
                fields = parse_message_tolerant(body)
                oid = _uuid(fields.get(1, [None])[0]) or ""
                hdr = header if header and _uuid(header.get(1, [None])[0]) == oid else {}
                header = {}
                if not _is_deleted(hdr, fields):
                    objects.append(PageObject(oid, kind, body, fields, hdr))
                continue
        header = top
    return objects
