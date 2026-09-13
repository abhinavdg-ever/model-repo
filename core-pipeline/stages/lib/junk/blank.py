"""Blank page detection — ported from advantmed-document-processing junk/blank.py.

Fast image heuristic + OCR text check + declared-blank phrasing.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PIL import Image

_BLANK_PHRASE = re.compile(
    r"intentionally\s+blank|\bblank\b",
    re.IGNORECASE,
)


def is_likely_blank_image(
    image: "Image.Image", *, ink_threshold: int = 245, max_ink_pixels: int = 80
) -> bool:
    """Fast pixel check: near-white page with almost no ink (skips OCR)."""
    gray = image.convert("L")
    sample = gray.resize((min(200, gray.width), min(200, gray.height)))
    pixels = sample.getdata()
    dark = sum(1 for p in pixels if p < ink_threshold)
    return dark <= max_ink_pixels


def detect_blank_page(text: str) -> bool:
    """True when OCR is empty or has fewer than 5 alphanumeric characters."""
    if not text:
        return True
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", text)
    return len(cleaned) < 5


def is_declared_blank_page(*, text: str, max_chars: int = 100) -> bool:
    """Blank when short OCR text contains ``intentionally blank`` or ``blank``.

    (No structure extract available in this pack — headings/key-values omitted.)
    """
    body = (text or "").strip()
    if not body or len(body) >= max_chars:
        return False
    return bool(_BLANK_PHRASE.search(body))
