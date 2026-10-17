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


def test_the_key_value_date_comes_before_an_edge_label():
    text = "===== 1.jpg =====\nDate of Service: 01/02/2024\n"
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
                    "keyword": "Visit Date",
                }
            ]
        },
    )
    assert hits[0]["dos_from_iso"] == "2024-03-15"
    assert hits[0]["page_source"] == "kv"


def test_an_edge_label_is_used_when_there_is_no_key_value_date():
    hits = detect_dos_per_page(
        "===== 1.jpg =====\nDate of Service: 01/02/2024\n", None, use_llm=False, kv_dates={}
    )
    assert hits[0]["dos_from_iso"] == "2024-01-02"
    assert hits[0]["page_source"] == "edge"


def test_a_key_value_date_beats_a_date_in_the_middle_of_the_page():
    filler = " ".join(f"word{i}" for i in range(40))
    text = f"===== 1.jpg =====\n{filler}\nDate of Service: 03/15/2024\n{filler}\n"
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


def test_an_unlabelled_text_date_is_not_the_page_date():
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
    return detect_dos_per_page(f"===== 4.png =====\n{text}\n", None, use_llm=False, **kw)[0]


FALLBACK = {"4.png": [{"iso": "2025-05-20", "raw": "5/20/25", "tier": "weak", "keyword": "Date",
                       "confidence": 0.62}]}


def test_a_date_only_the_rules_accepted_is_the_last_resort_capped_as_a_backup():
    hit = _dos("CT & MRI scheduling form, nothing dated at the edges.", kv_fallback=FALLBACK)
    assert hit["dos_from_iso"] == "2025-05-20"
    assert (hit["page_source"], hit["match_type"]) == ("kv_low", "kv_low")
    # Always the canon's kv_low (0.3), whatever the model scored.
    assert hit["confidence"] == 0.3


def test_the_fallback_without_a_model_score_uses_the_canon_kv_low():
    fallback = {"4.png": [{**FALLBACK["4.png"][0], "confidence": None}]}
    assert _dos("No edge date here.", kv_fallback=fallback)["confidence"] == 0.3


def test_a_selected_key_value_date_beats_the_fallback():
    kv = {"4.png": [{"iso": "2024-03-15", "raw": "03/15/2024", "tier": "service", "keyword": "DOS"}]}
    hit = _dos("No edge date here.", kv_dates=kv, kv_fallback=FALLBACK)
    assert (hit["dos_from_iso"], hit["page_source"]) == ("2024-03-15", "kv")


def test_the_dos_stage_writes_the_fallback_as_method_kv():
    from stages.lib.dos.stage import _page_method

    assert _page_method({"page_source": "kv_low"}, "rules") == "kv"


def test_rule_fallback_only_when_the_model_chose_nothing():
    rows = [
        {"key": "Received", "value": "", "score": 0.0, "accepted": False, "selected": False},
        {"key": "Date", "value": "5/20/25", "dos_from": "5/20/25", "score": 0.8,
         "accepted": False, "selected": False, "rule_accepted": True, "model_score": 0.41},
        {"key": "Date", "value": "5/8/23", "score": 0.7, "accepted": False, "selected": False,
         "rule_accepted": True, "model_score": 0.55},
    ]
    page = StagedPage("4.png", {"fields": {"dos": rows}})
    assert [row["value"] for row in page.rule_fallback("dos")] == ["5/8/23", "5/20/25"]
    chosen = {**rows[1], "accepted": True, "selected": True}
    assert StagedPage("4.png", {"fields": {"dos": [chosen, rows[2]]}}).rule_fallback("dos") == []


def test_rule_fallback_reads_staging_written_before_rule_accepted():
    """The pasted 67992303_56072120 p4 row: the rules scored 5/20/25 (0.8), the model said no."""
    rows = [
        {"key": "Date", "value": "", "score": 0.0, "accepted": False, "source": ""},
        {"key": "Date", "value": "5/20/25", "dos_from": "5/20/25", "dos_to": "5/20/25",
         "score": 0.8, "accepted": False, "selected": False, "source": "same_line"},
        {"key": "Visit", "value": "5/20/25", "score": 0.8, "accepted": False, "source": "same_line_shared"},
    ]
    page = StagedPage("4.png", {"fields": {"dos": rows}})
    assert [row["source"] for row in page.rule_fallback("dos")] == ["same_line"]
