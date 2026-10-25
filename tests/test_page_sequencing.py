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
    # J1, J2 … in page order, the duplicate included (2026-10-25, user); Main pages are not.
    assert junk_sequences(pages, verdicts) == {11: 1, 13: 2, 14: 3}



# --- the fences (2026-10-25, user) ---------------------------------------------------


def _feature(n):
    from stages.lib.sequencing.feature_extractor import features_from_text

    return features_from_text(page_id=str(n), page_number=n, text=f"page text number {n} " * 5)


def _marker(num, total, pattern="page_x_of_y"):
    from stages.lib.sequencing.engine.types import ExplicitMarker

    return ExplicitMarker(page_num=num, total_pages=total, pattern=pattern, confidence=1.0)


def _order(markers, documents=None, pages=10):
    from stages.lib.sequencing.detect import fenced_order

    features = [_feature(n) for n in range(1, pages + 1)]
    return fenced_order(features, {str(k): v for k, v in markers.items()},
                        {str(n): (documents or {}).get(n, 1) for n in range(1, pages + 1)})


def test_marked_pages_reorder_among_their_family_and_unmarked_pages_stay():
    # Pages 2, 4, 6 say 3, 1, 2 of 3; the rest have no number and keep their places.
    order, held = _order({2: _marker(3, 3), 4: _marker(1, 3), 6: _marker(2, 3)})
    assert order == ["1", "4", "3", "6", "5", "2", "7", "8", "9", "10"] and not held


def test_a_page_never_moves_more_than_five_places():
    # Page 1 says 3 of 3 and page 9 says 1 of 3: the swap would move them 8 places.
    order, held = _order({1: _marker(3, 3), 5: _marker(2, 3), 9: _marker(1, 3)})
    assert order == [str(n) for n in range(1, 11)]
    assert held == {"1", "9"}


def test_only_a_confident_page_n_of_m_moves_a_page():
    # "2 of 2" / "1 of 2" without the word page is not confident enough: no swap.
    weak = {3: _marker(2, 2, "x_of_y"), 4: _marker(1, 2, "x_of_y")}
    order, _ = _order(weak, documents={n: 1 for n in range(1, 11)})
    assert order == [str(n) for n in range(1, 11)]
    # "Page 2 of 2" / "Page 1 of 2" in one document swap.
    order, _ = _order({3: _marker(2, 2), 4: _marker(1, 2)}, documents={n: 1 for n in range(1, 11)})
    assert order[2:4] == ["4", "3"]


def test_nothing_moves_across_documents():
    order, _ = _order({3: _marker(2, 2), 4: _marker(1, 2)}, documents={1: 1, 2: 1, 3: 1, 4: 2, 5: 2})
    assert order[:5] == ["1", "2", "3", "4", "5"]


def test_two_pages_claiming_one_number_hold_the_family():
    order, held = _order({2: _marker(2, 2), 3: _marker(2, 2), 4: _marker(1, 2)}, documents={n: 1 for n in range(1, 11)})
    assert order == [str(n) for n in range(1, 11)] and held
