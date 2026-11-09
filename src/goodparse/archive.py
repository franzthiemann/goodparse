"""Read a ``.goodnotes`` archive: page members, attachments and the
document event log (``index.events.pb``).

The event log records, per page, versioned registers for its paper layer and
its position key (see :func:`_read_page_meta`), page deletions, and the
layers' canvas size and paper.  ``index.notes.pb`` lists the page members.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .model import Page
from .page import parse_page
from .protobuf import (
    iter_fields_tolerant,
    parse_message_tolerant,
    read_length_delimited_records,
)

def _page_members(names) -> List[str]:
    return sorted(n for n in names if "/notes/" in n or n.startswith("notes/"))


def _read_deleted_pages(opener, names) -> List[str]:
    """Return the full 36-char ``notes/`` member names of *deleted* pages.

    ``index.events.pb`` is the op log.  A page is created by an op carrying
    field #54 and later deleted/hidden by an op carrying field #56 (the same
    page UUID).  Deleted members are kept in the archive but must not produce
    a page (the reference export omits them).  Note the op log stores an
    *internal* page id that differs from the member name by one nibble in the
    final hex byte, so matching is on the first 32 chars.
    """
    if "index.events.pb" not in names:
        return []
    try:
        data = opener("index.events.pb")
    except Exception:
        return []
    deleted = set()
    for rec in read_length_delimited_records(data):
        try:
            top = parse_message_tolerant(rec)
        except Exception:
            continue
        f1 = top.get(1, [None])[0]
        if not isinstance(f1, (bytes, bytearray)):
            continue
        fields = {k for k in top if k != 1}
        if 56 in fields:
            deleted.add(bytes(f1).decode("ascii", "ignore").strip()[:32])
    if not deleted:
        return []
    out = []
    for n in names:
        if "/notes/" in n or n.startswith("notes/"):
            if n.split("/")[-1][:32] in deleted:
                out.append(n)
    return out


def _read_page_order(opener, names, fallback) -> List[str]:
    """Return page member names in document order.

    ``index.notes.pb`` lists the pages in their display order (each record's
    field #1 is a page UUID); we map those to ``notes/<UUID>`` members.  When
    the index is absent we fall back to the sorted member list.
    """
    if "index.notes.pb" not in names:
        return list(fallback)
    try:
        data = opener("index.notes.pb")
    except Exception:
        return list(fallback)
    nameset = set(names)
    ordered = []
    for rec in read_length_delimited_records(data):
        top = parse_message_tolerant(rec)
        if top.get(1) and isinstance(top[1][0], (bytes, bytearray)):
            member = "notes/" + bytes(top[1][0]).decode("ascii", "ignore").strip()
            if member in nameset and member not in ordered:
                ordered.append(member)
    for n in fallback:  # keep any pages missing from the index (defensive)
        if n not in ordered:
            ordered.append(n)
    return ordered


def _read_page_members(opener, names, attachments) -> List[Page]:
    """Parse every live ``notes/<UUID>`` member in display order, keeping
    empty pages (a page may hold only a background).

    Per-page geometry and paper come from the event log (see
    :func:`_read_page_meta`): each page has its own canvas, so documents can
    mix page sizes (A4 + landscape + strips).  A page the log does not
    describe falls back to A4 at scale 1.
    """
    member_names = _page_members(names)
    meta = _read_page_meta(opener, names, member_names)
    per_page = {m.split("/")[-1]: (c, p) for m, _layer, c, p in meta if m}

    order: List[str] = [m for m, _l, _c, _p in meta if m]
    for m in _read_page_order(opener, names, member_names):  # pages not in the log
        if m not in order:
            order.append(m)

    deleted = set(_read_deleted_pages(opener, names))
    paper_att = _read_paper_to_attachment(opener, names)

    pages = []
    for name in order:
        if name in deleted:
            continue
        canvas, paper = per_page.get(name.split("/")[-1], (None, None))
        # a paper UUID may be an internal id; map it to its real attachment
        paper = paper_att.get(paper, paper) if paper else None
        size = _paper_size(opener, names, paper) if canvas else None
        page = parse_page(opener(name), attachments, canvas, size)
        if paper and paper in attachments:
            page.background = attachments[paper]
        pages.append(page)
    return pages


_UUID_RE = re.compile(
    rb"([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})")


def _uuid_from(v) -> Optional[str]:
    if not isinstance(v, (bytes, bytearray)):
        return None
    m = _UUID_RE.search(bytes(v))
    return m.group(1).decode("ascii").upper() if m else None


def _paper_size(opener, names, paper) -> Optional[Tuple[float, float]]:
    """Page size from a paper PDF attachment's MediaBox ``[0 0 W H]``."""
    if not paper or f"attachments/{paper}" not in names:
        return None
    try:
        data = opener(f"attachments/{paper}")
    except Exception:
        return None
    if not data.startswith(b"%PDF"):
        return None
    m = re.search(
        rb"/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]", data)
    if not m:
        m = re.search(rb"/MediaBox\s*\[0\s+0\s+([\d.]+)\s+([\d.]+)\]", data)
        if m:
            return (float(m.group(1)), float(m.group(2)))
        return None
    return (float(m.group(3)), float(m.group(4)))


def _read_page_meta(opener, names, member_names) -> List[Tuple[str, str,
                                                                Optional[Tuple[float, float]], Optional[str]]]:
    """Derive per-page metadata from ``index.events.pb``.

    Returns ``[(member_name, layer, canvas_wh_or_None, paper_uuid_or_None)]`` in
    display order.  Each entry resolves a page's ``f54`` create op (page UUID in
    field 2, layer in field 3) to that layer's create-info op (top field 1 =
    layer, nested field ``f2.f4`` = paper, ``f2.f8`` = canvas dims).  When the
    event log is absent or carries no canvas dims this returns ``[]`` and the
    caller falls back to the single global canvas/page size.
    """
    if "index.events.pb" not in names:
        return []
    try:
        data = opener("index.events.pb")
    except Exception:
        return []
    try:
        recs = list(read_length_delimited_records(data))
    except Exception:
        return []

    # 1) layer create-info ops: top f1 = layer UUID, f2 = info message.
    layer_info: Dict[str, Tuple[Optional[str], Optional[Tuple[float, float]]]] = {}
    for rec in recs:
        t = parse_message_tolerant(rec)
        if 1 not in t or 2 not in t:
            continue
        layer = _uuid_from(t[1][0])
        info = t[2][0]
        if not layer or not isinstance(info, (bytes, bytearray)):
            continue
        paper: Optional[str] = None
        cxy: Optional[Tuple[float, float]] = None
        for f, _wt, v in iter_fields_tolerant(info):
            if f == 4:
                paper = _uuid_from(v) or paper
            elif f == 8 and isinstance(v, (bytes, bytearray)):
                cf = parse_message_tolerant(v)
                if 1 in cf and 2 in cf:
                    w, h = cf[1][0], cf[2][0]
                    if 100 < w < 5000 and 100 < h < 5000:
                        cxy = (float(w), float(h))
        if cxy:
            layer_info[layer] = (paper, cxy)
        elif paper:
            # paper known but no canvas yet; keep the best (canvas) if seen
            prev = layer_info.get(layer)
            if not prev or not prev[1]:
                layer_info[layer] = (paper, prev[1] if prev else None)

    # 2) pages.  Page properties are versioned registers
    #    ``{1: value, 2: {1: version}}``; the highest version wins.
    #    * f54 creates a page (UUID f2) with its paper layer (f3) and position
    #      key (f4);
    #    * a top-level f3 op re-assigns the paper layer (f3) — changing a
    #      page's paper after creation (Test6 page 2: ruled -> plain);
    #    * f55 moves a page: new position key in f3.
    #    Display order is NOT creation order: the position key is a
    #    fractional index ("4mgFG1" < "Am5Pm" < "L2FtP" < "O9t!N"); pages sort
    #    by their latest key, bytewise.
    member_prefix = {p32: m for p32, m in
                     ((m[-36:][:32], m) for m in member_names)}
    created: List[str] = []
    layer: Dict[str, Tuple[int, bytes]] = {}
    position: Dict[str, Tuple[int, bytes]] = {}

    def assign(reg: Dict[str, Tuple[int, bytes]], page: str, msg) -> None:
        v = _versioned(msg)
        if v and v[0] >= reg.get(page, (-1, b""))[0]:
            reg[page] = v

    for rec in recs:
        t = parse_message_tolerant(rec)
        for op in (54, 3, 55):
            if op not in t or not isinstance(t[op][0], (bytes, bytearray)):
                continue
            body = parse_message_tolerant(t[op][0])
            page = _uuid_from(body.get(2, [None])[0])
            if not page:
                continue
            if op == 54:
                created.append(page)
                assign(layer, page, body.get(3, [None])[0])
                assign(position, page, body.get(4, [None])[0])
            elif op == 3:
                assign(layer, page, body.get(3, [None])[0])
            else:
                assign(position, page, body.get(3, [None])[0])
    out: List[Tuple[str, str, Optional[Tuple[float, float]], Optional[str]]] = []
    for page in created:
        member = member_prefix.get(page[:32])
        lay = _uuid_from(layer[page][1]) if page in layer else None
        paper, cxy = layer_info.get(lay, (None, None)) if lay else (None, None)
        out.append((member, lay, cxy, paper))
    # Stable sort: pages without a key keep their creation order at the front.
    keyed = sorted(range(len(out)),
                   key=lambda i: position.get(created[i], (0, b""))[1])
    return [out[i] for i in keyed]


def _versioned(msg) -> Optional[Tuple[int, bytes]]:
    """Decode a versioned register ``{1: value bytes, 2: {1: version}}``."""
    if not isinstance(msg, (bytes, bytearray)):
        return None
    f = parse_message_tolerant(bytes(msg))
    value = f.get(1, [None])[0]
    if not isinstance(value, (bytes, bytearray)) or not value:
        return None
    ver = f.get(2, [None])[0]
    version = 0
    if isinstance(ver, (bytes, bytearray)):
        v = parse_message_tolerant(bytes(ver)).get(1, [0])[0]
        version = v if isinstance(v, int) else 0
    return version, bytes(value)

def _collect_attachments(opener, names) -> Dict[str, bytes]:
    out: Dict[str, bytes] = {}
    for n in names:
        if n.startswith("attachments/"):
            data = opener(n)
            if data:
                out[n.split("/")[-1]] = data
    return out


def _read_paper_to_attachment(opener, names) -> Dict[str, str]:
    """Map an *internal* paper UUID to its backing ``attachments/`` UUID.

    Most papers are stored under their own UUID, but some (e.g. imported or
    template papers) have an internal id that differs from the attachment
    filename.  An op whose field #1 is the paper UUID and whose field #6
    sub-message's field #2 is the attachment UUID records that mapping.
    Papers that map to themselves are the common case; the mapping is only
    needed to resolve the rest.
    """
    if "index.events.pb" not in names:
        return {}
    try:
        data = opener("index.events.pb")
    except Exception:
        return {}
    out: Dict[str, str] = {}
    try:
        recs = list(read_length_delimited_records(data))
    except Exception:
        return {}
    for rec in recs:
        t = parse_message_tolerant(rec)
        if 1 not in t or 6 not in t:
            continue
        paper = _uuid_from(t[1][0])
        v6 = t[6][0]
        if not paper or not isinstance(v6, (bytes, bytearray)):
            continue
        sub = parse_message_tolerant(v6)
        if 2 in sub:
            att = _uuid_from(sub[2][0])
            if att:
                out[paper] = att
    return out
