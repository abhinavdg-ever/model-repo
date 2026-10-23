"""build_taxonomy_v29.py on a made-up rules file."""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import build_taxonomy_v29 as b  # noqa: E402

RULES = {
    "version": 29,
    "L1": ["Progress Note", "Lab Report/Result", "Consent and Authorization", "Problem List"],
    "progress_note": {"L2_values": ["Others", "CC/HPI"]},
    "lab_report_L2": ["Radiology"],
    "consent_auth_L2": ["General Consent", "HIPAA and Privacy", "Others"],
    "L3_additions": {"Lab Report/Result": {"Radiology": ["CT Angiography Report"]}},
    "L3_merge": {"Lab Report/Result": {"Radiology": {"Sonogram Report": "Ultrasound Report"}}},
    "specialty_values": ["Podiatry", "Unknown"],
    "page_type_overrides": {"_comment": "x", "HIPAA Form": {"L1": "Consent and Authorization", "L2": "HIPAA and Privacy"}},
}
V24 = {
    "rules": {"section_L1": "Progress Note", "fallback": "Others",
              "codeability_values": ["codeable", "non_codeable", "discharge", "needs_review"]},
    "L1": [{"name": "Progress Note", "description": "a note"}],
    "L2": {"Consent Form": [{"name": "Others", "description": "old", "specialty": "", "codeability": "codeable",
                             "codeability_tag": "Codeable", "notes": ""}]},
    "L3": {"Lab Report/Result > Radiology": [{"name": "Sonogram Report"}, {"name": "Ultrasound Report"},
                                              {"name": "Others"}]},
    "specialty": [],
}


def test_others_goes_last_and_l1s_without_a_list_have_no_l2():
    lists = b.l2_lists(RULES)
    assert lists["Progress Note"] == ["CC/HPI", "Others"]
    assert lists["Problem List"] == []


def test_l3_drops_merged_names_adds_new_ones_and_keeps_others_last():
    t = b.build(RULES, V24, {"Consent and Authorization > General Consent": "codeable"})
    leaves = [x["name"] for x in t["L3"]["Lab Report/Result > Radiology"]]
    assert leaves == ["CT Angiography Report", "Ultrasound Report", "Others"]
    assert t["version"] == "v29" and "Problem List" in t["paths"]


def test_a_new_l2_takes_its_labels_codeability():
    t = b.build(RULES, V24, {"Consent and Authorization > General Consent": "codeable"})
    general = next(x for x in t["L2"]["Consent and Authorization"] if x["name"] == "General Consent")
    hipaa = next(x for x in t["L2"]["Consent and Authorization"] if x["name"] == "HIPAA and Privacy")
    assert general["codeability"] == "codeable" and hipaa["codeability"] == "needs_review"


def test_an_override_naming_an_l2_outside_its_l1_is_reported():
    lists = b.l2_lists(RULES)
    assert b.override_problems(RULES, lists) == []
    bad = {**RULES, "page_type_overrides": {"Coding Summary": {"L1": "Consent and Authorization",
                                                               "L2": "Administrative Record"}}}
    assert b.override_problems(bad, lists) == ["Coding Summary: L2 'Administrative Record' is not under Consent and Authorization"]
