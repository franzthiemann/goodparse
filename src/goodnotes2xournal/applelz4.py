"""Apple ``libcompression`` LZ4 frame decoder (the ``bv41`` format).

GoodNotes compresses each stroke's geometry with Apple's ``COMPRESSION_LZ4``,
which wraps standard LZ4 *block* data in a custom frame made of chunks:

* ``bv41`` + uint32 decompressed_size + uint32 compressed_size + LZ4 block
* ``bv4-`` + uint32 size + raw (uncompressed) bytes
* ``bv4$`` end-of-stream marker

This is pure Python so the package has no third-party runtime dependencies.
"""

from __future__ import annotations

import struct

MAGIC_COMPRESSED = b"bv41"
MAGIC_UNCOMPRESSED = b"bv4-"
MAGIC_END = b"bv4$"


def lz4_block_decompress(src: bytes, expected_size: int | None = None) -> bytes:
    """Decompress a single standard LZ4 block (no frame header)."""
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1

        literal_len = token >> 4
        if literal_len == 15:
            while True:
                b = src[i]
                i += 1
                literal_len += b
                if b != 0xFF:
                    break
        out += src[i:i + literal_len]
        i += literal_len
        if i >= n:
            break

        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0:
            raise ValueError("invalid LZ4 match offset 0")

        match_len = token & 0x0F
        if match_len == 15:
            while True:
                b = src[i]
                i += 1
                match_len += b
                if b != 0xFF:
                    break
        match_len += 4  # minimum match length

        start = len(out) - offset
        for j in range(match_len):
            out.append(out[start + j])

    if expected_size is not None and len(out) != expected_size:
        raise ValueError(
            f"LZ4 size mismatch: got {len(out)}, expected {expected_size}"
        )
    return bytes(out)


def apple_decompress(blob: bytes) -> bytes:
    """Decode an Apple ``bv41`` framed LZ4 blob into the raw payload."""
    out = bytearray()
    i = 0
    n = len(blob)
    while i < n:
        magic = blob[i:i + 4]
        if magic == MAGIC_COMPRESSED:
            dsize, csize = struct.unpack_from("<II", blob, i + 4)
            i += 12
            out += lz4_block_decompress(blob[i:i + csize], dsize)
            i += csize
        elif magic == MAGIC_UNCOMPRESSED:
            (dsize,) = struct.unpack_from("<I", blob, i + 4)
            i += 8
            out += blob[i:i + dsize]
            i += dsize
        elif magic == MAGIC_END:
            break
        else:
            raise ValueError(f"unexpected Apple-LZ4 chunk magic {magic!r} at {i}")
    return bytes(out)


def is_apple_lz4(blob: bytes) -> bool:
    """Return ``True`` if ``blob`` starts with an Apple-LZ4 frame magic."""
    return blob[:4] in (MAGIC_COMPRESSED, MAGIC_UNCOMPRESSED)
