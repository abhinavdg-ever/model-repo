"""Convert page images into browser-displayable formats.

Chrome / Firefox / Edge do not render ``image/tiff`` in ``<img>``. TIFF pages
must be re-encoded (JPEG) before the review UI can show them. Same path for
local disk and Entra-proxied blob bytes.

``thumb=1`` returns a small JPEG for the filmstrip so charts with hundreds of
pages do not pull full-resolution files for every thumbnail. Thumbs are cached
under the system temp dir keyed by path + mtime so reopening a chart is cheap.
"""
from __future__ import annotations

import hashlib
import io
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger("review_ui.images")

_TIFF_SUFFIXES = {".tif", ".tiff"}
_JPEG_QUALITY = 85
_THUMB_QUALITY = 70
_THUMB_MAX_EDGE = 120


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
    if thumb:
        cached = _read_thumb_cache(path, max_edge=max_edge)
        if cached is not None:
            return cached

    from PIL import Image

    with Image.open(path) as img:
        data = _frame_to_jpeg(
            img,
            quality=_THUMB_QUALITY if thumb else _JPEG_QUALITY,
            max_edge=max_edge if thumb else None,
        )
    if thumb:
        _write_thumb_cache(path, data, max_edge=max_edge)
    return data


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


def _thumb_cache_path(path: Path, *, max_edge: int) -> Path:
    try:
        st = path.stat()
        stamp = f"{path.resolve()}|{st.st_mtime_ns}|{st.st_size}|{max_edge}"
    except OSError:
        stamp = f"{path}|{max_edge}"
    digest = hashlib.sha1(stamp.encode("utf-8", errors="replace")).hexdigest()
    root = Path(tempfile.gettempdir()) / "review-ui-page-thumbs"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.jpg"


def _read_thumb_cache(path: Path, *, max_edge: int) -> bytes | None:
    cache = _thumb_cache_path(path, max_edge=max_edge)
    try:
        if cache.is_file() and cache.stat().st_size > 0:
            return cache.read_bytes()
    except OSError:
        return None
    return None


def _write_thumb_cache(path: Path, data: bytes, *, max_edge: int) -> None:
    cache = _thumb_cache_path(path, max_edge=max_edge)
    try:
        cache.write_bytes(data)
    except OSError:
        logger.debug("thumb cache write failed path=%s", cache, exc_info=True)


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
