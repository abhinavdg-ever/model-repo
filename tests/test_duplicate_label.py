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


def test_duplicate_display_confidence():
    assert duplicate_display_confidence(False, None) == 1.0
    assert duplicate_display_confidence(True, 1.0) == 1.0
    assert duplicate_display_confidence(True, 0.985) == 1.0
    # Partial: 1 + (sim - 1) x 10.
    assert abs(duplicate_display_confidence(False, 0.97, True) - 0.70) < 1e-9
    assert abs(duplicate_display_confidence(False, 0.95, True) - 0.50) < 1e-9
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
