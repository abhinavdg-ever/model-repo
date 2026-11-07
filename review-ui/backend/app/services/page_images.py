"""Convert page images into browser-displayable formats.

Chrome / Firefox / Edge do not render ``image/tiff`` in ``<img>``. TIFF pages
must be re-encoded (JPEG) before the review UI can show them. Same path for
local disk and Entra-proxied blob bytes.

``thumb=1`` returns a small JPEG for the filmstrip so charts with hundreds of
pages do not pull full-resolution files for every thumbnail. Thumbs are cached
under the system temp dir keyed by path + mtime (local) or key + ETag (blob)
so reopening a chart is cheap.
"""
from __future__ import annotations

import hashlib
import io
import logging
import tempfile
from pathlib import Path
from typing import Callable

logger = logging.getLogger("review_ui.images")

# Formats a browser <img> cannot show: re-encoded to JPEG on the way out. The
# is_tiff_* names predate the non-TIFF entries (JPEG 2000, Netpbm, DIB).
_TIFF_SUFFIXES = {".tif", ".tiff", ".jp2", ".j2k", ".pbm", ".pgm", ".ppm", ".pnm", ".dib"}
_JPEG_QUALITY = 85
_THUMB_QUALITY = 70
_THUMB_MAX_EDGE = 120


def is_tiff_name(name: str) -> bool:
    return Path(name).suffix.lower() in _TIFF_SUFFIXES


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


def _cache_file(stamp: str) -> Path:
    digest = hashlib.sha1(stamp.encode("utf-8", errors="replace")).hexdigest()
    root = Path(tempfile.gettempdir()) / "review-ui-page-thumbs"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.jpg"


def cached_derived_jpeg(stamp: str, produce: Callable[[], bytes]) -> bytes:
    """Disk-cache a derived JPEG (thumb / TIFF conversion) under ``stamp``.

    ``stamp`` must change whenever the source changes — for blobs, include the
    ETag. Saves re-downloading and re-encoding the source on every request.
    """
    cache = _cache_file(stamp)
    try:
        if cache.is_file() and cache.stat().st_size > 0:
            return cache.read_bytes()
    except OSError:
        pass
    data = produce()
    try:
        cache.write_bytes(data)
    except OSError:
        logger.debug("derived image cache write failed path=%s", cache, exc_info=True)
    return data


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
