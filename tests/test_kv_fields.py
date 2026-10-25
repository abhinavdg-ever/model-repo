"""Key/value values join the existing member and DOS logic."""
from __future__ import annotations

import stages.lib.dos.stage  # noqa: F401  — puts dos/ on sys.path for dos_logic
from dos_logic import detect_dos_per_page
from stages.lib.extraction.staging import StagedPage
from stages.lib.member.engine import overlay_staged_fields


def test_an_extracted_date_is_the_page_date_when_the_edges_have_none():
    text = "===== 1.jpg =====\nOffice visit. No calendar day is printed on this page.\n"
    hits = detect_dos_per_page(
        text,
        kv_dates={
            "1.jpg": [
                {
                    "iso": "2024-03-15",
                    "raw": "03/15/2024",
                    "tier": "service",
                    "keyword": "Date of Service",
                }
            ]
        },
    )
    assert hits[0]["dos_from_iso"] == "2024-03-15"
    assert hits[0]["page_source"] == "kv"


def test_the_key_value_date_comes_before_an_edge_label():
    text = "===== 1.jpg =====\nDate of Service: 01/02/2024\n"
    hits = detect_dos_per_page(
        text,
        kv_dates={
            "1.jpg": [
                {
                    "iso": "2024-03-15",
                    "raw": "03/15/2024",
                    "tier": "service",
                    "keyword": "Visit Date",
                }
            ]
        },
    )
    assert hits[0]["dos_from_iso"] == "2024-03-15"
    assert hits[0]["page_source"] == "kv"


def test_an_edge_label_is_used_when_there_is_no_key_value_date():
    hits = detect_dos_per_page(
        "===== 1.jpg =====\nDate of Service: 01/02/2024\n", kv_dates={}
    )
    assert hits[0]["dos_from_iso"] == "2024-01-02"
    assert hits[0]["page_source"] == "edge"


def test_a_key_value_date_beats_a_date_in_the_middle_of_the_page():
    filler = " ".join(f"word{i}" for i in range(40))
    text = f"===== 1.jpg =====\n{filler}\nDate of Service: 03/15/2024\n{filler}\n"
    hits = detect_dos_per_page(
        text,
        kv_dates={
            "1.jpg": [
                {
                    "iso": "2024-06-01",
                    "raw": "06/01/2024",
                    "tier": "service",
                    "keyword": "Visit Date",
                }
            ]
        },
    )
    assert hits[0]["dos_from_iso"] == "2024-06-01"
    assert hits[0]["page_source"] == "kv"


def test_an_unlabelled_text_date_is_not_the_page_date():
    text = "===== 1.jpg =====\n03/14/2024\nChief Complaint: cough\n"
    hits = detect_dos_per_page(text)
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
        f"===== 1.jpg =====\n{body}\n", kv_dates={"1.jpg": kv} if kv else None
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
    assert _page_dates("Admission Date: 03/01/2024  Discharge Date: 03/05/2024") == (
        "2024-03-01", "2024-03-05")


def test_an_unlabelled_or_reversed_range_is_not_a_dos_range():
    # A reference period on a lab page is not a date of service.
    assert _page_dates("Reference period 01/01/2024 - 02/01/2024") == ("", "")
    assert _page_dates("Date of Service: 03/05/2024 - 03/01/2024") == ("2024-03-05", "2024-03-05")


def test_admit_and_discharge_still_pair_in_date_order():
    assert _page_dates("Admission Date: 03/01/2024\nDischarge Date: 03/05/2024") == (
        "2024-03-01", "2024-03-05")


def _dos(text, **kw):
    return detect_dos_per_page(f"===== 4.png =====\n{text}\n", **kw)[0]




def test_a_key_value_date_carries_the_models_score_as_its_confidence():
    kv = {"4.png": [{"iso": "2024-03-15", "raw": "03/15/2024", "tier": "service", "keyword": "DOS",
                     "confidence": 0.42}]}
    hit = _dos("Date of Service: 01/02/2024", kv_dates=kv)
    assert (hit["dos_from_iso"], hit["page_source"], hit["confidence"]) == ("2024-03-15", "kv", 0.42)


def test_the_edge_label_is_used_only_without_a_key_value_date():
    hit = _dos("Date of Service: 01/02/2024")
    assert (hit["dos_from_iso"], hit["page_source"], hit["confidence"]) == ("2024-01-02", "edge", 0.68)  # 0.8 x no_model
    assert _dos("No date anywhere.")["page_source"] == ""


def test_the_dos_stage_writes_kv_or_rules():
    from stages.lib.dos.stage import _page_method

    assert [_page_method({"page_source": s}) for s in ("kv", "edge", "edge_tesseract", "")] == [
        "kv", "rules", "rules", "rules"]


def test_the_best_date_is_the_highest_model_score_at_any_score():
    rows = [
        {"key": "Received", "value": "", "score": 0.0, "accepted": False, "selected": False},
        {"key": "Date", "value": "5/20/25", "dos_from": "5/20/25", "score": 0.8,
         "accepted": False, "selected": False, "rule_accepted": True, "model_score": 0.41},
        {"key": "Date", "value": "5/8/23", "score": 0.7, "accepted": False, "selected": False,
         "rule_accepted": True, "model_score": 0.15},
        {"key": "Visit", "value": "4/1/23", "score": 0.0, "accepted": False, "selected": False,
         "rule_accepted": False, "model_score": 0.9},
    ]
    page = StagedPage("4.png", {"fields": {"dos": rows}})
    # Highest score among what the rules accepted, however low; never a rule-rejected row.
    assert page.best("dos")["value"] == "5/20/25"
    assert StagedPage("4.png", {"fields": {"dos": rows[:1]}}).best("dos") is None


def test_the_best_date_without_a_model_is_the_rules_pick():
    """Staging from before the model ran: no model scores, no rule_accepted."""
    rows = [
        {"key": "Date", "value": "", "score": 0.0, "accepted": False, "source": ""},
        {"key": "Date", "value": "5/20/25", "dos_from": "5/20/25", "dos_to": "5/20/25",
         "score": 0.8, "accepted": True, "selected": True, "source": "same_line"},
        {"key": "Visit", "value": "5/20/25", "score": 0.8, "accepted": False, "source": "same_line_shared"},
    ]
    assert StagedPage("4.png", {"fields": {"dos": rows}}).best("dos")["source"] == "same_line"
