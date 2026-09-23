"""Quality label post-process: Handwritten + High → Medium."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core-pipeline"))

from stages.lib.imaging.quality_label_postprocess import (  # noqa: E402
    apply_quality_label_postprocess,
    base_quality_label,
)


def test_base_quality_label():
    assert base_quality_label(0.80) == "high"
    assert base_quality_label(0.50) == "medium"
    assert base_quality_label(0.20) == "low"
    assert base_quality_label(None) is None


def test_handwritten_high_becomes_medium():
    assert (
        apply_quality_label_postprocess(
            quality_tag="high",
            quality_score=0.85,
            printed_or_handwritten="handwritten",
        )
        == "medium"
    )


def test_printed_high_stays_high():
    assert (
        apply_quality_label_postprocess(
            quality_tag="high",
            quality_score=0.85,
            printed_or_handwritten="printed",
        )
        == "high"
    )


def test_handwritten_low_unchanged():
    assert (
        apply_quality_label_postprocess(
            quality_tag="low",
            quality_score=0.2,
            printed_or_handwritten="handwritten",
        )
        == "low"
    )
