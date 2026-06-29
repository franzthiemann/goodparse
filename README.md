# goodnotes2xournal

Convert **GoodNotes** (`.goodnotes`) files into editable **Xournal++** (`.xopp`)
documents, preserving strokes (geometry, colour, and per-point width).

Pure Python, no third-party runtime dependencies.

## Install

```bash
pip install -e .
```

## Usage

### CLI

```bash
goodnotes2xournal notes.goodnotes              # -> notes.xopp
goodnotes2xournal notes.goodnotes -o out.xopp  # explicit output
goodnotes2xournal notes.goodnotes -w 1.5 -v    # 1.5x stroke widths, verbose
```

### Library

```python
from goodnotes2xournal import parse_goodnotes, convert_file

doc = parse_goodnotes("notes.goodnotes")
for page in doc.pages:
    for stroke in page.strokes:
        stroke.points  # [(x, y, width), ...] in PDF points
        stroke.color   # (r, g, b, a) floats 0..1

convert_file("notes.goodnotes", "notes.xopp")
```

## Reverse-engineered format notes

A `.goodnotes` file is a **ZIP archive**. The interesting members are
`notes/<UUID>` — one per page — which hold the strokes.

| Layer | Encoding |
|-------|----------|
| Page file | length-delimited protobuf records (`<varint len><message>`) |
| Stroke | protobuf message in field `#7` of a content record |
| Colour | field `#4`: RGBA as float32 sub-fields `1=R 2=G 3=B 4=A` (omitted = 0.0) |
| Geometry | field `#2`: **Apple `libcompression` LZ4** blob (`bv41`…`bv4$` frame) |
| Points | after decompression: float32 `(x, y, width)` triplets, stride 12 |

Key details discovered from the sample files:

- The compression is Apple's framed LZ4: `bv41` + uint32 decompressed size +
  uint32 compressed size + an LZ4 *block*, terminated by `bv4$`. Implemented from
  scratch in [`applelz4.py`](src/goodnotes2xournal/applelz4.py).
- The decompressed buffer starts with `tpl\0` + length, a constant 40-byte style
  template, a small count header, then the point array, then a trailer. The
  rendered path is the **first** run of valid triplets at offset ≥ 64; later runs
  are high-resolution/trailer data and are ignored.
- The third float per point is the **rendered width in points** (GoodNotes bakes
  pen pressure into it), not raw pressure — so thin pressure-varying pens read
  ~0.5–1.4 while a thick fixed pen reads ~3–4.
- Coordinates are **PDF points @72 dpi** and pages are **A4 (595.28 × 841.89 pt)**,
  matching Xournal++'s units and top-left origin, so geometry maps 1:1.

## Status / limitations

Implemented: multi-page documents, pen strokes, colour, per-point width.

Not yet handled (would need more varied samples): highlighter/fountain pen-type
distinctions, eraser strokes, embedded images and PDF page backgrounds, and text
boxes. Contributions of sample files exercising those features are welcome.

## Development

```bash
pip install -e ".[test]"
pytest
```
