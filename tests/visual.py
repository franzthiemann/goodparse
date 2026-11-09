"""Visual comparison of goodparse PDF output against GoodNotes' own export.

Each ``samples/<name>.goodnotes`` that has a sibling ``<name>.pdf`` (exported
from GoodNotes) is converted to PDF, both documents are rasterised, and every
page gets a score: the mean absolute per-pixel difference (0..255, greyscale)
after resizing ours to the reference raster.  0 = identical; a missing page or
a page-size mismatch shows up as a large score.

Used by ``tests/test_visual.py`` as a regression ratchet and runnable directly
to inspect differences side by side::

    python tests/visual.py OUTDIR [samples/X.goodnotes ...]

which writes ``OUTDIR/<name>_p<N>.png`` (left = reference, right = ours).
"""

from __future__ import annotations

import os
import sys
import tempfile
from typing import List, Optional

import pypdfium2 as pdfium
from PIL import Image, ImageChops, ImageStat

from goodparse import convert_file

DPI = 60
SAMPLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "samples")
MISSING = 255.0  # score of a page that exists in only one of the two documents


def _render(path: str) -> List[Image.Image]:
    """Rasterise every page.  Pages and bitmaps are closed before their
    document: closing a document under live child objects lets pdfium crash
    when they are garbage-collected later."""
    pdf = pdfium.PdfDocument(path)
    out = []
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            bitmap = page.render(scale=DPI / 72)
            out.append(bitmap.to_pil().convert("RGB"))
            bitmap.close()
            page.close()
    finally:
        pdf.close()
    return out


def reference_samples() -> List[str]:
    """Sample names (no extension) that have a GoodNotes reference PDF."""
    names = []
    for f in sorted(os.listdir(SAMPLES)):
        stem, ext = os.path.splitext(f)
        if ext == ".goodnotes" and os.path.exists(os.path.join(SAMPLES, stem + ".pdf")):
            names.append(stem)
    return names


def compare(name: str, outdir: Optional[str] = None) -> List[float]:
    """Return one score per page (max of both page counts) for sample ``name``."""
    src = os.path.join(SAMPLES, name + ".goodnotes")
    ref = os.path.join(SAMPLES, name + ".pdf")
    with tempfile.TemporaryDirectory() as tmp:
        ours_pdf = os.path.join(tmp, name + ".pdf")
        convert_file(src, ours_pdf)
        ours = _render(ours_pdf)
    refs = _render(ref)
    scores = []
    for i in range(max(len(refs), len(ours))):
        if i >= len(refs) or i >= len(ours):
            scores.append(MISSING)
            continue
        r = refs[i]
        o = ours[i]
        # A size mismatch (e.g. pages out of order) must not hide behind the
        # resize: penalise it as a missing page.
        if abs(r.width - o.width) > 2 or abs(r.height - o.height) > 2:
            scores.append(MISSING)
        else:
            o = o.resize(r.size)
            diff = ImageChops.difference(r, o).convert("L")
            scores.append(ImageStat.Stat(diff).mean[0])
        if outdir:
            o = o.resize(r.size)
            canvas = Image.new("RGB", (r.width * 2 + 10, r.height), "red")
            canvas.paste(r, (0, 0))
            canvas.paste(o, (r.width + 10, 0))
            canvas.save(os.path.join(outdir, f"{name}_p{i + 1}.png"))
    return scores


def main(argv: List[str]) -> None:
    outdir = argv[0]
    os.makedirs(outdir, exist_ok=True)
    names = ([os.path.splitext(os.path.basename(a))[0] for a in argv[1:]]
             or reference_samples())
    for name in names:
        scores = compare(name, outdir)
        print(f"{name:10s} " + " ".join(f"{s:6.2f}" for s in scores))


if __name__ == "__main__":
    main(sys.argv[1:])
