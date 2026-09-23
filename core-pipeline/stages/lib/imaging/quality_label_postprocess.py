"""End-of-stage quality label post-processing (from image_preprocessing).

Does not change ``quality_score`` — only the discrete ``quality_tag``.
"""
from __future__ import annotations


def base_quality_label(score_01: float | None) -> str | None:
    """Map normalized score in [0, 1] to high / medium / low."""
    if score_01 is None:
        return None
    s = float(score_01)
    if s >= 0.70:
        return "high"
    if s >= 0.40:
        return "medium"
    return "low"


def apply_quality_label_postprocess(
    *,
    quality_tag: str | None,
    quality_score: float | None,
    printed_or_handwritten: str | None,
) -> str | None:
    """Assign quality_tag; Handwritten + High is downgraded to Medium.

    Matches the teammate ``image_preprocessing`` rule: a high engineering
    score on a handwritten page is reported as medium so reviewers do not
    read it as a clean printed scan.
    """
    tag = (quality_tag or "").strip().lower() or base_quality_label(quality_score)
    if not tag:
        return None
    hw = (printed_or_handwritten or "").strip().lower()
    if hw in {"handwritten", "hand"} and tag == "high":
        return "medium"
    return tag
