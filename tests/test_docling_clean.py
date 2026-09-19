"""Unit tests for Docling markdown cleanup."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def test_strips_image_placeholders_and_empty_tables():
    from stages.lib.imaging.docling_ocr import clean_docling_markdown

    raw = (
        "<!-- image -->\n\n<!-- image -->\n\n"
        "Date (Printed): 5/22/2025\n\n\n\n"
        "PATIENT DATA\n"
        "| --- | --- |\n"
        "|  |  |\n"
        "EMERGENCY CONTACT\n"
    )
    out = clean_docling_markdown(raw)
    assert "<!-- image -->" not in out
    assert "[image]" not in out
    assert "| --- |" not in out
    assert "PATIENT DATA" in out
    assert "EMERGENCY CONTACT" in out
    assert "Date (Printed)" in out
    # No triple blank runs
    assert "\n\n\n" not in out


def test_patient_data_matches_canon():
    from stages.lib.imaging.section_header_match import best_header_match

    score, label = best_header_match("PATIENT DATA")
    assert score >= 0.90
    assert "Patient" in label or "patient" in label.lower()
