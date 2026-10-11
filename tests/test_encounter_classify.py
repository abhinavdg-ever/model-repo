"""Tests for tiered, per-visit encounter-type classification."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from stages.lib.encounter.encounter_classify import (  # noqa: E402
    CANON_PATH,
    CanonError,
    _parse_canon,
    classify_pages,
    decide,
    gather,
    load_canon,
    visit_date,
)

DEFAULT = "2022-02-02"


@pytest.fixture(scope="module")
def canon():
    load_canon.cache_clear()
    return load_canon()


def page(pid, text="", dos="2025-03-14", type_id="", name=""):
    return {
        "page_id": pid, "page_name": f"{pid}.jpg", "page_number": pid, "text": text,
        "dos_from": dos, "dos_to": dos, "reason": "" if dos else "no_date",
        "page_type_id": type_id, "page_type_name": name or type_id,
    }


def one(canon, *pages):
    """The decision for a single visit made of ``pages``."""
    return decide(gather(list(pages), canon), canon)


# --- loader --------------------------------------------------------------


def test_shipped_canon_loads(canon):
    assert set(canon.labels) == {"home", "outpatient_tele", "outpatient_f2f", "inpatient"}
    assert canon.priority["home"] < canon.priority["inpatient"]


def _raw():
    return json.loads(CANON_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["tier2"]["home"].append("hospital course"), "is in both"),
        (lambda r: r["tier3"]["inpatient"].append("chief complaint"), "is in both"),
        (lambda r: r["tier1_page_types"].update({"no_such_type": "home"}), "not a page type or sub-type"),
        (lambda r: r["tier1_page_types"].update({"Discharge Summary": "space"}), "unknown setting"),
        (lambda r: r["negatives"].append({"phrase": "x y", "cancels": ["not a phrase"]}),
         "not a tier 2 or tier 3 phrase"),
        (lambda r: r["context"].append("telehealth"), "also scores"),
    ],
)
def test_loader_refuses_a_bad_canon(mutate, message):
    raw = copy.deepcopy(_raw())
    mutate(raw)
    with pytest.raises(CanonError, match=message):
        _parse_canon(raw)


# --- step 1: visits ------------------------------------------------------


def test_visit_date_prefers_the_document_level_and_skips_the_default():
    assert visit_date(page_from="", page_to="", doc_from="2025-03-14", doc_to="2025-03-14",
                      default_date=DEFAULT) == ("2025-03-14", "2025-03-14", "")
    assert visit_date(page_from="", page_to="", doc_from=DEFAULT, doc_to=DEFAULT,
                      default_date=DEFAULT) == ("", "", "default_date")
    assert visit_date(page_from="", page_to="", doc_from="", doc_to="",
                      default_date=DEFAULT) == ("", "", "no_date")


def test_same_date_separated_by_another_visit_is_two_visits(canon):
    rows = classify_pages(
        [
            page(1, "telehealth visit", dos="2025-03-14"),
            page(2, "hospital course", dos="2025-03-20"),
            page(3, "no setting words", dos="2025-03-14"),
        ],
        entries=canon,
    )
    assert [r["encounter_type"] for r in rows] == ["outpatient_tele", "inpatient", ""]
    assert rows[2]["reason"] == "no_setting_evidence"


def test_undated_pages_never_inherit(canon):
    rows = classify_pages(
        [page(1, "telehealth visit"), page(2, "meds continued", dos="")], entries=canon
    )
    assert rows[0]["encounter_type"] == "outpatient_tele"
    assert rows[1]["encounter_type"] == ""
    assert rows[1]["reason"] == "no_date"
    assert rows[1]["confidence"] == 0


# --- step 2: findings ----------------------------------------------------


def test_repetition_does_not_add_weight(canon):
    evidence = gather([page(1, "follow up " * 20 + " discharge summary")], canon)
    assert [f.source for f in evidence.findings].count("follow up") == 1


def test_trailing_punctuation_phrases_match(canon):
    evidence = gather([page(1, "HPI:cough A/P:rest")], canon)
    assert {"hpi:", "a/p:"} <= {f.source for f in evidence.findings}


def test_word_boundaries(canon):
    # "inpatient" must not hit inside "outpatient".
    evidence = gather([page(1, "outpatient clinic")], canon)
    assert all(f.setting != "inpatient" for f in evidence.findings)


def test_context_never_scores(canon):
    d = one(canon, page(1, "Radiology report and MRI report"))
    assert d.setting == "" and d.reason == "no_setting_evidence"


def test_negative_cancels_the_finding_it_names(canon):
    d = one(canon, page(1, "Home visit. Patient was discharged home yesterday."))
    assert d.setting == ""


def test_negatives_do_not_touch_page_type_evidence(canon):
    d = one(canon, page(1, "discharged home last week", type_id="HouseCalls visit summary"))
    assert d.setting == "home"


# --- step 3: decide ------------------------------------------------------


def test_tier3_alone_never_decides(canon):
    d = one(canon, page(1, "Chief complaint: cough. Follow up in 2 weeks."))
    assert d.setting == "" and d.decided_by == "unresolved"


def test_tier1_beats_any_amount_of_tier2_and_tier3(canon):
    d = one(canon, page(1, "office visit clinic visit follow up " * 10, type_id="Discharge Summary",
                        name="Discharge Summary"))
    assert (d.setting, d.decided_by, d.confidence) == ("inpatient", "tier1", 0.95)
    assert d.matched_keyword == "Discharge Summary"


def test_tier2_single_setting(canon):
    d = one(canon, page(1, "Telehealth visit. Follow up in 4 weeks."))
    assert (d.setting, d.decided_by, d.confidence, d.conflict) == (
        "outpatient_tele", "tier2", 0.80, False)


def test_conflict_goes_to_more_tier3_hints_and_is_flagged(canon):
    d = one(
        canon,
        page(1, type_id="Discharge Summary"),
        page(2, "chief complaint, follow up, subjective: ...", type_id="Office Visit"),
    )
    assert (d.setting, d.confidence, d.conflict) == ("outpatient_f2f", 0.70, True)
    assert set(d.contenders) == {"inpatient", "outpatient_f2f"}


def test_conflict_without_hints_goes_to_priority(canon):
    d = one(canon, page(1, "home visit and telehealth"))
    assert d.setting == "home"  # Home 10 beats Tele 20
    assert d.conflict and d.confidence == 0.60


# --- the worked example --------------------------------------------------


def test_worked_example(canon):
    hospital = (
        "hospital course ... admission date 03/12/2025 ... discharged home ... "
        + "follow up " * 14 + "chief complaint " * 3 + "history and physical " * 2
    )
    pages = (
        [page(1, hospital, dos="2025-03-14")]
        + [page(3, "", dos="2025-03-14", type_id="Discharge Summary", name="Discharge Summary")]
        + [page(7, "", dos="2025-03-14", type_id="Progress Note")]
        + [page(25, "telehealth follow up telehealth", dos="2025-04-02")]
        + [page(26, "", dos="2025-04-02", type_id="Progress Note")]
        + [page(32, "whatever", dos="")]
    )
    log: list = []
    rows = {r["page_id"]: r for r in classify_pages(pages, entries=canon, visit_log=log)}

    a = rows[1]
    assert (a["encounter_type"], a["confidence"], a["decided_by"], a["matched_keyword"]) == (
        "inpatient", 0.95, "tier1", "Discharge Summary")
    assert rows[7]["encounter_type"] == "inpatient"
    assert "discharged home" in log[0]["negatives_fired"]

    b = rows[25]
    assert (b["encounter_type"], b["confidence"], b["decided_by"], b["matched_keyword"]) == (
        "outpatient_tele", 0.80, "tier2", "telehealth")
    assert rows[26]["encounter_type"] == "outpatient_tele"
    assert rows[26]["continue_applied"] == "y"  # its own text gives no answer

    assert (rows[32]["encounter_type"], rows[32]["reason"]) == ("", "no_date")


# --- label-free regression checks ---------------------------------------


@pytest.mark.parametrize("type_id", ["Discharge Summary", "Operative Report", "ICU Note"])
def test_a_hospital_document_is_never_f2f(canon, type_id):
    d = one(canon, page(1, "office visit follow up chief complaint " * 5, type_id=type_id))
    assert d.setting != "outpatient_f2f"


def test_discharged_home_text_is_not_home(canon):
    assert one(canon, page(1, "home visit; patient discharged home")).setting != "home"


# --- visits follow continuity documents ----------------------------------


def test_a_continuity_document_is_one_visit_even_across_dates(canon):
    pages = [
        {**page(1, "telehealth visit by video", dos="2025-03-14"), "document": 1},
        {**page(2, "review of systems", dos="2025-03-20"), "document": 1},
        {**page(3, "", dos=""), "document": 1},
        {**page(4, "inpatient hospital course", dos="2025-03-14"), "document": 2},
    ]
    rows = classify_pages(pages, entries=canon)
    assert rows[0]["encounter_type"] == rows[1]["encounter_type"] == rows[2]["encounter_type"]
    assert rows[0]["encounter_type"] == "outpatient_tele"
    assert rows[3]["encounter_type"] != "outpatient_tele"


def test_a_document_with_no_date_on_any_page_is_unresolved(canon):
    pages = [{**page(1, "telehealth visit", dos=""), "document": 1},
             {**page(2, "", dos=""), "document": 1}]
    rows = classify_pages(pages, entries=canon)
    assert rows[0]["encounter_type"] == "" and rows[1]["encounter_type"] == ""


def test_without_documents_visits_are_date_runs(canon):
    rows = classify_pages([page(1, "telehealth visit", dos="2025-03-14"),
                           page(2, "", dos="2025-04-01")], entries=canon)
    assert rows[0]["encounter_type"] == "outpatient_tele" and rows[1]["encounter_type"] == ""
