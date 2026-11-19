"""The Accuracy download as an .xlsx (2026-11-19, user): two tabs, percents as numbers."""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review-ui" / "backend"))


def test_two_sheets_and_percent_cells():
    from openpyxl import load_workbook

    from app.routers.accuracy import XlsxSheet, build_xlsx

    data = build_xlsx([
        XlsxSheet(name="Pages", header=["Chart Name", "Page Number", "Page Confidence %"], rows=[["c1", 1, "85%"]]),
        XlsxSheet(name="Charts", header=["Chart Name", "Overall Accuracy"], rows=[["c1", "70%"]]),
    ])
    book = load_workbook(io.BytesIO(data))
    assert book.sheetnames == ["Pages", "Charts"]
    assert book["Pages"]["C2"].value == 0.85 and book["Pages"]["C2"].number_format == "0%"
    assert book["Charts"]["A1"].value == "Chart Name" and book["Charts"].freeze_panes == "A2"
