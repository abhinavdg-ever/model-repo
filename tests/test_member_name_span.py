"""2026-11-01 (user): the member-name rule returns the name only -- never a word after it
('Female'), never a span across a date or a field label -- and an accepted extraction
name that is the member comes first (core-pipeline/stages/lib/member)."""
from types import SimpleNamespace

from stages.lib.member import engine


def _rule(text, first, last):
    expected = {"DummyFirstName": first, "DummyLastName": last, "DummyMiddleName": ""}
    return engine.extract_full_name_rule(text, expected, "2")


def test_the_name_only_hyphen_kept():
    assert _rule("Parker-Hill Dylan Female Provider: Visit", "Dylan", "Parker-Hill") == "Parker-Hill Dylan"
    assert _rule("Patient: Dylan Parker-Hill Attending", "Dylan", "Parker-Hill") == "Dylan Parker-Hill"


def test_never_across_a_date_or_a_label():
    assert _rule("Parker-Hill 02/26/1970 Female Dylan", "Dylan", "Parker-Hill") == "N/A"
    assert _rule("Nicole Home Name Phone Jackson", "Nicole", "Jackson") == "N/A"
    assert _rule("Name: Jackson Nicole D.O.B: 08/23/1995", "Nicole", "Jackson") == "Jackson Nicole"


def test_accepted_extraction_name_of_the_member_comes_first():
    row = {"key": "Name", "value": "Jackson Nicole", "accepted": True, "selected": True,
           "sentence": "Nicole Home Name Phone Jackson", "ner_text": "Jackson Nicole"}
    staged = SimpleNamespace(
        fields={"name": [row]},
        accepted=lambda field: [row] if field == "name" else [],
    )
    expected = {"DummyFirstName": "Nicole", "DummyLastName": "Jackson", "DummyMiddleName": "",
                "DummyDOB": "", "MemberID": ""}
    fields = {"Detected_Full_Name": "Nicole Home Name Phone Jackson", "Detected_DOB": "", "Detected_MemberID": ""}
    out, _, _ = engine.overlay_staged_fields(fields, None, staged, expected, "2")
    assert out["Detected_Full_Name"] == "Jackson Nicole"
