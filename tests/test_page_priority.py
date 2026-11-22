"""Family priority order and the Progress Note model guard (Brief 4 section 3.3, 2026-11-22).
Synthetic pages only; the model is a stand-in with the probabilities each case names."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.page_classify import leaf, priority  # noqa: E402

PN, LAB, DS, OP, PROC = "Progress Note", "Lab Report", "Discharge Summary", "Operative Note", "Procedure Note"

LAB_TABLE = """Specimen: Blood
Collected: 01/02/2025
Test  Result  Reference Range  Flag
Sodium  140  135-145
Potassium  4.1  3.5-5.1
CLIA #00D0000000
Accession 123456
"""
QUEST = "LABORATORY REPORT\nPerforming lab: Made Up Labs\n" + LAB_TABLE


def model(*pairs):
    """A stand-in page-leaf model: (leaf path, probability) pairs."""
    cands = [{"path": p, "score": s} for p, s in pairs]
    return lambda text, k, per_l1: leaf.ModelSide(leaf.LEAF_MODEL, cands, {p: s for p, s in pairs})


def classify(monkeypatch, text, *pairs):
    monkeypatch.setattr(leaf, "model_side", model(*pairs) if pairs else (lambda t, k, n: leaf.ModelSide()))
    return leaf.predict(text)


def test_note_headers_and_a_lab_table_is_a_progress_note(monkeypatch):
    text = "Chief Complaint: follow up\n" + LAB_TABLE + "Assessment and Plan:\ncontinue\n"
    r = classify(monkeypatch, text)
    assert r.l1 == PN and r.leaf["priority_rule"]["rule"] == "progress_note_headers"


def test_a_quest_style_report_with_no_note_headers_is_a_lab_report(monkeypatch):
    r = classify(monkeypatch, QUEST, ("Lab Report > Lab Result > Chemistry Report", 0.8))
    assert r.l1 == LAB and not (r.leaf["priority_rule"] or {}).get("rule")


def test_discharge_headers_beat_note_headers(monkeypatch):
    r = classify(monkeypatch, "Hospital Course:\nstable\nDischarge Diagnosis:\nmade up\nHPI:\nmade up\n")
    assert r.l1 == DS and r.leaf["priority_rule"]["rule"] == "discharge_summary"


def test_operative_headers_beat_note_headers(monkeypatch):
    text = "Preoperative Diagnosis:\nx\nPostoperative Diagnosis:\nx\nProcedure Performed:\nx\nHPI:\nx\n"
    r = classify(monkeypatch, text)
    assert r.l1 == OP and r.leaf["priority_rule"]["rule"] == "operative_note"


def test_a_consultation_letter_does_not_fire_the_note_rule():
    text = "Dear Dr. Made Up,\nHPI:\nmade up\nAssessment:\nmade up\nSincerely,\nDr. Other\n"
    rule = next(r for r in priority.settings()["rules"] if r["name"] == "progress_note_headers")
    assert priority.rule_fires(rule, text, ()) is None


def test_the_note_rule_yields_to_a_sure_operative_model(monkeypatch):
    r = classify(monkeypatch, "HPI:\nmade up\nPlan:\nmade up\n", ("Operative Note > Surgery Report", 0.95))
    assert r.l1 == OP
    assert any("yielded" in y for y in r.leaf["priority_rule"]["yielded"])


def test_the_guard_keeps_a_note_with_a_lab_table(monkeypatch):
    text = QUEST.replace("LABORATORY REPORT\n", "")
    r = classify(monkeypatch, text, ("Progress Note > Lab and Imaging Results", 0.70),
                 ("Lab Report > Lab Result > Chemistry Report", 0.25))
    assert r.l1 == PN and r.leaf["priority_rule"]["rule"] == priority.GUARD


def test_the_guard_yields_to_a_discharge_title(monkeypatch):
    r = classify(monkeypatch, "DISCHARGE SUMMARY\nmade up text\n", ("Progress Note > CC/HPI", 0.70))
    assert r.l1 == DS and r.leaf["priority_rule"]["rule"] == "discharge_summary"


def test_an_unsure_model_does_not_fire_the_guard(monkeypatch):
    r = classify(monkeypatch, QUEST, ("Progress Note > CC/HPI", 0.30), ("Lab Report > Lab Result > Chemistry Report", 0.6))
    assert r.l1 == LAB


def test_a_diagnosis_list_title_is_a_problem_list(monkeypatch):
    r = classify(monkeypatch, "DIAGNOSIS LIST\nmade up\nmade up\n")
    assert r.l1 == "Problem List" and r.l2 == "Diagnosis List/Coding Summary"


def test_a_rule_against_the_model_is_flipped_and_decided_by_rule(monkeypatch):
    text = "Hospital Course:\nstable\nDischarge Diagnosis:\nmade up\n"
    r = classify(monkeypatch, text, ("Progress Note > CC/HPI", 0.40), ("Lab Report > Lab Result", 0.35))
    assert r.l1 == DS and r.decided_by == "rule" and r.page_type_confidence == pytest.approx(0.3)
