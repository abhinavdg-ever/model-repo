"""Docling's first / last row against Tesseract's (2026-11-19, user; modules/ocr/edge_check.py)."""
from modules.ocr.edge_check import check, edge_missing

TESS = ("Patient Name: Webb, Paul Date of Birth: 01/31/1978 Sex: F Encounter Date: January 15, 2025\n"
        "Reason for Visit\nModerate persistent asthma\n"
        "Electronically signed by Jane Roe NP on 01/15/2025")
BODY = "\n\nReason for Visit\n\nModerate persistent asthma\n\n"


def test_a_row_run_together_takes_tesseracts_line():
    docling = ("Patient Name:\n\nWebb, PaulDate of Birth:\n\n01/31/1978\n\nSex: F Encounter Date: January 15, 2025"
               + BODY + "Electronically signed by Jane Roe NP on 01/15/2025")
    fixed, edges = check(docling, TESS)
    assert edges == {"top": "spacing_fixed", "bottom": "match"}
    assert fixed.startswith(TESS.splitlines()[0]) and "PaulDate" not in fixed
    assert fixed.endswith("Electronically signed by Jane Roe NP on 01/15/2025")


def test_a_missing_row_is_reported_and_text_kept():
    docling = "Reason for Visit" + BODY + "Electronically signed by Jane Roe NP on 01/15/2025"
    fixed, edges = check(docling, TESS)
    assert edges["top"] == "missing" and edge_missing(edges) and fixed == docling


def test_the_same_row_or_a_letter_misread_is_a_match():
    docling = TESS.replace("Webb", "Wcbb")
    fixed, edges = check(docling, TESS)
    assert edges == {"top": "match", "bottom": "match"} and fixed == docling


def test_a_short_tesseract_row_is_skipped():
    _, edges = check("anything at all here", "Page 1\nsome body text here and more\nok")
    assert edges == {"top": "skipped", "bottom": "skipped"}


def test_reused_docling_text_is_edge_checked_at_routing(monkeypatch):
    """2026-11-20 (user): Docling text from a run before the edge check (Reuse OCR) gets it at the
    routing stage: the first row is fixed and stored."""
    import json
    import modules.ocr.stage_final2 as s2
    import common.db as db

    stored = {}
    monkeypatch.setattr(s2, "upsert_ocr_result", lambda conn, **kw: stored.update(kw))
    monkeypatch.setattr(db, "page_blocks_prelim", lambda row: False)
    raw = json.dumps({"content": "Hill, ErinMRN: NA/ DOB: 04/27/1948 (72 yo)\nPage 9\nTherapist Progress Note"})
    tess = "Hill, Erin MRN: NA/ DOB: 04/27/1948 (72 yo) Page 9\nTherapist Progress Note"
    new = s2._edge_checked(None, 1, 7, raw, tess, {})
    doc = json.loads(new)
    assert doc["edge_check"]["top"] == "spacing_fixed"
    assert doc["content"].splitlines()[0] == "Hill, Erin MRN: NA/ DOB: 04/27/1948 (72 yo) Page 9"
    assert stored["ocr_type"] == "docling" and stored["page_id"] == 7
    assert s2._edge_checked(None, 1, 7, new, tess, {}) is None   # already checked: left alone
