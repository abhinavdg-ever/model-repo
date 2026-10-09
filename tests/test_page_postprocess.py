"""Post-processing rules applied after the family is chosen."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from stages.lib.page_classify.postprocess import apply  # noqa: E402


def _row(page_id: int, family: str, name: str | None = None) -> dict:
    return {
        "page_id": page_id,
        "page_name": name or f"{page_id}.jpg",
        "family": family,
        "family_display": family,
        "page_type": family,
        "tag": "codeable",
        "is_codeable": "Codeable",
        "confidence": 0.8,
        "family_source": "model",
    }


def test_a_signed_page_becomes_a_progress_note():
    rows = apply(
        [_row(1, "consent_form", "1.jpg"), _row(2, "progress_note", "2.jpg")],
        signed_names={"1.jpg"},
    )
    assert rows[0]["family"] == "progress_note"
    assert rows[0]["page_type"] == "Progress Note"
    assert rows[0]["family_source"] == "signature"
    assert rows[1]["family_source"] == "model"


def test_demographics_between_progress_notes_becomes_one():
    rows = apply(
        [
            _row(1, "progress_note"),
            _row(2, "patient_demographics"),
            _row(3, "patient_demographics"),
            _row(4, "progress_note"),
            _row(5, "patient_demographics"),
        ]
    )
    assert rows[1]["family"] == "progress_note"
    assert rows[1]["family_source"] == "between"
    assert rows[2]["family_source"] == "between"
    assert rows[4]["family"] == "patient_demographics"


def test_a_signed_demographics_page_between_notes_is_a_progress_note():
    rows = apply(
        [
            _row(1, "progress_note", "1.jpg"),
            _row(2, "laboratory_report", "2.jpg"),
            _row(3, "progress_note", "3.jpg"),
        ],
        signed_names={"2.jpg"},
    )
    assert [row["family"] for row in rows] == [
        "progress_note",
        "progress_note",
        "progress_note",
    ]
    assert rows[1]["family_source"] == "signature"
