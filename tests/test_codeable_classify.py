"""Tests for term-frequency codeable / non-codeable classification."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from stages.lib.imaging.codeable_classify import (  # noqa: E402
    classify_pages,
    load_canon,
    score_text,
)


@pytest.fixture(scope="module")
def canon():
    return load_canon()


def test_canon_has_continue_flags(canon):
    assert any(e.carries for e in canon)
    tags = {e.tag for e in canon}
    assert "codeable" in tags
    assert "non_codeable" in tags
    assert "discharge_frequency" in tags


def test_progress_note_scores_codeable(canon):
    hit = score_text("PROGRESS NOTE\nPatient presents with cough.", canon)
    assert hit is not None
    assert hit.tag == "codeable"
    assert hit.continue_ == "y"


def test_consent_form_scores_non_codeable(canon):
    hit = score_text("Consent form — patient authorization signature", canon)
    assert hit is not None
    assert hit.tag == "non_codeable"


def test_discharge_summary_scores_discharge(canon):
    hit = score_text("DISCHARGE SUMMARY\nHospital Course: ...", canon)
    assert hit is not None
    assert hit.tag == "discharge_frequency"
    assert hit.continue_ == "y"


def test_continue_carries_until_dos_changes(canon):
    pages = [
        {
            "page_id": 1,
            "page_name": "1.jpg",
            "page_number": 1,
            "text": "Progress Note visit today",
            "dos_from": "2024-01-10",
            "dos_to": "2024-01-10",
        },
        {
            "page_id": 2,
            "page_name": "2.jpg",
            "page_number": 2,
            "text": "random continuation page with no header keywords",
            "dos_from": "2024-01-10",
            "dos_to": "2024-01-10",
        },
        {
            "page_id": 3,
            "page_name": "3.jpg",
            "page_number": 3,
            "text": "Consent form authorization",
            "dos_from": "2024-02-01",
            "dos_to": "2024-02-01",
        },
    ]
    rows = classify_pages(pages, entries=canon)
    assert rows[0]["tag"] == "codeable"
    assert rows[0]["continue_applied"] == "n"
    assert rows[1]["tag"] == "codeable"
    assert rows[1]["continue_applied"] == "y"
    assert rows[1]["is_codeable"] == "Codeable"
    assert rows[2]["tag"] == "non_codeable"
    assert rows[2]["continue_applied"] == "n"


def test_continue_switches_on_new_continue_type_same_dos(canon):
    pages = [
        {
            "page_id": 1,
            "page_name": "1.jpg",
            "text": "Progress Note",
            "dos_from": "2024-03-01",
            "dos_to": "2024-03-01",
        },
        {
            "page_id": 2,
            "page_name": "2.jpg",
            "text": "Discharge Report final",
            "dos_from": "2024-03-01",
            "dos_to": "2024-03-01",
        },
        {
            "page_id": 3,
            "page_name": "3.jpg",
            "text": "no keywords here",
            "dos_from": "2024-03-01",
            "dos_to": "2024-03-01",
        },
    ]
    rows = classify_pages(pages, entries=canon)
    assert rows[0]["tag"] == "codeable"
    assert rows[1]["tag"] == "discharge_frequency"
    assert rows[1]["continue_applied"] == "n"
    assert rows[2]["tag"] == "discharge_frequency"
    assert rows[2]["continue_applied"] == "y"
