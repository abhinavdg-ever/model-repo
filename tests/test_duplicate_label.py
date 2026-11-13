"""Yes / Partial / No duplicate label mapping and display confidence.

2026-11-11 (user: Duplicate_Logic.md): a duplicate, or a 100% text match a hard block kept from
being one (a normal page). 2026-11-13 (user): they read Yes and Partial; Partial matches Yes."""
from __future__ import annotations

import sys
from pathlib import Path

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
