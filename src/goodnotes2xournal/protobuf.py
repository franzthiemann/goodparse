"""Minimal pure-Python Protocol Buffers wire-format reader.

GoodNotes files are protobuf, but no ``.proto`` schema is published, so we read
the self-describing wire format directly instead of generating message classes.
Only the four standard wire types are needed:

* 0 = varint
* 1 = 64-bit (fixed64 / double)
* 2 = length-delimited (string, bytes, embedded message, packed repeated)
* 5 = 32-bit (fixed32 / float)
"""

from __future__ import annotations

import struct
from typing import Dict, Iterator, List, Tuple


def read_varint(data: bytes, i: int) -> Tuple[int, int]:
    """Read a base-128 varint at ``i``; return ``(value, next_index)``."""
    shift = 0
    result = 0
    while True:
        b = data[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7


def iter_fields(data: bytes) -> Iterator[Tuple[int, int, object]]:
    """Yield ``(field_number, wire_type, value)`` for a flat message.

    ``value`` is the raw int for varints, a ``bytes`` slice for wire type 2, and
    a Python ``float`` for the fixed 32/64-bit types.
    """
    i = 0
    n = len(data)
    while i < n:
        key, i = read_varint(data, i)
        field = key >> 3
        wt = key & 7
        if wt == 0:
            val, i = read_varint(data, i)
            yield field, wt, val
        elif wt == 2:
            ln, i = read_varint(data, i)
            yield field, wt, data[i:i + ln]
            i += ln
        elif wt == 5:
            yield field, wt, struct.unpack_from("<f", data, i)[0]
            i += 4
        elif wt == 1:
            yield field, wt, struct.unpack_from("<d", data, i)[0]
            i += 8
        else:
            raise ValueError(f"unsupported wire type {wt} at offset {i}")


def parse_message(data: bytes) -> Dict[int, List[object]]:
    """Parse a flat message into ``{field_number: [values...]}``."""
    out: Dict[int, List[object]] = {}
    for field, _wt, val in iter_fields(data):
        out.setdefault(field, []).append(val)
    return out


def is_valid_message(data: bytes) -> bool:
    """Return ``True`` if ``data`` parses cleanly as a protobuf message.

    Used to decide whether a length-delimited field is a nested message or an
    opaque byte string.
    """
    try:
        for _ in iter_fields(data):
            pass
    except (IndexError, ValueError, struct.error):
        return False
    return True


def read_length_delimited_records(data: bytes) -> List[bytes]:
    """Split a stream of ``<varint length><message>`` records.

    GoodNotes ``notes/<UUID>`` files store each page element as one such record.
    """
    records: List[bytes] = []
    i = 0
    n = len(data)
    while i < n:
        ln, i = read_varint(data, i)
        records.append(data[i:i + ln])
        i += ln
    return records
