"""Key/value values join the existing member and DOS logic."""
from __future__ import annotations

import stages.lib.dos.stage  # noqa: F401  — puts dos/ on sys.path for dos_logic
from dos_logic import detect_dos_per_page
from stages.lib.extraction.staging import StagedPage
from stages.lib.member.engine import overlay_staged_fields


def test_an_extracted_date_joins_the_dos_scorer():
    text = "===== 1.jpg =====\nOffice visit. No calendar day is printed on this page.\n"
    hits = detect_dos_per_page(
        text,
        None,
        use_llm=False,
        kv_dates={
            "1.jpg": [
                {
                    "iso": "2024-03-15",
                    "raw": "03/15/2024",
                    "tier": "encounter",
                    "keyword": "Date of Service",
                }
            ]
        },
    )
    assert hits[0]["dos_from_iso"] == "2024-03-15"
    assert hits[0]["page_source"] == "kv"


def test_a_date_already_in_the_text_is_not_replaced():
    text = "===== 1.jpg =====\nDate of Service: 01/02/2024\n"
    hits = detect_dos_per_page(
        text,
        None,
        use_llm=False,
        kv_dates={
            "1.jpg": [
                {
                    "iso": "2024-01-02",
                    "raw": "01/02/2024",
                    "tier": "encounter",
                    "keyword": "Date of Service",
                }
            ]
        },
    )
    assert hits[0]["dos_from_iso"] == "2024-01-02"
    assert hits[0]["page_source"] == "kv"


def test_a_key_value_date_beats_a_stronger_text_date():
    text = "===== 1.jpg =====\nDate of Service: 03/15/2024\n"
    hits = detect_dos_per_page(
        text,
        None,
        use_llm=False,
        kv_dates={
            "1.jpg": [
                {
                    "iso": "2024-06-01",
                    "raw": "06/01/2024",
                    "tier": "encounter",
                    "keyword": "Visit Date",
                }
            ]
        },
    )
    assert hits[0]["dos_from_iso"] == "2024-06-01"
    assert hits[0]["page_source"] == "kv"


def test_a_text_date_below_0_75_is_not_the_page_date():
    text = "===== 1.jpg =====\n03/14/2024\nChief Complaint: cough\n"
    hits = detect_dos_per_page(text, None, use_llm=False)
    assert hits[0]["dos_from_iso"] == ""
    assert hits[0]["page_source"] == ""


def test_staged_member_fields_fill_what_the_rules_missed():
    staged = StagedPage(
        "1.jpg",
        {
            "fields": {
                "name": [{"value": "Jane Q Public", "accepted": True, "selected": True, "key": "Patient"}],
                "dob": [{"value": "03/15/1980", "accepted": True, "selected": True, "key": "DOB"}],
                "member_id": [{"value": "M123", "accepted": True, "selected": True, "key": "Member ID"}],
            }
        },
    )
    fields = {
        "Detected_Full_Name": "N/A",
        "Detection_Source_Name": "",
        "ner_key_source_Name": "",
        "Detected_DOB": "N/A",
        "Detection_Source_DOB": "",
        "ner_key_source_DOB": "",
        "Detected_MemberID": "N/A",
        "Detection_Source_MemberID": "",
        "ner_key_source_MemberID": "",
    }
    expected = {
        "DummyFirstName": "Jane",
        "DummyMiddleName": "Q",
        "DummyLastName": "Public",
        "DummyDOB": "03/15/1980",
        "MemberID": "M123",
    }
    filled, _people, names = overlay_staged_fields(fields, None, staged, expected, "3")
    assert filled["Detected_Full_Name"] == "Jane Q Public"
    assert filled["Detected_DOB"] == "03/15/1980"
    assert filled["Detected_MemberID"] == "M123"
    assert names == ["Jane Q Public"]
