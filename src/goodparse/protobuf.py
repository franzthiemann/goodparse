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


def iter_fields_tolerant(data: bytes) -> Iterator[Tuple[int, int, object]]:
    """Like :func:`iter_fields`, but never raises on malformed input.

    The group wire types (3/4) that current GoodNotes writes in its index files
    are yielded with value ``None``; a truncated or unknown field ends the
    iteration instead of raising.
    """
    i = 0
    n = len(data)
    try:
        while i < n:
            key, i = read_varint(data, i)
            field = key >> 3
            wt = key & 7
            if wt == 0:
                val, i = read_varint(data, i)
                yield field, wt, val
            elif wt == 2:
                ln, i = read_varint(data, i)
                if i + ln > n:
                    return
                yield field, wt, data[i:i + ln]
                i += ln
            elif wt == 5:
                if i + 4 > n:
                    return
                yield field, wt, struct.unpack_from("<f", data, i)[0]
                i += 4
            elif wt == 1:
                if i + 8 > n:
                    return
                yield field, wt, struct.unpack_from("<d", data, i)[0]
                i += 8
            elif wt in (3, 4):  # group start/end marker, no payload
                yield field, wt, None
            else:
                return
    except IndexError:  # varint ran off the end
        return


def parse_message_tolerant(data: bytes) -> Dict[int, List[object]]:
    """:func:`parse_message` built on :func:`iter_fields_tolerant`."""
    out: Dict[int, List[object]] = {}
    for field, _wt, val in iter_fields_tolerant(data):
        out.setdefault(field, []).append(val)
    return out


def get_bytes(data: bytes, field_no: int) -> "bytes | None":
    """First length-delimited value of ``field_no`` in ``data``, or ``None``."""
    for field, wt, val in iter_fields_tolerant(data):
        if field == field_no and wt == 2:
            return bytes(val)
    return None
