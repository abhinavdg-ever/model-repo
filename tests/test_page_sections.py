"""Shared page detectors (Brief 5 section 3, 2026-11-22). Made-up text only."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.page_classify import sections  # noqa: E402

NOTE = """Office Visit 3/4/2025 Made Up Clinic Provider: Dr Made Up
Chief Complaint: cough
History of Present Illness:
made up text
Review of Systems:
made up
Physical Exam:
made up
Assessment and Plan:
made up
Electronically signed by Dr Made Up
"""


def test_sections_in_page_order_signature_never_counts():
    names = [s.name for s in sections.note_sections(NOTE)]
    assert names[0] == "Chief complaint" and names[-1] == "Assessment and plan (combined)"
    assert all("sign" not in n.lower() for n in names)


def test_running_header_lines_are_excluded():
    page = "Chief Complaint: x\nPlan:\nmade up\n"
    assert [s.name for s in sections.note_sections(page, exclude=["Plan:"])] == ["Chief complaint"]


def test_ranks_follow_the_note_order():
    assert sections.first_section_rank(NOTE) == 1
    assert sections.last_section_rank(NOTE) == 8
    assert sections.last_section_rank("made up lines only") is None


def test_start_patterns():
    assert sections.start_pattern(NOTE) == sections.VISIT_CC
    soap = "Office Visit 3/4/2025 Clinic Provider: Dr X\nS: made up\nO: made up\n"
    assert sections.start_pattern(soap) == sections.VISIT_SOAP
    history = "Date of Service: 03/04/2025\nPast Medical History:\nmade up\nChief Complaint: x\n"
    assert sections.start_pattern(history) == sections.VISIT_HISTORY_FIRST
    assert sections.start_pattern("Chief Complaint: x\n") is None  # no visit header


def test_lab_markers():
    report = "Performing Lab: Made Up Labs\nCLIA 00D0000000\nAccession 1234\nCollected: 01/02/2025\nFinal Report\n"
    assert set(sections.standalone_lab_markers(report)) == {"performing_lab", "clia", "accession",
                                                             "specimen_block", "final_report"}
    embedded = "Dr Made Up Clinic\nLab Results:\nSodium 140\n"
    assert sections.embedded_result_markers(embedded, previous_note_text="Dr Made Up Clinic\nChief Complaint: x") == \
        ["result_heading", "same_running_header"]


def test_visual_pages():
    assert sections.is_visual("ECG tracing 12 lead")
    assert not sections.is_visual(" ".join(["word"] * 40))
    assert sections.is_visual(" ".join(["word"] * 40), picture_share=0.7)
    assert not sections.is_visual("few words", kind="junk")
