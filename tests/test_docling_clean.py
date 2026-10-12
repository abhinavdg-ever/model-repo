"""Unit tests for Docling markdown cleanup."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def test_strips_image_placeholders_and_empty_tables():
    from stages.lib.ocr.docling_ocr import clean_docling_markdown

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
    from stages.lib.ocr.section_header_match import best_header_match

    score, label = best_header_match("PATIENT DATA", use_minilm=False)
    assert score >= 0.90
    assert "Patient" in label or "patient" in label.lower()


def test_export_keeps_the_page_header_and_text_inside_pictures():
    """Docling's default export drops the furniture layer (the page-header
    banner with the encounter date) and text inside a picture region."""
    import pytest

    pytest.importorskip("docling_core")
    from docling_core.types.doc import DocItemLabel, DoclingDocument
    from docling_core.types.doc.document import ContentLayer

    from stages.lib.ocr.docling_ocr import export_markdown

    doc = DoclingDocument(name="page")
    doc.add_text(label=DocItemLabel.PAGE_HEADER, text="Encounter Date: December 03, 2025",
                 content_layer=ContentLayer.FURNITURE)
    picture = doc.add_picture()
    doc.add_text(label=DocItemLabel.TEXT, text="Date of birth 04/05/1993", parent=picture)
    doc.add_text(label=DocItemLabel.SECTION_HEADER, text="Reason for Visit")

    out = export_markdown(doc)
    assert out.index("Encounter Date") < out.index("Reason for Visit")
    assert "Date of birth 04/05/1993" in out


def test_export_falls_back_on_an_old_docling_core():
    from stages.lib.ocr.docling_ocr import export_markdown

    class OldDoc:
        def export_to_markdown(self, **kwargs):
            if kwargs:
                raise TypeError("unexpected keyword")
            return "body only"

    assert export_markdown(OldDoc()) == "body only"
