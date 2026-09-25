"""Convert page images into browser-displayable formats.

Chrome / Firefox / Edge do not render ``image/tiff`` in ``<img>``. TIFF pages
must be re-encoded (JPEG) before the review UI can show them. Same path for
local disk and Entra-proxied blob bytes.

``thumb=1`` returns a small JPEG for the filmstrip so charts with hundreds of
pages do not pull full-resolution files for every thumbnail.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

logger = logging.getLogger("review_ui.images")

_TIFF_SUFFIXES = {".tif", ".tiff"}
_JPEG_QUALITY = 85
_THUMB_QUALITY = 72
_THUMB_MAX_EDGE = 160


def is_tiff_path(path: Path | str) -> bool:
    return Path(path).suffix.lower() in _TIFF_SUFFIXES


def is_tiff_name(name: str) -> bool:
    return Path(name).suffix.lower() in _TIFF_SUFFIXES


def tiff_path_to_jpeg_bytes(path: Path) -> bytes:
    """Open a TIFF on disk and return JPEG bytes (first frame only)."""
    from PIL import Image

    with Image.open(path) as img:
        return _frame_to_jpeg(img, quality=_JPEG_QUALITY)


def tiff_bytes_to_jpeg_bytes(data: bytes) -> bytes:
    """Decode TIFF bytes and return JPEG bytes (first frame only)."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        return _frame_to_jpeg(img, quality=_JPEG_QUALITY)


def path_to_display_jpeg(
    path: Path,
    *,
    thumb: bool = False,
    max_edge: int = _THUMB_MAX_EDGE,
) -> bytes:
    """Load any common page image and return JPEG bytes (optionally thumbnail)."""
    from PIL import Image

    with Image.open(path) as img:
        return _frame_to_jpeg(
            img,
            quality=_THUMB_QUALITY if thumb else _JPEG_QUALITY,
            max_edge=max_edge if thumb else None,
        )


def bytes_to_display_jpeg(
    data: bytes,
    *,
    thumb: bool = False,
    max_edge: int = _THUMB_MAX_EDGE,
) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        return _frame_to_jpeg(
            img,
            quality=_THUMB_QUALITY if thumb else _JPEG_QUALITY,
            max_edge=max_edge if thumb else None,
        )


def _frame_to_jpeg(img, *, quality: int = _JPEG_QUALITY, max_edge: int | None = None) -> bytes:
    # Multi-page TIFFs: show the first frame (each chart page is usually its
    # own file; a multi-frame TIFF still needs *something* visible).
    try:
        img.seek(0)
    except EOFError:
        pass
    frame = img.copy()
    if frame.mode in ("RGBA", "LA", "P"):
        frame = frame.convert("RGB")
    elif frame.mode == "1":
        frame = frame.convert("L").convert("RGB")
    elif frame.mode != "RGB":
        frame = frame.convert("RGB")
    if max_edge and max_edge > 0:
        from PIL import Image as _Image

        frame.thumbnail((max_edge, max_edge), _Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    frame.save(buf, format="JPEG", quality=quality, optimize=False)
    return buf.getvalue()
