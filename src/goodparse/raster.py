"""Raster media for the writers: images and page paper as PNG/JPEG.

Xournal++ and Excalidraw only display raster images, while GoodNotes stores
photos (PNG/JPEG), vector stickers (small PDFs) and paper (PDF pages).  This
module turns each into bytes those formats can show, the same way for both.
The PDF writer embeds photos itself but shares the rotation rule.

Needs Pillow (decode / re-encode) and pypdfium2 (render PDFs); without them
the functions return ``None`` and the caller skips the item with a warning.
"""

from __future__ import annotations

import io
import warnings
from dataclasses import dataclass
from typing import Optional

from .model import Image

# Vector stickers are rasterised at this resolution of their placed size.
STICKER_DPI = 300.0
# Paper is rendered at this multiple of its page size (2 = 144 dpi), like
# the PDF writer's backgrounds, and stored as JPEG of this quality.
PAPER_SCALE = 2.0
JPEG_QUALITY = 86

_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8"


@dataclass
class Raster:
    data: bytes
    mime: str        # "image/png" | "image/jpeg"
    width: int       # pixels
    height: int


def is_rotated(width: float, height: float, place_w: float, place_h: float) -> bool:
    """True when GoodNotes shows an image turned by 90 degrees: the placed box
    has the swapped aspect of the image (portrait box, landscape pixels or vice
    versa).  ``abs(native - placed) > 0.15`` and ``native * placed ~ 1`` catch
    4:3/3:4, 16:9/9:16 etc.; a zero box size disables the check."""
    if place_w <= 0 or place_h <= 0 or width <= 0 or height <= 0:
        return False
    native, placed = width / height, place_w / place_h
    return abs(native - placed) > 0.15 and abs(native * placed - 1.0) < 0.5


def render_pdf_page(data: bytes, index: int = 0, scale: float = 1.0,
                    transparent: bool = False):
    """Page ``index`` of PDF ``data`` as a PIL image (RGBA when
    ``transparent``, else RGB on white), or ``None``."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None
    try:
        doc = pdfium.PdfDocument(data)
    except Exception:
        return None
    try:
        page = doc[index if index < len(doc) else 0]
        kwargs = {"fill_color": (255, 255, 255, 0)} if transparent else {}
        bitmap = page.render(scale=scale, **kwargs)
        im = bitmap.to_pil()
        im.load()
        bitmap.close()
        page.close()
        return im if transparent else im.convert("RGB")
    finally:
        doc.close()


def _encode(im, mime: str) -> Raster:
    buf = io.BytesIO()
    if mime == "image/jpeg":
        im.convert("RGB").save(buf, format="JPEG", quality=JPEG_QUALITY)
    else:
        im.save(buf, format="PNG")
    return Raster(buf.getvalue(), mime, im.width, im.height)


def image_raster(image: Image, page_scale: float) -> Optional[Raster]:
    """A photo or sticker as PNG/JPEG, turned the way GoodNotes shows it."""
    data = image.data
    try:
        from PIL import Image as PILImage
    except ImportError:
        PILImage = None
    try:
        if data[:4] == b"%PDF":
            # vector sticker: rasterise at STICKER_DPI of its placed width
            probe = render_pdf_page(data, 0, 1.0, transparent=True)
            if probe is None:
                raise ValueError("cannot render the sticker PDF")
            k = STICKER_DPI / 72.0 * image.size[0] * page_scale / probe.width
            return _encode(render_pdf_page(data, 0, k, transparent=True), "image/png")
        if PILImage is None:
            if data[:8] == _PNG or data[:2] == _JPEG:
                return Raster(data, "image/png" if data[:8] == _PNG else "image/jpeg", 0, 0)
            raise ValueError("Pillow is needed to decode this image")
        im = PILImage.open(io.BytesIO(data))
        mime = "image/jpeg" if data[:2] == _JPEG else "image/png"
        if is_rotated(im.width, im.height, image.size[0], image.size[1]):
            return _encode(im.rotate(270, expand=True), mime)   # CCW 90 degrees
        if data[:8] == _PNG or data[:2] == _JPEG:
            return Raster(data, mime, im.width, im.height)
        return _encode(im, "image/png")
    except Exception as exc:
        warnings.warn(f"skipping an image that cannot be decoded: {exc}")
        return None


def paper_raster(background: bytes, index: int) -> Optional[Raster]:
    """Page ``index`` of a paper PDF as a JPEG at PAPER_SCALE."""
    im = render_pdf_page(background, index, PAPER_SCALE)
    if im is None:
        warnings.warn("cannot render the page's paper (pypdfium2 missing?)")
        return None
    return _encode(im, "image/jpeg")
