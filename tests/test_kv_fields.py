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


def _page_dates(body, kv=None):
    hit = detect_dos_per_page(
        f"===== 1.jpg =====\n{body}\n", None, use_llm=False, kv_dates={"1.jpg": kv} if kv else None
    )[0]
    return hit["dos_from_iso"], hit["dos_to_iso"]


def test_a_key_value_range_keeps_its_end_date():
    kv = [{"iso": "2024-03-01", "iso_to": "2024-03-07", "raw": "03/01/2024 - 03/07/2024",
           "tier": "service", "keyword": "Date of Service"}]
    assert _page_dates("Seen today. Assessment stable.", kv) == ("2024-03-01", "2024-03-07")


def test_a_written_dos_range_keeps_both_ends():
    assert _page_dates("Date of Service: 03/01/2024 - 03/05/2024") == ("2024-03-01", "2024-03-05")
    assert _page_dates("DOS: 03/01/2024 to 03/05/2024") == ("2024-03-01", "2024-03-05")
    assert _page_dates("Certification Period: 03/01/2024 through 04/29/2024") == (
        "2024-03-01", "2024-04-29")


def test_an_unlabelled_or_reversed_range_is_not_a_dos_range():
    # A reference period on a lab page is not a date of service.
    assert _page_dates("Reference period 01/01/2024 - 02/01/2024") == ("", "")
    assert _page_dates("Date of Service: 03/05/2024 - 03/01/2024") == ("2024-03-05", "2024-03-05")


def test_admit_and_discharge_still_pair_in_date_order():
    assert _page_dates("Admission Date: 03/01/2024\nDischarge Date: 03/05/2024") == (
        "2024-03-01", "2024-03-05")
