"""End-of-stage quality label post-processing (from image_preprocessing).

Does not change ``quality_score`` — only the discrete ``quality_tag``.
"""
from __future__ import annotations

import re
from collections import Counter


# High >= 0.70, Medium 0.40-0.70, Low < 0.40 (2026-11-14, user: kept after pricing 0.80 / 0.50
# and 0.75 / 0.50 against Doc Intel volume).
QUALITY_HIGH_AT = 0.70
QUALITY_MEDIUM_AT = 0.40


def base_quality_label(score_01: float | None) -> str | None:
    """Map normalized score in [0, 1] to high / medium / low."""
    if score_01 is None:
        return None
    s = float(score_01)
    if s >= QUALITY_HIGH_AT:
        return "high"
    if s >= QUALITY_MEDIUM_AT:
        return "medium"
    return "low"


def apply_quality_label_postprocess(
    *,
    quality_tag: str | None,
    quality_score: float | None,
    document_type: str | None,
) -> str | None:
    """Assign quality_tag; a handwritten page type + High is downgraded to Medium.

    Matches the teammate ``image_preprocessing`` rule: a high engineering
    score on a handwritten page is reported as medium so reviewers do not
    read it as a clean printed scan.
    """
    tag = (quality_tag or "").strip().lower() or base_quality_label(quality_score)
    if not tag:
        return None
    if (document_type or "").strip().lower() == "handwritten" and tag == "high":
        return "medium"
    return tag


# Docling (Final OCR 1) against Tesseract (prelim): below 50% word overlap the
# tag drops one level, below 10% it drops to Low. Runs before Final OCR 2
# decides which pages skip Azure, so a downgraded page is sent to Azure.
OCR_OVERLAP_ONE_LEVEL = 0.50
OCR_OVERLAP_TO_LOW = 0.10
_ONE_LEVEL_DOWN = {"high": "medium", "medium": "low", "low": "low"}
_WORD = re.compile(r"[a-z0-9]+")


def ocr_overlap(docling_text: str | None, tesseract_text: str | None) -> float | None:
    """Word overlap of the two OCR texts in [0, 1] (Dice on lowercase word counts).

    None when both are empty: nothing to compare.
    """
    a = Counter(_WORD.findall((docling_text or "").lower()))
    b = Counter(_WORD.findall((tesseract_text or "").lower()))
    total = sum(a.values()) + sum(b.values())
    if not total:
        return None
    return round(2 * sum((a & b).values()) / total, 4)


def downgrade_by_ocr_overlap(quality_tag: str | None, overlap: float | None) -> str | None:
    """< 10% → low; < 50% → one level down (high → medium, medium → low)."""
    tag = (quality_tag or "").strip().lower()
    if not tag or overlap is None:
        return quality_tag
    if overlap < OCR_OVERLAP_TO_LOW:
        return "low"
    if overlap < OCR_OVERLAP_ONE_LEVEL:
        return _ONE_LEVEL_DOWN.get(tag, tag)
    return tag
