"""The shipped blank/junk decision config: demographic pages are not junked as Invoice
on the model's score alone."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "core-pipeline" / "models" / "blank-junk" / "default.json"
VENDOR = ROOT / "core-pipeline" / "stages" / "lib" / "blank_junk" / "model"

FACESHEET = (
    "FRANCISCAN SYSTEM SERVICES MRN: 00837157 DOB: 01/29/1958\n"
    "01/23/2025 - ED in St. Anthony Hospital Facesheet\n"
    "Patient Demographics Name Patient ID SSN Birth Date\n"
    "Emergency Contact 1\n"
    "Hospital Account Name Acct ID Class Emergency Status Billed Primary Coverage AARP MEDICARE\n"
    "Guarantor Account (for Hospital Account #144732886)\n"
    "Coverage Information Subscriber # Admission Information\n"
)


def _decide(proba, text=FACESHEET):
    sys.path.insert(0, str(VENDOR))
    try:
        from src.models.bert_classifier import BERT_LABELS
        from src.models.decision import DecisionConfig, decide_from_proba
    finally:
        sys.path.remove(str(VENDOR))
    decision = json.loads(CONFIG.read_text(encoding="utf-8"))["decision"]
    probs = [proba.get(label, 0.0) for label in BERT_LABELS]
    return decide_from_proba(probs, list(BERT_LABELS), ocr_text=text, config=DecisionConfig(**decision))


def test_invoice_needs_a_billing_phrase_to_junk_a_demographic_page():
    result = _decide({"JUNK_INVOICE": 0.97, "KEEP": 0.02, "JUNK_OTHERS": 0.01})
    assert result.flag == "KEEP"
    assert result.reason == "protocol_retain_demographic"


def test_a_real_invoice_with_demographics_is_still_junk():
    text = "Patient DOB: 01/29/1958 MRN 123\nINVOICE\nAmount due: $120.00\nBalance due: $120.00\n"
    result = _decide({"JUNK_INVOICE": 0.97, "KEEP": 0.02, "JUNK_OTHERS": 0.01}, text)
    assert result.flag == "JUNK"
