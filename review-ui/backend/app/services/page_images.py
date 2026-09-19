"""Convert page images into browser-displayable formats.

Chrome / Firefox / Edge do not render ``image/tiff`` in ``<img>``. TIFF pages
must be re-encoded (JPEG) before the review UI can show them. Same path for
local disk and Entra-proxied blob bytes.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

logger = logging.getLogger("review_ui.images")

_TIFF_SUFFIXES = {".tif", ".tiff"}
_JPEG_QUALITY = 90


def is_tiff_path(path: Path | str) -> bool:
    return Path(path).suffix.lower() in _TIFF_SUFFIXES


def is_tiff_name(name: str) -> bool:
    return Path(name).suffix.lower() in _TIFF_SUFFIXES


def tiff_path_to_jpeg_bytes(path: Path) -> bytes:
    """Open a TIFF on disk and return JPEG bytes (first frame only)."""
    from PIL import Image

    with Image.open(path) as img:
        return _frame_to_jpeg(img)


def tiff_bytes_to_jpeg_bytes(data: bytes) -> bytes:
    """Decode TIFF bytes and return JPEG bytes (first frame only)."""
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        return _frame_to_jpeg(img)


def _frame_to_jpeg(img) -> bytes:
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
    buf = io.BytesIO()
    frame.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=False)
    return buf.getvalue()
