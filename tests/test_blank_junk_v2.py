"""Blank / Junk v2 (2026-11-28, user; docs/specials/BLANK_JUNK_V2_LOGIC.md): route + four-tier ladder."""
from __future__ import annotations

import modules.blank_junk.stage as stage  # noqa: F401  — puts the blank_junk lib on sys.path

import method  # noqa: E402
from method import PageInputs, decide  # noqa: E402

FILLER = "the quick brown fox jumps over a lazy dog near the river bank today " * 3
FAX = "FAX COVER SHEET\nUrgent\nFor Review\nPlease Reply\n" + FILLER
LAB = "Lab Results\nReference Range 3.5-5.0\nValue 4.2\n" + FILLER


def bert(p_junk, label="JUNK_OTHERS", flag=None, p_blank=0.0):
    p_keep = round(max(0.0, 1.0 - p_junk - p_blank), 4)
    flag = flag or ("JUNK" if p_junk >= 0.70 else "KEEP")
    return {"p_keep": p_keep, "p_junk": p_junk, "p_blank": p_blank, "top_junk_label": label,
            "service": {"flag": flag, "subclass": label if flag == "JUNK" else "", "confidence": max(p_keep, p_junk),
                        "review_required": False, "decision_reason": "argmax", "audit_tag": "",
                        "p_keep": p_keep, "p_blank": p_blank, "p_junk": p_junk}}


def v2(text, **kw):
    kw.setdefault("bert", bert(0.5))
    return decide(PageInputs(text=text, pass_no=2, document_type=kw.pop("document_type", "printed"), logic="v2", **kw))


def test_tier1_beats_a_keep_and_the_veto_saves_a_sure_keep():
    v = v2(FAX + LAB)  # fax cover sheet (tier 1) beats the lab keep (tier 2)
    assert v.rule == "R4e" and v.code == method.CODE_LETTER_FAX and v.votes["route"] == "general"
    v = v2(FAX, bert=bert(0.005))  # p(keep) >= 0.99: vetoed -> Main, review
    assert v.code == method.CODE_MAIN and v.rule == "O3" and v.votes["review_required"]


def test_tier3_loses_to_a_keep():
    fax_header_unsigned = "Fax\n" + LAB
    v = v2(fax_header_unsigned, headings=("Fax",))
    assert v.code == method.CODE_MAIN and v.rule == "G4"


def test_model_blank_on_text_is_set_aside_with_review():
    v = v2(FILLER, bert=bert(0.20, flag="BLANK", p_blank=0.5))  # p(keep) 0.30 >= p(junk) 0.20
    assert v.code == method.CODE_MAIN and v.votes.get("model_blank_on_text") and v.votes["review_required"]


def test_invoice_evidence_without_the_model_saying_invoice():
    text = "INVOICE\nBill To: Clinic\n| Copy fee | $25.00 |\nTotal $25.00\n" + FILLER
    v = v2(text, bert=bert(0.2), final1_text=text)
    assert v.code == method.CODE_MAIN and v.rule == "O2" and v.votes["invoice_evidence"]["rule"] == "I1"


def test_visual_route_needs_no_model():
    v = v2("photo", document_type="visual", bert=None)
    assert v.votes["route"] == "visual" and v.rule == "V2" and v.code == method.CODE_MAIN


def test_pass1_trims():
    tiny = decide(PageInputs(text="ab 12", pass_no=1, document_type="printed", logic="v2"))
    assert tiny.rule != "R2c" and tiny.code == method.CODE_MAIN  # R2c moved to pass 2's Short route
    assert decide(PageInputs(text="ab 12", pass_no=1, document_type="printed")).rule == "R2c"  # v1 unchanged
    report = "Patient report printed for the record " + "word " * 12 + "\nEnd of Report"
    assert decide(PageInputs(text=report, pass_no=1, document_type="printed", logic="v2")).rule == "R2b"  # < 25 words
    assert decide(PageInputs(text=report, pass_no=1, document_type="printed")).rule != "R2b"  # v1: < 10 words


def test_r4c_is_kept_and_never_vetoed():
    v = v2(FILLER, bert=bert(0.0, p_blank=0.99))
    assert v.rule == "R4c" and v.code != method.CODE_MAIN


def test_blank_junk_logic_switch(monkeypatch):
    monkeypatch.setenv("BLANK_JUNK_LOGIC", "shadow")
    assert stage.blank_junk_logic() == "shadow"
    monkeypatch.setenv("BLANK_JUNK_LOGIC", "nonsense")
    assert stage.blank_junk_logic() == "v1"
