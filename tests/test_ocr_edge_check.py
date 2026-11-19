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
