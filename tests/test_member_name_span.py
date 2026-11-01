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


def _staged(**fields):
    def rows(field):
        return fields.get(field, [])

    return SimpleNamespace(
        fields=fields,
        rows=rows,
        accepted=lambda field: [r for r in rows(field) if r.get("accepted")],
    )


EXPECTED = {"DummyFirstName": "Nicole", "DummyLastName": "Jackson", "DummyMiddleName": "",
            "DummyDOB": "", "MemberID": ""}


def _pick(staged, page_find="N/A"):
    fields = {"Detected_Full_Name": page_find, "Detection_Source_Name": "rule based" if page_find != "N/A" else "",
              "Detected_DOB": "", "Detected_MemberID": ""}
    out, _, _ = engine.overlay_staged_fields(fields, None, staged, EXPECTED, "2")
    return out["Detected_Full_Name"]


def test_1_a_name_row_that_is_the_member_as_written():
    row = {"key": "Name", "value": "Jackson Nicole", "ner_text": "Jackson Nicole D.O.B", "accepted": True,
           "selected": True, "sentence": "Nicole Home Name Phone Jackson"}
    assert _pick(_staged(name=[row]), page_find="Nicole Home Jackson") == "Jackson Nicole"


def test_1_an_unaccepted_row_whose_ner_text_contains_the_member():
    row = {"key": "Patient", "value": "", "ner_text": "Jackson Nicole D.O.B", "accepted": False,
           "selected": False, "sentence": "x"}
    assert _pick(_staged(name=[row])) == "Jackson Nicole"


def test_2_else_the_shortest_matching_phrase_in_the_text():
    rows = [
        {"key": "Patient", "value": "", "ner_text": "", "accepted": False, "selected": False,
         "sentence": "Patient: Nicole Ann Marie Jackson seen today"},
        {"key": "Visit", "value": "", "ner_text": "", "accepted": False, "selected": False,
         "sentence": "Visit for Jackson, Nicole on Monday"},
    ]
    assert _pick(_staged(name=rows)) == "Jackson Nicole"


def test_2_only_the_name_rows_sentences():
    other = {"key": "Guarantor", "value": "", "ner_text": "", "accepted": False, "selected": False,
             "sentence": "Guarantor: Nicole Jackson"}
    accepted = {"key": "Name", "value": "Maria Lopez", "ner_text": "Maria Lopez", "accepted": True,
                "selected": True, "sentence": "Name: Maria Lopez"}
    assert _pick(_staged(name=[accepted], guarantor=[other])) == "Maria Lopez"


def test_3_else_the_accepted_name():
    row = {"key": "Name", "value": "Maria Lopez", "ner_text": "Maria Lopez", "accepted": True,
           "selected": True, "sentence": "Name: Maria Lopez"}
    assert _pick(_staged(name=[row])) == "Maria Lopez"
