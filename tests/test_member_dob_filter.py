"""DOB post-processing across a chart's pages (2026-11-19, user): a DOB after 2000 more than
10 years from the manifest's is dropped; otherwise one after 2000 that is not the chart's
majority DOB is dropped. The member's own DOB and dates up to 2000 are never dropped."""
from modules.member.dob_filter import DROPPED_FAR, DROPPED_MINORITY, drop_reasons

MANIFEST = "03/14/1950"


def test_a_recent_date_far_from_the_manifest_is_dropped():
    assert drop_reasons(["04/27/2026", "03/14/1950", "N/A"], MANIFEST) == [DROPPED_FAR, None, None]


def test_dates_up_to_2000_and_the_members_own_dob_stay():
    assert drop_reasons(["01/01/2000", "07/04/1980", "1950-03-14"], MANIFEST) == [None, None, None]
    # a member born after 2000: their own DOB is never dropped
    assert drop_reasons(["05/06/2005"], "05/06/2005") == [None]


def test_within_10_years_a_date_off_the_majority_is_dropped():
    manifest = "05/06/2005"
    dobs = ["06/07/2008", "06/07/2008", "02/03/2010", "N/A"]
    assert drop_reasons(dobs, manifest) == [None, None, DROPPED_MINORITY, None]


def test_no_manifest_dob_uses_the_majority_only():
    dobs = ["2008-06-07", "06/07/2008", "02/03/2010", "07/04/1980"]
    assert drop_reasons(dobs, "") == [None, None, DROPPED_MINORITY, None]


def test_no_clear_majority_drops_nothing_by_rule_2():
    assert drop_reasons(["06/07/2008", "02/03/2010"], "") == [None, None]
    assert drop_reasons(["06/07/2008"], "") == [None]


def test_verify_record_drops_before_verifying(monkeypatch):
    """The member's name plus a 2026 date read as the DOB no longer verifies the page."""
    from types import SimpleNamespace

    from modules.member import detect_name_mode, engine, verify_record

    monkeypatch.setattr(engine, "name_candidates", lambda text, model_id=None: [])

    exp = {"DummyFirstName": "Justin", "DummyLastName": "Anderson", "DummyMiddleName": "",
           "DummyDOB": "08/29/1954", "MemberID": ""}
    row = {"value": "04/27/2026", "accepted": True, "selected": True, "key": "dob", "sentence": "DOB 04/27/2026"}
    rows = {"dob": [row]}
    staged = SimpleNamespace(fields=rows, rows=lambda f: rows.get(f, []),
                             accepted=lambda f: [r for r in rows.get(f, []) if r.get("accepted")])
    page = {"page_no": 1, "page_name": "1.jpg", "text": "Patient Name: Justin Anderson", "staged": staged}
    result = verify_record("rec", [page], exp, detect_name_mode(exp))
    assert result.pages[0].detected_dob == "N/A"
    assert result.pages[0].ner_key_source_dob == DROPPED_FAR
    assert not result.pages[0].page_verified
