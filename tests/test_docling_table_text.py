"""2026-11-06: a dense page Docling reads as one table (cell matching off) keeps its text from
the OCR lines Docling read (core-pipeline/modules/ocr/docling_ocr.py)."""
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.ocr.docling_ocr import markdown_lost_text, ocr_lines_text  # noqa: E402


def test_lines_are_grouped_into_rows_left_to_right():
    cells = [
        (300, 102, 400, 118, "08/02/2026 5:20 AM"),
        (10, 100, 90, 120, "Height"),
        (120, 101, 200, 119, "70.44"),
        (10, 140, 90, 160, "Weight"),
    ]
    assert ocr_lines_text(cells) == "Height  70.44  08/02/2026 5:20 AM\nWeight"


def test_a_table_lost_to_empty_cells_falls_back_to_the_ocr():
    ocr = "\n".join(f"question{i} answer{i} author{i}" for i in range(20))
    assert markdown_lost_text("Assessment Group:\n\nPeterson, Harold\n\n3", ocr)
    assert not markdown_lost_text(ocr, ocr)


def test_a_short_page_is_left_alone():
    assert not markdown_lost_text("", "Signature page only")


def test_empty_table_cells_are_filled_and_the_table_kept():
    import pytest

    pytest.importorskip("docling_core")
    from docling_core.types.doc.base import BoundingBox, CoordOrigin
    from docling_core.types.doc.document import DoclingDocument, TableCell, TableData

    from modules.ocr.docling_ocr import clean_docling_markdown, export_markdown, fill_empty_table_cells

    doc = DoclingDocument(name="page")
    cells = [
        TableCell(text="", bbox=BoundingBox(l=c * 100, t=r * 20, r=c * 100 + 100, b=r * 20 + 20,
                                            coord_origin=CoordOrigin.TOPLEFT),
                  start_row_offset_idx=r, end_row_offset_idx=r + 1, start_col_offset_idx=c, end_col_offset_idx=c + 1)
        for r in range(2) for c in range(2)
    ]
    doc.add_table(data=TableData(num_rows=2, num_cols=2, table_cells=cells))
    assert clean_docling_markdown(export_markdown(doc)) == ""
    ocr = [(5, 2, 60, 18, "Question"), (105, 2, 160, 18, "Answer"), (5, 22, 60, 38, "Height"), (105, 22, 160, 38, "70.44")]
    assert fill_empty_table_cells(doc, ocr, 1000) == 4
    table = clean_docling_markdown(export_markdown(doc))
    assert table.startswith("|") and "Height" in table and "70.44" in table
