"""Duplicate Yes / Partial / No label mapping and display confidence.

2026-10-22 (user): ≥ 98% (or contained) is a duplicate, Yes; 95-98% a partial
duplicate, Partial, which stays a Main page."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "review-ui" / "backend"))

from app.services.duplicate_label import (  # noqa: E402
    duplicate_display_confidence,
    format_duplicate_label,
)


def test_duplicate_labels():
    assert format_duplicate_label(False, None) == "No"
    # The pipeline's flag decides: every duplicate is ≥ 98%.
    assert format_duplicate_label(True, 1.0) == "Yes"
    assert format_duplicate_label(True, 0.985) == "Yes"
    assert format_duplicate_label(False, 0.96, True) == "Partial"
    assert format_duplicate_label(None, 1.0) == "NA"


def test_similarity_confidence_curve():
    from app.services.duplicate_label import similarity_confidence

    # Duplicate side: 100% at 100% alike, falling exponentially to 50% at 95%.
    assert similarity_confidence(1.0) == 1.0
    assert abs(similarity_confidence(0.99) - 0.8706) < 1e-4
    assert abs(similarity_confidence(0.98) - 0.7579) < 1e-4
    assert abs(similarity_confidence(0.95) - 0.5) < 1e-9
    # Not-a-duplicate side: rising from 50% at 95% to 100% at 80%, and 100% below.
    assert abs(similarity_confidence(0.90) - 0.6300) < 1e-4
    assert similarity_confidence(0.80) == 1.0
    assert similarity_confidence(0.40) == 1.0
    values = [similarity_confidence(s / 100) for s in range(95, 101)]
    assert values == sorted(values)  # rises with similarity above the cut
    below = [similarity_confidence(s / 100) for s in range(80, 95)]
    assert below == sorted(below, reverse=True)  # rises as similarity falls below it


def test_duplicate_display_confidence():
    # The page's best similarity decides, whatever its verdict.
    assert abs(duplicate_display_confidence(True, 0.985, best_similarity=0.985) - 0.8123) < 1e-4
    assert abs(duplicate_display_confidence(False, 0.7, True, 0.96) - 0.5743) < 1e-4
    assert abs(duplicate_display_confidence(False, 0.7, False, 0.90) - 0.6300) < 1e-4
    assert duplicate_display_confidence(False, 0.7, False, 0.50) == 1.0
    # Before the score was stored: a duplicate / partial row's own similarity stands in;
    # a page never scored reads 1.0.
    assert duplicate_display_confidence(True, 1.0) == 1.0
    assert abs(duplicate_display_confidence(False, 0.97, True) - 0.6598) < 1e-4
    assert duplicate_display_confidence(False, None) == 1.0
    assert duplicate_display_confidence(None, 1.0) is None


def test_a_duplicate_shows_its_originals_blank_junk_tag():
    from app.core.schemas import ImagingPageResult
    from app.services.imaging_overlays import inherit_duplicate_blank_junk

    pages = [
        ImagingPageResult(pageNumber=1, fileName="1.jpg", blankOrJunk="Yes (Junk)", isDuplicate=False),
        ImagingPageResult(pageNumber=2, fileName="2.jpg", blankOrJunk="Yes (Duplicate)", isDuplicate=True, duplicateOf="1.jpg"),
        ImagingPageResult(pageNumber=3, fileName="3.jpg", blankOrJunk="Yes (Duplicate)", isDuplicate=True, duplicateOf="9.jpg"),
        ImagingPageResult(pageNumber=4, fileName="4.jpg", blankOrJunk="No", isDuplicate=False),
        # A duplicate of a Main page keeps its own Yes (Duplicate).
        ImagingPageResult(pageNumber=5, fileName="5.jpg", blankOrJunk="Yes (Duplicate)", isDuplicate=True, duplicateOf="4.jpg"),
    ]
    out = inherit_duplicate_blank_junk(pages)
    assert [page.blankOrJunk for page in out] == [
        "Yes (Junk)", "Yes (Junk)", "Yes (Duplicate)", "No", "Yes (Duplicate)",
    ]
    assert out[1].isDuplicate is True
