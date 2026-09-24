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
    load_canon.cache_clear()
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


def test_unmatched_page_is_not_sure(canon):
    pages = [
        {
            "page_id": 1,
            "page_name": "1.jpg",
            "page_number": 5,
            "text": "zzzz unrelated scribbles with no clinical headers",
            "dos_from": "",
            "dos_to": "",
        }
    ]
    rows = classify_pages(pages, entries=canon)
    assert rows[0]["page_type"] == "Not Available"
    assert rows[0]["tag"] == "not_sure"
    assert rows[0]["is_codeable"] == "Not Sure"


def test_early_pages_with_patient_data_are_demographics(canon):
    text = (
        "Patient Name: Jane Doe\nDate of Birth: 01/01/1980\n"
        "Address: 1 Main St\nMember ID: 12345\nInsurance: Aetna"
    )
    pages = [
        {
            "page_id": 1,
            "page_name": "1.jpg",
            "page_number": 1,
            "text": text,
            "dos_from": "",
            "dos_to": "",
        },
        {
            "page_id": 2,
            "page_name": "2.jpg",
            "page_number": 2,
            "text": text,
            "dos_from": "",
            "dos_to": "",
        },
    ]
    rows = classify_pages(pages, entries=canon)
    assert rows[0]["page_type"] == "Demographics"
    assert rows[0]["is_codeable"] == "Codeable"
    assert rows[1]["page_type"] == "Demographics"


def test_demographics_canon_entry_exists(canon):
    types = {e.page_type for e in canon}
    assert "Demographics" in types
    assert not any("\\" in e.page_type for e in canon)


def test_page_type_labels_hide_parentheticals(canon):
    assert not any("(" in e.page_type or ")" in e.page_type for e in canon)
    soap = next(e for e in canon if e.page_type.casefold().startswith("soap note"))
    assert soap.page_type == "SOAP Note"


def test_consultations_and_consulta_are_one_type(canon):
    types = {e.page_type for e in canon}
    assert "Consultations / Consulta" in types
    assert "Consultations" not in types
    assert "Consulta (Spanish)" not in types
    assert "Consulta" not in types
    hit = score_text("CONSULTA medica del paciente", canon)
    assert hit is not None
    assert hit.page_type == "Consultations / Consulta"
    hit2 = score_text("Consultations follow-up note", canon)
    assert hit2 is not None
    assert hit2.page_type == "Consultations / Consulta"


def test_strong_near_duplicates_merged(canon):
    types = {e.page_type for e in canon}
    assert "Progress Note" in types
    assert "Progress notes" not in types
    assert not any(t == "Progress notes" for t in types)

    assert "Initial Psychiatric Evaluation" in types
    assert not any(t.casefold().startswith("intial") for t in types)

    assert "Telephone Messages" in types
    assert "Telephone or Call - Messages" not in types

    assert "Any Type of Notice" in types
    assert "Any Type of Notification" not in types

    assert "PAP / HPV Report" in types
    assert "PAP Report/Pap Smear" not in types
    assert "PAP/HPV Report" not in types

    assert "Annual Wellness Visit" in types
    assert "Annual Wellnes Visit" not in types
    assert "Madicare Annual Wellness" not in types

    assert "Emergency / ED Visit" in types
    assert "Emegency visit records" not in types


def test_spelling_fixes_in_canon(canon):
    types = {e.page_type for e in canon}
    assert "Lab Requisition" in types
    assert "Principal Diagnosis" in types
    assert "Rehabilitation Daily Treatment Note" in types
    assert "Physical Therapy Assessment/Note" in types
    assert "Echocardiogram / Transthoracic Echocardiography" in types
    # Typo aliases still match
    hit = score_text("Lab requisation form attached", canon)
    assert hit is not None
    assert hit.page_type == "Lab Requisition"
    hit2 = score_text("intial psychiatric evaluation note", canon)
    assert hit2 is not None
    assert hit2.page_type == "Initial Psychiatric Evaluation"


def test_progress_note_beats_other_matches(canon):
    """When several types hit, Progress Note / Office Visit win."""
    text = (
        "PROGRESS NOTE\nOffice visit today.\nConsent form authorization.\n"
        "Patient Education handout attached."
    )
    hit = score_text(text, canon)
    assert hit is not None
    assert "progress note" in hit.page_type.casefold()


def test_office_visit_beats_weaker_matches(canon):
    text = (
        "Office visit follow-up.\nPrescriptions/RX Page refill list.\n"
        "Check List of vitals."
    )
    hit = score_text(text, canon)
    assert hit is not None
    n = hit.page_type.casefold()
    assert "office visit" in n or n == "visit report"
