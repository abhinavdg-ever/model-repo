"""Provider signatures in a Progress Note encounter (2026-11-24, user): first / last page keep
theirs; a middle lab / medication page's is dropped; another middle page's is kept at x 0.80.
Other encounters are unchanged. Made-up values."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as V

import pytest

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

pytest.importorskip("psycopg", reason="imaging_final imports the db layer")
from modules.imaging_final.stage import progress_note_signatures  # noqa: E402


def _sig(name, confidence=0.9):
    return {"signature_present": True, "provider_name": name, "provider_credentials": "MD", "signers": None,
            "provider_version": 1, "provider_confidence": confidence}


def test_progress_note_middle_pages():
    sigs = {i: _sig(f"P{i}") for i in range(1, 7)}
    pn = lambda l2: V(page_type=f"Progress Note ({l2})", page_subtype=None)  # noqa: E731
    verdicts = {1: pn("CC/HPI"), 2: pn("Lab and Imaging Results"), 3: pn("Assessment and Plan"),
                4: pn("Medication List"), 5: pn("Physical Exam"), 6: pn("Signature")}
    classification = {5: {"page_type": "Lab Report", "page_subtype": None}}
    out = progress_note_signatures(sigs, {1: [1, 2, 3, 4, 5, 6]}, verdicts, classification)
    assert out[1] == sigs[1] and out[6] == sigs[6]                           # first / last page
    assert out[2]["provider_name"] is None and out[2]["signature_present"] is False  # lab results
    assert out[4]["provider_name"] is None                                   # medication list
    assert out[5]["provider_name"] is None                                   # its own type: Lab Report
    assert out[3]["provider_name"] == "P3" and out[3]["provider_confidence"] == 0.72  # kept, x 0.80
    assert sigs[3]["provider_confidence"] == 0.9                             # input untouched


def test_other_encounters_unchanged():
    sigs = {i: _sig(f"P{i}") for i in range(1, 4)}
    lab = V(page_type="Lab Report (Chemistry)", page_subtype=None)
    out = progress_note_signatures(sigs, {1: [1, 2, 3]}, {1: lab, 2: lab, 3: lab}, {})
    assert out == sigs
