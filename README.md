# goodparse

Convert **GoodNotes** (`.goodnotes`) files into editable **Xournal++** (`.xopp`),
**Excalidraw** (`.excalidraw`), or **PDF** (`.pdf`) documents, preserving strokes
(geometry, colour, and per-point width).

The core parser is pure Python with **no third-party runtime dependencies**. PDF
output optionally uses `pypdfium2` + `Pillow` (for backgrounds / images), and PDF
*text* additionally uses `fontTools` **only** to embed real font glyphs when the
matching font happens to be installed on the system (see [PDF export](#pdf-export)).
Nothing is required: without those optional packages the converter still works,
falling back to PDF's built-in base-14 fonts (Helvetica / Times / Courier)
for text.

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

GoodNotes canvas coordinates map to PDF points by each page's uniform scale,
and the origin flips from top-left to bottom-left
(`y_pdf = page.height - y * scale`). Because a PDF path has a *constant* line
width, a pressure-varying stroke is drawn as one round-cap segment per point,
each at the mean of its two endpoint widths — overlapping round caps blend into
a smooth stroke whose thickness follows the original per-point widths. Shape
fills are painted beneath their outline. Colour alpha maps to a shared `/ExtGState` (`/ca` and `/CA`).
The file is a hand-built, minimally valid PDF 1.4 (stdlib `zlib` only):
uncompressed dictionaries, Flate-compressed content streams, and a linear
xref/trailer.

#### Text and fonts

Text boxes (and the small "letter" stickers written directly onto a page) are
decoded from the GoodNotes run data — text, per-run colour, and size — and
re-emitted as PDF text. GoodNotes stores a *font family name* per run (e.g.
`Futura`, `Helvetica Neue`) that is **not** one of PDF's 14 base fonts, so there
is a two-tier strategy:

- **Default (no dependencies):** each run is drawn with the closest built-in
  base-14 family — **Times** for serif families (e.g. `Georgia`), **Courier**
  for monospace ones (e.g. `Courier New`), **Helvetica** otherwise — placed
  with those fonts' real advance widths
  ([`base14_widths.py`](src/goodparse/base14_widths.py)). This always works
  and keeps the converter dependency-free; the glyphs are close but not a
  pixel match for other faces.
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
| `Georgia`                   | Gelasio if installed, else Nimbus Roman — `fonts-urw-base35` |
| `Times New Roman`           | Nimbus Roman — `fonts-urw-base35` |
| `Courier New`               | Nimbus Mono PS — `fonts-urw-base35` |

So to *enable* real-font embedding, install `fonts-urw-base35` (one package
covers all of these) and the optional `fontTools` dependency
(`pip install "goodparse[pdf-fonts]"`) that does the subsetting/embedding. If
the clone isn't installed, or `fontTools` is absent, the converter silently uses
the base-14 fallback — the output is still valid, just with the stand-in face.


## Reverse-engineered format notes

A `.goodnotes` file is a **ZIP archive**. No schema is published; everything
below was derived from the sample files and checked against GoodNotes' own PDF
exports (`samples/*.pdf`).

| Member | Content |
|--------|---------|
| `notes/<UUID>` | one page: an **object log** of protobuf records |
| `attachments/<UUID>` | paper (background) PDFs, photos, sticker PDFs |
| `index.events.pb` | document event log: page creation, order, paper, deletion |
| `index.notes.pb` | list of page members |

**Page object log** ([`records.py`](src/goodparse/records.py)). Each object is a
*header* record (object UUID, version, `f3 = 1` when **deleted**) followed by a
*body* record whose single field number is the object kind: `7` stroke/shape,
`9` shape fill, `8` RTF text box, `21` text object, `1` image placement,
`22` connector (elbow arrow), `12` "start typing" paragraph (no geometry:
GoodNotes lays these out as page-wide text with margins 44, 31 per indent
level and a fixed line pitch). An image's file is
named by its header `f7` (a derived UUID, e.g. in pages copied between
notebooks) or else `f4`.
Deleted objects stay in the file and must be skipped. A moved object keeps its
geometry and stores the move as a translation in `f6`.

**Stroke paths** ([`ink.py`](src/goodparse/ink.py)). The stroke's `f2` is an
Apple `libcompression` LZ4 blob (`bv41`…`bv4$`, decoder in
[`applelz4.py`](src/goodparse/applelz4.py)) that decompresses to a
**self-describing container**: `tpl\0`, a length, a type signature such as
`vuA(v)A(S(uu))A(S(uuuu))vA(f)` (`v` u16, `u`/`f` 32-bit, `A(…)` counted array,
`S(…)` struct, tightly packed), then the values. Five signatures cover every
sample: pressure pen (per-point half-width), uniform pen / marker /
highlighter (one width), and pencil (x, y, tilt, pressure). Paths are node
flags (move / quadratic segment) plus points; the segments are midpoint
quadratic smoothing of the raw pen samples, so the drawn curve is the
quadratic Bézier.

**Vector shapes and fills** ([`shapes.py`](src/goodparse/shapes.py)). A
shape-recognised stroke keeps exact geometry in `f9`: `f1` polygon, `f2`
quadratic curve, `f3` rectangle (centre + size + rotation), `f4` ellipse
(centre + semi-axes + rotation). A fill object references its outline by UUID
and carries the fill RGBA.

**Text** ([`richtext.py`](src/goodparse/richtext.py),
[`text_rtf.py`](src/goodparse/text_rtf.py)). Text objects hold an LZ4 run blob
(per-run text, colour, font family, size, weight, italic, underline,
strikethrough; per-paragraph alignment and list style, with `-404` meaning "inherit the default"). Older
text boxes hold Cocoa RTF.

**Pages** ([`archive.py`](src/goodparse/archive.py)). Page properties in the
event log are versioned registers: each page has a paper layer (which gives
its canvas size, its paper PDF and — for imported documents — the page of a
multi-page paper PDF) and a **fractional-index position key**; moving
a page or changing its paper writes a newer version. Display order is the
bytewise order of the position keys, not creation order.

**Units.** Coordinates are canvas units with a top-left origin. Each page's
canvas maps to PDF points by a uniform scale (6/11 in every sample: an A4
canvas is 1091 × 1543 units). Uniform stroke widths render at 0.5 pt per
stored unit; pressure pens at twice their stored half-width.

## Status / limitations

Implemented: pen, pressure pen, marker, highlighter and pencil strokes; vector
shapes with colour and fill; moved, resized and recoloured objects; deleted
objects skipped; images and sticker PDFs; text objects (fonts, size, weight,
underline, colour, alignment, lists) and RTF text boxes; per-page size, paper
and display order. Output to Xournal++ (`.xopp`), Excalidraw (`.excalidraw`)
and PDF (`.pdf`). `tests/test_visual.py` scores every page of every sample
against GoodNotes' own export and fails on any regression.

Known gaps:

- **"Start typing" text** is laid out from measured rules
  ([`flowtext.py`](src/goodparse/flowtext.py)); GoodNotes breaks some heading
  lines earlier than their width requires, which is not reproduced yet.
- GoodNotes' regular PDF export drops the indentation of "start typing" text
  (a GoodNotes bug); the visual tests therefore prefer a flattened export
  (`<name>_flat.pdf`) as reference when one exists.

- **Pencil** is drawn as flat colour at reduced opacity; GoodNotes renders a
  grain texture that builds up where strokes overlap, which flat colour can
  only approximate.
- **RTF text boxes** wrap at a narrower width than the stored box width; the
  exact wrap rule is not known yet.
- GoodNotes indents list items slightly; the converter writes the bullet or
  number at the paragraph's left edge.
- Pencil with Apple Pencil pressure and tilt has only one sample (Test9);
  a dedicated pencil calibration sample is still to come.
- Every sample uses the same canvas scale (landscape pages too), so whether uniform widths are
  "0.5 pt" or "11/12 canvas units" per unit can't be told apart (identical at
  that scale).

Contributions of sample files (`.goodnotes` plus GoodNotes' PDF export)
exercising new features are welcome.

## Development

```bash
pip install -e ".[test]"
pytest
```
