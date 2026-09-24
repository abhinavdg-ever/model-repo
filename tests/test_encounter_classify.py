"""Tests for DOS-wide encounter-type classification."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from stages.lib.imaging.encounter_classify import (  # noqa: E402
    classify_pages,
    load_canon,
    pick_encounter,
    score_text,
)


@pytest.fixture(scope="module")
def canon():
    return load_canon()


def test_canon_has_four_types(canon):
    types = {e.encounter_type for e in canon}
    assert types >= {"outpatient_f2f", "outpatient_tele", "inpatient", "home"}


def test_soap_is_outpatient_f2f(canon):
    scores = score_text("SOAP Note\nSubjective: … Assessment and Plan", canon)
    hit = pick_encounter(scores)
    assert hit is not None
    assert hit.encounter_type == "outpatient_f2f"


def test_telehealth_is_outpatient_tele(canon):
    scores = score_text("Telehealth visit via video", canon)
    hit = pick_encounter(scores)
    assert hit is not None
    assert hit.encounter_type == "outpatient_tele"


def test_discharge_summary_is_inpatient(canon):
    scores = score_text("DISCHARGE SUMMARY\nHospital Course", canon)
    hit = pick_encounter(scores)
    assert hit is not None
    assert hit.encounter_type == "inpatient"


def test_clinic_progress_note_is_outpatient_f2f(canon):
    """Office Progress Notes must not classify as inpatient."""
    text = (
        "Progress Notes: Miriam P. Zidehsarai, D.O.\n"
        "Appointment Facility: AKI 19 Ravenna\n"
        "Reason for Appointment: 6 month follow up\n"
        "History of Present Illness: Patient here for hospital follow up. "
        "Was admitted to hospital with shortness of breath.\n"
        "Examination: alert and oriented\n"
    )
    scores = score_text(text, canon)
    hit = pick_encounter(scores)
    assert hit is not None
    assert hit.encounter_type == "outpatient_f2f"


def test_bare_progress_notes_title_is_outpatient_f2f(canon):
    scores = score_text("Progress Notes\nCurrent Medications: lisinopril", canon)
    hit = pick_encounter(scores)
    assert hit is not None
    assert hit.encounter_type == "outpatient_f2f"


def test_home_visit_is_home(canon):
    scores = score_text("Skilled Nursing Visit Note", canon)
    hit = pick_encounter(scores)
    assert hit is not None
    assert hit.encounter_type == "home"


def test_diagnostic_imaging_alone_does_not_establish(canon):
    scores = score_text("Diagnostic imaging / Radiology report only", canon)
    assert pick_encounter(scores) is None


def test_entire_dos_gets_same_type(canon):
    pages = [
        {
            "page_id": 1,
            "page_name": "1.jpg",
            "text": "Office visit SOAP Note",
            "dos_from": "2024-05-01",
            "dos_to": "2024-05-01",
        },
        {
            "page_id": 2,
            "page_name": "2.jpg",
            "text": "continuation page with no keywords",
            "dos_from": "2024-05-01",
            "dos_to": "2024-05-01",
        },
        {
            "page_id": 3,
            "page_name": "3.jpg",
            "text": "Discharge Summary",
            "dos_from": "2024-06-01",
            "dos_to": "2024-06-01",
        },
    ]
    rows = classify_pages(pages, entries=canon)
    assert rows[0]["encounter_type"] == "outpatient_f2f"
    assert rows[1]["encounter_type"] == "outpatient_f2f"
    assert rows[1]["encounter_label"] == "Outpatient (F2F)"
    assert rows[2]["encounter_type"] == "inpatient"
