"""2026-11-28 (user): a page headed "Signature Log" with Medium / High handwriting (>= 20%) is not
junk: Main, Page Type Progress Note / Signature (ground truth "Signature page"), Non-Codable."""
from __future__ import annotations

import modules.blank_junk.stage  # noqa: F401  — puts the blank_junk lib on sys.path

import method  # noqa: E402
from method import PageInputs, decide  # noqa: E402

from apps.worker.stages._support import is_invoice_page, is_signature_log_page  # noqa: E402

LOG_TEXT = (
    "datavant\nSIGNATURE LOG Site ID: Group Name: State:\n"
    "The Medical Records provided by this office, as requested on March 2026, are true and accurate copies\n"
    "Practice Group Administrator/Custodian of Medical Records\n"
    "Below is a log of signatures used by clinicians in the Medical Records.\n"
    "NPI Provider Full Name (Printed Name) Credential Legal Signature Actual Chart Signature Variations\n"
    "Securely respond to Datavant-managed requests in a single, up-to-date queue. Login or Signup here\n"
)


def bert_junk() -> dict:
    return {"p_keep": 0.01, "p_junk": 0.99, "p_blank": 0.0, "top_junk_label": "JUNK_RECORD_REQUEST",
            "service": {"flag": "JUNK", "subclass": "JUNK_RECORD_REQUEST", "confidence": 0.99,
                        "review_required": False, "decision_reason": "argmax", "audit_tag": "",
                        "p_keep": 0.01, "p_blank": 0.0, "p_junk": 0.99}}


def run(hw: float | None, text: str = LOG_TEXT, pass_no: int = 2, **kw) -> method.Verdict:
    return decide(PageInputs(text=text, pass_no=pass_no, document_type="visual", hw_area_pct=hw,
                             bert=bert_junk() if pass_no == 2 else None, **kw))


def test_filled_log_medium_or_high_is_main():
    for hw in (20.0, 31.0, 75.0):
        v = run(hw)
        assert v.code == method.CODE_MAIN and v.rule == "SIGLOG", (hw, v.rule, v.reason)
        assert is_signature_log_page(v.reason) and not is_invoice_page(v.reason)


def test_empty_log_low_handwriting_left_to_the_other_rules():
    for hw in (None, 3.0, 19.9):
        v = run(hw)
        assert v.rule != "SIGLOG" and not is_signature_log_page(v.reason), (hw, v.rule)


def test_only_signature_log_header_counts():
    v = run(40.0, text=LOG_TEXT.replace("SIGNATURE LOG", "RELEASE FORM"))
    assert v.rule != "SIGLOG"
    # A "signature log" mention far down the page is not the header.
    body = "\n".join(f"line {i} of the record request letter" for i in range(12)) + "\nsee the signature log"
    assert run(40.0, text=body).rule != "SIGLOG"
    # A stage-6 section header says it too.
    v = run(40.0, text="Datavant\nrecords request", headings=("Signature Log",))
    assert v.rule == "SIGLOG"


def test_pass_1_does_not_decide_it():
    assert run(40.0, pass_no=1).rule != "SIGLOG"


def test_page_classification_row_matches_the_ground_truth_signature_page():
    from modules.page_classify import stage as page_classify

    row = page_classify._signature_log_row(0.9)
    assert (row["page_type"], row["page_subtype"]) == ("Progress Note", "Signature")
    assert row["classification_category"] == "non_codeable" and row["decided_by"] == "signature_log"
