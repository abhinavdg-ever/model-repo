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


def _empty_fields():
    return {
        "Detected_Full_Name": "N/A", "Detection_Source_Name": "", "ner_key_source_Name": "",
        "Detected_DOB": "N/A", "Detection_Source_DOB": "", "ner_key_source_DOB": "",
        "Detected_MemberID": "N/A", "Detection_Source_MemberID": "", "ner_key_source_MemberID": "",
    }


def test_the_manifest_name_in_any_sentence_beats_the_selected_value():
    """2026-10-27 (user): before the selected value, look for the manifest member in every
    sentence, in either order; a selected drug name must not become the member's name."""
    staged = StagedPage(
        "4.png",
        {
            "fields": {
                "name": [
                    {"key": "Patient Name", "sentence": "Patient Name: Public Jane once a day",
                     "value": "", "accepted": False, "selected": False},
                    {"key": "near:Acct", "sentence": "Trelegy Ellipta inhaler once a day",
                     "value": "Trelegy Ellipta", "accepted": True, "selected": True},
                ],
                "dob": [{"key": "DOB", "sentence": "born 3/15/1980 in town", "value": "", "accepted": False}],
                "member_id": [],
            }
        },
    )
    expected = {"DummyFirstName": "Jane", "DummyMiddleName": "", "DummyLastName": "Public",
                "DummyDOB": "03/15/1980", "MemberID": ""}
    filled, _people, names = overlay_staged_fields(_empty_fields(), None, staged, expected, "2")
    assert filled["Detected_Full_Name"] == "Public Jane"
    assert filled["ner_key_source_Name"] == "sentence: Patient Name"
    assert filled["Detected_DOB"] == "03/15/1980"
    assert names == ["Trelegy Ellipta"]


def test_without_the_member_in_a_sentence_the_accepted_value_is_used():
    staged = StagedPage(
        "1.png",
        {"fields": {"name": [{"key": "Patient", "sentence": "Patient: Mary Major", "value": "Mary Major",
                              "accepted": True, "selected": True}]}},
    )
    expected = {"DummyFirstName": "Jane", "DummyMiddleName": "", "DummyLastName": "Public",
                "DummyDOB": "", "MemberID": ""}
    filled, _people, _names = overlay_staged_fields(_empty_fields(), None, staged, expected, "2")
    assert filled["Detected_Full_Name"] == "Mary Major"


def test_a_page_lists_the_names_that_are_not_the_member():
    from stages.lib.member.engine import verify_record

    expected = {"DummyFirstName": "Jane", "DummyMiddleName": "", "DummyLastName": "Public",
                "DummyDOB": "03/15/1980", "MemberID": ""}
    staged = StagedPage(
        "1.png",
        {"fields": {"name": [
            {"key": "Patient", "sentence": "Patient: Mary Major", "value": "Mary Major", "accepted": True, "selected": True},
            {"key": "Patient", "sentence": "Patient: Public, Jane", "value": "Public, Jane", "accepted": True, "selected": False},
        ]}},
    )
    result = verify_record("c", [{"page_no": 1, "page_name": "1.png", "text": "DOB 03/15/1980", "staged": staged}],
                           expected, "2")
    page = result.pages[0]
    # The member is on the page: no other names (2026-10-27, user).
    assert page.page_verified and page.other_names == []


def test_a_page_without_the_member_lists_its_names():
    from stages.lib.member.engine import verify_record

    expected = {"DummyFirstName": "Jane", "DummyMiddleName": "", "DummyLastName": "Public",
                "DummyDOB": "03/15/1980", "MemberID": ""}
    staged = StagedPage(
        "1.png",
        {"fields": {"name": [
            {"key": "Patient", "sentence": "Patient: Mary Major", "value": "Mary Major", "accepted": True, "selected": True},
        ]}},
    )
    result = verify_record("c", [{"page_no": 1, "page_name": "1.png", "text": "DOB 03/15/1980", "staged": staged}],
                           expected, "2")
    assert result.pages[0].other_names == ["Mary Major"]


def test_a_surname_of_two_words_matches():
    """2026-10-27 (user): a manifest last name "Le Blanc" against "Le Blanc Jonathan" (and
    "LeBlanc, Jonathan"); another patient on the page stays someone else."""
    from stages.lib.member.extractors.rule_based.name_common import name_matches

    staged = StagedPage(
        "1.png",
        {"fields": {"name": [
            {"key": "Patient", "sentence": "Patient Details Patient: Le Blanc Jonathan of Birth: 09/07/1997",
             "value": "", "accepted": False, "selected": False},
            {"key": "Patient", "sentence": "Patient: Doe, Mary", "value": "Doe, Mary", "accepted": True, "selected": True},
        ]}},
    )
    expected = {"DummyFirstName": "Jonathan", "DummyMiddleName": "", "DummyLastName": "Le Blanc",
                "DummyDOB": "09/07/1997", "MemberID": ""}
    filled, _people, _names = overlay_staged_fields(_empty_fields(), None, staged, expected, "2")
    assert filled["Detected_Full_Name"] == "Le Blanc Jonathan"
    assert filled["Detected_DOB"] == "09/07/1997"
    assert name_matches("LeBlanc, Jonathan", "Jonathan", "Le Blanc", "", "2")
    assert not name_matches("Doe, Mary", "Jonathan", "Le Blanc", "", "2")


def test_a_nickname_matches_the_manifest_first_name():
    """2026-10-27 (user): Bill for William, Bob for Robert (member_name_variants_canon.json)."""
    from stages.lib.member.extractors.rule_based.name_common import name_matches

    assert name_matches("Bill Smith", "William", "Smith", "", "2")
    assert name_matches("Smith, Bobby", "Robert", "Smith", "", "2")
    assert not name_matches("Bill Jones", "William", "Smith", "", "2")
    assert not name_matches("Mary Smith", "William", "Smith", "", "2")


def test_phrases_that_are_not_people_are_dropped():
    """2026-10-28 (user): no model -- digits, a word run into a credential, or every word a
    common / clinical word."""
    from stages.lib.member.name_filter import looks_like_person

    for junk in ["Sars Cov 2", "Amy N MDOccurred", "Elderly Woman", "Dear Ms", "Vitamin D Deficiency",
                 "Visual Inspection"]:
        assert not looks_like_person(junk), junk
    for name in ["Alice M Green", "Hoing Amy N", "Dylan Green", "Le Blanc Jonathan", "Thompson, Patricia",
                 "McDonald Ronald", "JOHN SMITH"]:
        assert looks_like_person(name), name


def test_a_junk_phrase_is_not_the_page_name():
    staged = StagedPage(
        "1.png",
        {"fields": {"name": [
            {"key": "Patient", "sentence": "Dear Ms,", "value": "Dear Ms", "accepted": True, "selected": True},
        ]}},
    )
    expected = {"DummyFirstName": "Jane", "DummyMiddleName": "", "DummyLastName": "Public",
                "DummyDOB": "", "MemberID": ""}
    filled, _people, names = overlay_staged_fields(_empty_fields(), None, staged, expected, "2")
    assert filled["Detected_Full_Name"] == "N/A" and names == []
