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

DEFAULT = "2222-02-02"


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
    assert set(canon.labels) == {"home", "outpatient_tele", "outpatient_f2f", "inpatient", "not_applicable"}
    assert canon.priority["home"] < canon.priority["inpatient"]


def _raw():
    return json.loads(CANON_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["tier2"]["home"].append("hospital course"), "is in both"),
        (lambda r: r["tier3"]["inpatient"].append("chief complaint"), "is in both"),
        (lambda r: r["tier1_page_types"].update({"no_such_type": "home"}), "not a v24 canon page type"),
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
    assert [r["encounter_type"] for r in rows] == ["outpatient_tele", "inpatient", "outpatient_f2f"]
    assert rows[2]["decided_by"] == "default"
    assert rows[2]["reason"] == "no_setting_evidence"


def test_undated_pages_never_inherit(canon):
    rows = classify_pages(
        [page(1, "telehealth visit"), page(2, "meds continued", dos="")], entries=canon
    )
    assert rows[0]["encounter_type"] == "outpatient_tele"
    assert (rows[1]["encounter_type"], rows[1]["decided_by"]) == ("outpatient_f2f", "default")
    assert rows[1]["reason"] == "no_setting_evidence"  # nothing on it votes
    assert rows[1]["confidence"] == 0.3


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
    # Page 1 (hospital course, x2) and page 3 (Discharge Summary) vote inpatient; page 7 abstains.
    assert (a["encounter_type"], a["confidence"], a["decided_by"], a["matched_keyword"]) == (
        "inpatient", 1.0, "vote", "hospital course")
    assert [v["weight"] for v in log[0]["votes"]] == [2.0, 1.0, 1.0]
    assert rows[7]["encounter_type"] == "inpatient"
    assert "discharged home" in log[0]["negatives_fired"]

    b = rows[25]
    assert (b["encounter_type"], b["confidence"], b["decided_by"], b["matched_keyword"]) == (
        "outpatient_tele", 1.0, "vote", "telehealth")
    assert rows[26]["encounter_type"] == "outpatient_tele"
    assert rows[26]["continue_applied"] == "y"  # its own text gives no answer

    assert (rows[32]["encounter_type"], rows[32]["decided_by"]) == ("outpatient_f2f", "default")


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


def test_a_document_with_no_date_still_votes(canon):
    pages = [{**page(1, "telehealth visit", dos=""), "document": 1},
             {**page(2, "", dos=""), "document": 1}]
    rows = classify_pages(pages, entries=canon)
    assert all((r["encounter_type"], r["decided_by"]) == ("outpatient_tele", "vote") for r in rows)


def test_without_documents_visits_are_date_runs(canon):
    rows = classify_pages([page(1, "telehealth visit", dos="2025-03-14"),
                           page(2, "", dos="2025-04-01")], entries=canon)
    assert rows[0]["encounter_type"] == "outpatient_tele"
    assert (rows[1]["encounter_type"], rows[1]["decided_by"]) == ("outpatient_f2f", "default")


def test_no_setting_evidence_defaults_to_f2f_low_confidence(canon):
    rows = classify_pages([page(1, "Chief complaint: cough.", dos="2025-03-14")], entries=canon)
    r = rows[0]
    assert (r["encounter_type"], r["decided_by"], r["confidence"]) == ("outpatient_f2f", "default", 0.3)
    assert r["reason"] == "no_setting_evidence" and r["matched_keyword"] == ""


def test_an_empty_default_leaves_the_visit_unresolved(canon):
    from dataclasses import replace
    rows = classify_pages([page(1, "Chief complaint: cough.", dos="2025-03-14")],
                          entries=replace(canon, default_setting=""))
    assert (rows[0]["encounter_type"], rows[0]["decided_by"]) == ("", "unresolved")


# --- applies_to: Progress Note / Discharge Summary only ------------------


def test_a_lab_encounter_is_not_applicable_on_every_page(canon):
    pages = [{**page(1, "telehealth visit"), "page_family": "Lab Report/Result", "document": 1},
             {**page(2, "office visit"), "page_family": "Progress Note", "document": 1}]
    rows = classify_pages(pages, entries=canon)
    # One page votes a setting: Not Applicable is set aside, however the lab pages weigh.
    assert all((r["encounter_type"], r["decided_by"]) == ("outpatient_f2f", "vote") for r in rows)
    only_labs = [{**page(n, "telehealth visit"), "page_family": "Lab Report/Result", "document": 1} for n in (1, 2)]
    rows = classify_pages(only_labs, entries=canon)
    assert all((r["encounter_type"], r["encounter_label"]) == ("not_applicable", "Not Applicable") for r in rows)
    assert rows[0]["matched_keyword"] == "Lab Report/Result"


def test_progress_notes_and_discharge_summaries_are_classified(canon):
    for family, text, expected in (("Progress Note", "telehealth visit", "outpatient_tele"),
                                   ("Discharge Summary", "", "inpatient"),
                                   ("Progress Note", "", "outpatient_f2f")):
        rows = classify_pages([{**page(1, text), "page_family": family}], entries=canon)
        assert rows[0]["encounter_type"] == expected


def test_the_first_classified_page_sets_the_family(canon):
    pages = [{**page(1, "telehealth visit"), "page_family": "Unknown", "document": 1},
             {**page(2, ""), "page_family": "Progress Note", "document": 1}]
    rows = classify_pages(pages, entries=canon)
    assert rows[0]["encounter_type"] == "outpatient_tele"


def test_follow_up_visits_are_outpatient_f2f(canon):
    for text in ("Follow up visit for hypertension.", "Follow-up appointment, established patient.",
                 "Return visit: knee pain."):
        d = one(canon, page(1, text))
        assert (d.setting, d.decided_by) == ("outpatient_f2f", "tier2"), text


# --- every page votes, the first page counts twice (2026-10-23) ---------


def _doc(*pages):
    return [{**p, "document": 1} for p in pages]


def test_the_first_page_counts_twice_and_wins_a_tie(canon):
    pages = _doc({**page(1, "telehealth visit"), "page_family": "Progress Note"},
                 {**page(2, "hospital course"), "page_family": "Progress Note"},
                 {**page(3, "hospital course"), "page_family": "Progress Note"})
    log: list = []
    rows = classify_pages(pages, entries=canon, visit_log=log)
    # 2 (tele, first page) against 2 (inpatient): the first page's vote.
    assert rows[0]["encounter_type"] == "outpatient_tele" and rows[0]["confidence"] == 0.5
    assert rows[1]["continue_applied"] == "y"


def test_more_pages_outvote_the_first(canon):
    pages = _doc({**page(1, "telehealth visit"), "page_family": "Progress Note"},
                 *[{**page(n, "hospital course"), "page_family": "Progress Note"} for n in (2, 3, 4)])
    rows = classify_pages(pages, entries=canon)
    assert rows[0]["encounter_type"] == "inpatient" and rows[0]["confidence"] == 0.6


def test_family_defaults_vote_when_a_page_has_no_evidence(canon):
    pages = _doc({**page(1, ""), "page_family": "Discharge Summary"},
                 {**page(2, ""), "page_family": "Progress Note"},
                 {**page(3, ""), "page_family": "Lab Report/Result"})
    log: list = []
    rows = classify_pages(pages, entries=canon, visit_log=log)
    assert [(v["setting"], v["by"]) for v in log[0]["votes"]] == [
        ("inpatient", "family_default"), ("outpatient_f2f", "family_default"), ("not_applicable", "not_applicable")]
    assert rows[0]["encounter_type"] == "inpatient"
