"""Yes / Partial / No duplicate label mapping and display confidence.

2026-11-11 (user: Duplicate_Logic.md): a duplicate, or a 100% text match a hard block kept from
being one (a normal page). 2026-11-13 (user): they read Yes and Partial; Partial matches Yes."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "review-ui" / "backend"))

from app.services.duplicate_label import format_duplicate_label  # noqa: E402


def test_duplicate_labels():
    assert format_duplicate_label(False, None) == "No"
    # The pipeline's flag decides: every duplicate is ≥ 98%.
    assert format_duplicate_label(True, 1.0) == "Yes"
    assert format_duplicate_label(True, 0.985) == "Yes"
    assert format_duplicate_label(False, 0.96, True) == "No"  # the old similarity Partial is gone
    assert format_duplicate_label(False, 1.0, False, True) == "Partial"
    assert format_duplicate_label(None, 1.0) == "NA"


def test_duplicate_confidence_curves_2026_11_14():
    """CONFIDENCE_LOGIC.md 12.2.1: Yes / Partial = max(0.30, 1 - 10 x (1 - s)); No: 1.0 under
    80%, 1.0 -> 0.80 to 90%, 0.80 -> 0.30 to 100%. The same numbers as the pipeline's canon."""
    from app.services.duplicate_label import (
        duplicate_match_confidence,
        duplicate_no_confidence,
        similarity_confidence,
    )
    from modules import confidence as conf

    for s, want in ((1.0, 1.0), (0.99, 0.9), (0.98, 0.8), (0.97, 0.7), (0.95, 0.5), (0.93, 0.3), (0.90, 0.3)):
        assert abs(duplicate_match_confidence(s) - want) < 1e-9
        assert conf.duplicate_match(s) == pytest.approx(want)
    for s, want in ((0.40, 1.0), (0.79, 1.0), (0.80, 1.0), (0.85, 0.9), (0.90, 0.8), (0.92, 0.7),
                    (0.95, 0.55), (0.98, 0.4), (1.0, 0.3)):
        assert abs(duplicate_no_confidence(s) - want) < 1e-9
        assert conf.duplicate_no(s) == pytest.approx(want)
    # Nothing compared: No is sure.
    assert duplicate_no_confidence(None) == 1.0 and conf.duplicate_no(None) == 1.0
    # The call a similarity makes: Yes at or above 95%, No below.
    assert similarity_confidence(0.99) == pytest.approx(0.9)
    assert similarity_confidence(0.94) == pytest.approx(0.6)
    assert similarity_confidence(0.80) == 1.0


def test_a_duplicate_shows_its_originals_blank_junk_tag():
    from app.core.schemas import ImagingPageResult
    from app.services.imaging_overlays import inherit_duplicate_blank_junk

    pages = [
        ImagingPageResult(pageNumber=1, fileName="1.jpg", blankOrJunk="Yes (Junk)", isDuplicate=False),
        ImagingPageResult(pageNumber=2, fileName="2.jpg", blankOrJunk="Yes (Junk)", isDuplicate=True, duplicateOf="1.jpg"),
        ImagingPageResult(pageNumber=3, fileName="3.jpg", blankOrJunk="Yes (Junk)", isDuplicate=True, duplicateOf="9.jpg"),
        ImagingPageResult(pageNumber=4, fileName="4.jpg", blankOrJunk="No", isDuplicate=False),
        # A duplicate of a Main page keeps its own Yes (Junk).
        ImagingPageResult(pageNumber=5, fileName="5.jpg", blankOrJunk="Yes (Junk)", isDuplicate=True, duplicateOf="4.jpg"),
    ]
    out = inherit_duplicate_blank_junk(pages)
    assert [page.blankOrJunk for page in out] == [
        "Yes (Junk)", "Yes (Junk)", "Yes (Junk)", "No", "Yes (Junk)",
    ]
    assert out[1].isDuplicate is True




def test_a_copy_is_displayed_as_partial():
    """2026-11-13 (user): a blocked 100% match reads "Partial (to page N)", a Yes for comparisons."""
    import sys
    sys.path.insert(0, "review-ui/backend")
    from app.services.duplicate_label import format_duplicate_label
    from app.services.excel_compare.normalizer import normalize_labelled_yes_no

    assert format_duplicate_label(False, None, is_content=True) == "Partial"
    assert normalize_labelled_yes_no("Partial (to page 4)") == "yes"
    assert normalize_labelled_yes_no("Yes (to page 4)") == "yes"
    assert normalize_labelled_yes_no("Copy of 4") == "yes"  # the label before 2026-11-13
    assert normalize_labelled_yes_no("Duplicate of 4") == "yes"
    assert normalize_labelled_yes_no("Repeated Content (To Page 4)") == "no"  # older exports
