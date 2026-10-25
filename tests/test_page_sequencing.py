"""Tests for ported page-sequencing engine (markers / order)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from stages.lib.sequencing import compute_sequence_assignments  # noqa: E402


def test_explicit_page_markers_order_pages():
    filler = (
        "Patient presents for follow-up evaluation of chronic conditions with "
        "review of medications labs and plan of care documented in detail."
    )
    pages = [
        {
            "page_id": 10,
            "page_number": 1,
            "text": f"{filler}\nPage 2 of 3\n{filler}",
            "is_classified": False,
        },
        {
            "page_id": 11,
            "page_number": 2,
            "text": f"{filler}\nPage 1 of 3\n{filler}",
            "is_classified": False,
        },
        {
            "page_id": 12,
            "page_number": 3,
            "text": f"{filler}\nPage 3 of 3\n{filler}",
            "is_classified": False,
        },
    ]
    assignments = compute_sequence_assignments(pages, cross_encoder_enabled=False)
    by_id = {a["page_id"]: a for a in assignments}
    ordered = sorted(assignments, key=lambda a: a["sequence_position"])
    assert [a["page_id"] for a in ordered] == ["11", "10", "12"]
    assert by_id["11"]["sequence_method"] in {
        "explicit_marker",
        "explicit_marker_reference",
    }


def test_junk_pages_are_classified_last():
    pages = [
        {
            "page_id": 1,
            "page_number": 1,
            "text": "Main clinical content with enough words to not be blank "
            "and keep going so word count stays high.",
            "is_classified": False,
        },
        {
            "page_id": 2,
            "page_number": 2,
            "text": "fax cover",
            "is_classified": True,
        },
    ]
    assignments = compute_sequence_assignments(pages, cross_encoder_enabled=False)
    by_id = {a["page_id"]: a for a in assignments}
    assert by_id["1"]["sequence_position"] == 1
    assert by_id["2"]["sequence_position"] == 2
    assert by_id["2"]["sequence_method"] == "classified_page"


def test_original_order_fallback_when_no_markers():
    pages = [
        {
            "page_id": "a",
            "page_number": 1,
            "text": "First page with substantial clinical narrative content here.",
        },
        {
            "page_id": "b",
            "page_number": 2,
            "text": "Second page with substantial clinical narrative content here.",
        },
    ]
    assignments = compute_sequence_assignments(pages, cross_encoder_enabled=False)
    ordered = sorted(assignments, key=lambda a: a["sequence_position"])
    assert [a["page_id"] for a in ordered] == ["a", "b"]


def test_blank_and_junk_pages_have_their_own_series():
    from stages.lib.sequencing.stage import junk_sequences

    pages = [{"id": i, "page_number": n} for i, n in ((10, 4), (11, 1), (12, 2), (13, 3), (14, 5))]
    verdicts = {11: "junk", 12: "not_blank_junk", 13: "duplicate", 14: "blank", 10: "not_blank_junk"}
    # J1, J2 … in page order; Main pages and the duplicate are not numbered here.
    assert junk_sequences(pages, verdicts) == {11: 1, 14: 2}
