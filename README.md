# goodparse

Convert **GoodNotes** (`.goodnotes`) files into editable **Xournal++** (`.xopp`),
**Excalidraw** (`.excalidraw`), or **PDF** (`.pdf`) documents, preserving strokes
(geometry, colour, and per-point width).

The core parser is pure Python with **no third-party runtime dependencies**. PDF
output optionally uses `pypdfium2` + `Pillow` (for backgrounds / images), and PDF
*text* additionally uses `fontTools` **only** to embed real font glyphs when the
matching font happens to be installed on the system (see [PDF export](#pdf-export)).
Nothing is required: without those optional packages the converter still works,
falling back to PDF's built-in base-14 Helvetica for text.

## Install

```bash
pip install -e .
```

## Usage

### CLI

```bash
goodparse notes.goodnotes                    # -> notes.xopp
goodparse notes.goodnotes -o out.xopp        # explicit output
goodparse notes.goodnotes -o out.excalidraw  # format from extension
goodparse notes.goodnotes -f excalidraw      # -> notes.excalidraw
goodparse notes.goodnotes -o out.pdf         # -> out.pdf
goodparse notes.goodnotes -w 1.5 -v          # 1.5x stroke widths, verbose
```

*(The legacy command `goodnotes2xournal` is also kept as an alias.)*

### Library

```python
from goodparse import parse_goodnotes, convert_file

doc = parse_goodnotes("notes.goodnotes")
for page in doc.pages:
    for stroke in page.strokes:
        stroke.points  # [(x, y, width), ...] in PDF points
        stroke.color   # (r, g, b, a) floats 0..1

convert_file("notes.goodnotes", "notes.xopp")
convert_file("notes.goodnotes", "notes.excalidraw")   # format from extension
convert_file("notes.goodnotes", "notes.pdf")
convert_file("notes.goodnotes", fmt="excalidraw")     # explicit format
```

### Excalidraw export

Each stroke becomes a `freedraw` element: GoodNotes' per-point widths map to
Excalidraw pressures (with `strokeWidth` set to the stroke's maximum width),
so thin/thick pens and pressure tapering are preserved, though Excalidraw's
freehand renderer makes the exact thickness an approximation. Pages are laid
out top-to-bottom on the canvas, each outlined by a locked rectangle marking
the page bounds. Colour alpha maps to element opacity.

### PDF export

PDF's coordinate space is a 1:1 match for GoodNotes (PDF points @72 dpi, same
page size) except for the origin: GoodNotes is top-left, PDF is bottom-left, so
each point is Y-flipped per page (`y_pdf = page.height - y`). Because a PDF path
has a *constant* line width, a pressure-varying stroke is drawn as one round-cap
segment per point, each at the mean of its two endpoint widths — overlapping
round caps blend into a smooth stroke whose thickness follows the original
per-point widths. Colour alpha maps to a shared `/ExtGState` (`/ca` and `/CA`).
The file is a hand-built, minimally valid PDF 1.4 (stdlib `zlib` only):
uncompressed dictionaries, Flate-compressed content streams, and a linear
xref/trailer.

#### Text and fonts

Text boxes (and the small "letter" stickers written directly onto a page) are
decoded from the GoodNotes run data — text, per-run colour, and size — and
re-emitted as PDF text. GoodNotes stores a *font family name* per run (e.g.
`Futura`, `Helvetica Neue`) that is **not** one of PDF's 14 base fonts, so there
is a two-tier strategy:

- **Default (no dependencies):** each run is drawn with the built-in
  **Helvetica** family (base-14, `/Helvetica`, `/Helvetica-Bold`, …) using
  Helvetica's advance widths. This always works and keeps the converter
  dependency-free; the glyphs are close but not a pixel match for non-Helvetica
  faces.
- **Optional embedding (when the font is present):** if the named family can be
  matched to a real font *installed on this system*, that font is **subset to the
  characters actually used** and **embedded** in the PDF as a CIDType0C CFF font
  (`/FontFile3`), so the output shows the correct letterforms (e.g. the geometric
  `Futura` of a "GOOD" sticker instead of a Helvetica approximation). The subset
  carries a `ToUnicode` map, so the text stays selectable/searchable.

The family → installed-font match is an **explicit clone map**, not a bare
`fc-match`, because fontconfig does not alias names like `Futura` to a matching
clone (it would pick an unrelated face). The map covers the families seen in
practice:

| GoodNotes family            | installed clone (Debian/Ubuntu package) |
|-----------------------------|------------------------------------------|
| `Futura`, `Futura PT`, …    | URW Gothic (Book/Demi) — `fonts-urw-base35` |
| `Helvetica`, `Helvetica Neue`| Nimbus Sans (Reg/Bold/…) — `fonts-urw-base35` |

So to *enable* real-font embedding, install `fonts-urw-base35` (one package
covers both families) and the optional `fontTools` dependency
(`pip install "goodparse[pdf-fonts]"`) that does the subsetting/embedding. If
the clone isn't installed, or `fontTools` is absent, the converter silently uses
the base-14 fallback — the output is still valid, just with the stand-in face.


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
  scratch in [`applelz4.py`](src/goodparse/applelz4.py).
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

Implemented: multi-page documents, pen/fountain-pen/pencil/marker strokes
(colour, per-point width and pressure), red tape, embedded photos and vector
PDF stickers (as Form XObjects), paper backgrounds, and text boxes plus the
small "letter" stickers — to Xournal++ (`.xopp`), Excalidraw (`.excalidraw`),
and PDF (`.pdf`). PDF text uses the base-14 Helvetica face by default and
embeds the real font when it is installed (see [Text and fonts](#text-and-fonts)).

Not yet handled / known gaps: some pages' display order can differ (no stable
per-shape display index in the file), and the global stroke thickness model is
an approximation. Contributions of sample files exercising new features are
welcome.

## Development

```bash
pip install -e ".[test]"
pytest
```
