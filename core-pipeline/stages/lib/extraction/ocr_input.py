"""What the extractors read from a page's OCR.

Every extractor works on word boxes: a key and its value are found by where they sit, not
by the order of the text. Azure Document Intelligence (final2) stores them per page in
``pagesMeta[].words`` as ``{content, polygon}`` with the page's pixel size; the standalone
OCR JSON the module was built on keeps the same words and size at the top of the page.
Both are read here.

Docling / RapidOCR (final1) keeps a box on each text line. Those lines are split
into words and stored on the page, so a page Azure never read — a high-quality
printed page skips the billed call — can still be extracted. Final2 wins when
it has word boxes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


def _usable(word: Any) -> bool:
    """A word with text and a four-corner polygon (the shape util/geometry.py reads)."""
    if not isinstance(word, dict) or not str(word.get("content") or "").strip():
        return False
    polygon = word.get("polygon")
    return isinstance(polygon, (list, tuple)) and len(polygon) >= 8


def _word_source(ocr_page: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The dict holding this page's ``words`` / ``width`` / ``height``, or None."""
    if any(_usable(word) for word in ocr_page.get("words") or []):
        return ocr_page
    for meta in ocr_page.get("pagesMeta") or ocr_page.get("pages_meta") or []:
        if isinstance(meta, dict) and any(_usable(word) for word in meta.get("words") or []):
            return meta
    return None


def has_word_boxes(ocr_page: Optional[dict[str, Any]]) -> bool:
    return isinstance(ocr_page, dict) and _word_source(ocr_page) is not None


def words_from_line(
    text: str, left: float, top: float, right: float, bottom: float
) -> list[dict[str, Any]]:
    """Split one OCR line into word boxes that share its height.

    Final1's reader (RapidOCR inside Docling, or RapidOCR-onnx) returns a box
    per line. The characters of each word take a share of that width, with one
    character of space between words. The polygon is the same four-corner shape
    Azure stores, top-left origin.
    """
    tokens = str(text or "").split()
    if not tokens or right <= left or bottom <= top:
        return []
    total = sum(len(token) for token in tokens) + max(len(tokens) - 1, 0)
    if total <= 0:
        return []
    span = right - left
    cursor = left
    words: list[dict[str, Any]] = []
    for index, token in enumerate(tokens):
        width = span * (len(token) / total)
        words.append({"content": token, "polygon": _polygon(cursor, top, cursor + width, bottom)})
        cursor += width
        if index < len(tokens) - 1:
            cursor += span / total
    return words


def _polygon(left: float, top: float, right: float, bottom: float) -> list[float]:
    return [
        round(left, 2), round(top, 2),
        round(right, 2), round(top, 2),
        round(right, 2), round(bottom, 2),
        round(left, 2), round(bottom, 2),
    ]


def page_for_extraction(
    final2: Optional[dict[str, Any]],
    final1: Optional[dict[str, Any]],
    *,
    page_name: str,
    page_number: Optional[int],
    image_path: Path,
) -> Optional[dict[str, Any]]:
    """The page extraction reads: Final2 word boxes, else Final1."""
    chosen = extraction_page(
        final2, page_name=page_name, page_number=page_number, image_path=image_path
    )
    if chosen is not None:
        return chosen
    return extraction_page(
        final1, page_name=page_name, page_number=page_number, image_path=image_path
    )


def extraction_page(
    ocr_page: Optional[dict[str, Any]],
    *,
    page_name: str,
    page_number: Optional[int],
    image_path: Path,
) -> Optional[dict[str, Any]]:
    """One page in the shape the extractors take, or None when it has no word boxes.

    ``image_path`` is the image the OCR read (the corrected page when rotation changed it):
    the heading detector looks at the same pixels the word boxes were measured on.
    """
    if not isinstance(ocr_page, dict):
        return None
    source = _word_source(ocr_page)
    if source is None:
        return None
    return {
        "pageNumber": page_number,
        "fileName": page_name,
        "width": source.get("width"),
        "height": source.get("height"),
        "words": source["words"],
        "imagePath": str(image_path),
    }
