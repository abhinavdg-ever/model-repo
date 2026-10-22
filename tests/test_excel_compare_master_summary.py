"""What Manual Review took from the standalone ExcelCompare tool: UNVALIDATED,
unique chart counts, the field-wise breakdown with its evidence, compared
Type (Printed/HW), and the Master Summary PDF -- read against the QA sheet's
own headers."""

import io

import pytest

pd = pytest.importorskip("pandas")

from app.services.excel_compare import _summary
from app.services.excel_compare.compare import compare_files
from app.services.excel_compare.field_comparator import compare_fields
from app.services.excel_compare.record_matcher import unique_pdf_ids
from app.services.excel_compare.result_builder import build_summary

# The manual QA sheet as it arrives: extra ID columns, trailing spaces in headers.
QA_HEADERS = [
    "DocumentID", "RecordID", "PDF ID", "Page Number", "Member Name ", "Member DOB ",
    "Member ID ", "Type (Printed/HW)", "Visibility", "Tilt(y/n)", "DOS from", "DOS To",
    "Is Blank or Junk?", "Duplicate", "Page Type ", "Page Subtype ",
    "Codable Or Non Codable ", "Current Sequence ", "Actual Sequence ",
]
QA_ROWS = [
    ["55295235", "66944857", "66944857_55295235", 1, "Anderson Lisa", "4/5/1993", "204568", "Printed",
     "Visible", "N", "1/28/2025", "1/28/2025", "No", "No", "Progress Note", "", "Codable", 1, 1],
    ["55295235", "66944857", "66944857_55295235", 2, "Anderson Lisa", "4/5/1993", "1050824", "Printed",
     "Visible", "N", "1/28/2025", "1/28/2025", "No", "No", "Progress Note", "", "Codable", 2, 2],
    ["55295235", "66944857", "66944857_55295235", 3, "Anderson Lisa", "4/5/1993", "1050824", "HW",
     "Visible", "N", "1/28/2025", "1/28/2025", "No", "No", "Progress Note", "", "Codable", 3, 3],
]


def _upload(frame, name):
    buffer = io.BytesIO(frame.to_csv(index=False).encode())
    buffer.name = name
    return buffer


def _qa_sheet():
    return pd.DataFrame(QA_ROWS, columns=QA_HEADERS)


def _algo_sheet():
    return pd.DataFrame(
        [
            {"PDF ID": "66944857_55295235", "Page Number": page, "Extracted Name": "Anderson Lisa",
             "Extracted DOB": "1993-04-05", "Member ID": member_id, "Type (Printed/HW)": kind,
             "DOS From": "2025-01-28", "DOS To": dos_to, "Is Blank or Junk?": "No", "Duplicate": "No",
             "Page Type": "Progress Note", "Page Subtype": "", "Codable / Non-Codable": "Codable",
             "Current Sequence": page, "Actual Sequence": page}
            for page, member_id, kind, dos_to in [
                (1, "204568", "Printed", "2025-01-28"),
                (2, "1050824", "Printed", "2025-01-29"),
                (3, "1050824", "Handwritten", "2025-01-28"),
            ]
        ]
    )


def test_the_qa_sheet_headers_are_all_read():
    result = compare_files(_upload(_qa_sheet(), "qa.csv"), _upload(_algo_sheet(), "algo.csv"))
    # Every compared field the sheet has; Provider Credentials is the only one it lacks.
    assert result["active_comparison_fields"] == [
        "Extracted Name", "Extracted DOB", "Member ID", "Type (Printed/HW)", "DOS From", "DOS To",
        "Is Blank or Junk?", "Duplicate", "Page Type", "Page Subtype", "Codable / Non-Codable",
        "Current Sequence", "Actual Sequence",
    ]
    exempt = {entry["column"] for entry in result["exempted_columns"]}
    assert {"Provider Credentials", "Visibility", "Tilt"} <= exempt


def test_qa_sheet_totals_breakdown_and_evidence():
    result = compare_files(_upload(_qa_sheet(), "qa.csv"), _upload(_algo_sheet(), "algo.csv"))
    statuses = [row["Status"] for row in result["result_rows"]]
    # Page 3's HW is the algo's Handwritten; only page 2's DOS To disagrees.
    assert statuses == ["CORRECT", "ERROR", "CORRECT"]
    assert (result["matched_count"], result["unvalidated_count"]) == (3, 0)
    assert result["exact_match_rate"] == pytest.approx(66.67)
    assert result["overall_discrepancy_rate"] == pytest.approx(33.33)
    assert result["unique_discrepancy_count"] == 1
    assert result["field_error_summary"][0] == {"field": "DOS To", "error_count": 1, "error_percentage": 33.33}
    assert result["mismatch_evidence"] == [{
        "pdf_id": "66944857_55295235", "page_number": 2, "field": "DOS To",
        "ground_truth_value": "01/28/2025", "system_value": "01/29/2025",
    }]
    assert (result["total_unique_ground_charts"], result["total_unique_system_charts"],
            result["total_unique_charts_compared"]) == (1, 1, 1)


def test_printed_and_handwritten_spellings_compare_equal():
    def match(gt, algo):
        field = "Type (Printed/HW)"
        return compare_fields({field: gt}, {field: algo}, [field])[field]["match"]

    assert match("HW", "Handwritten") is True
    assert match("Printed", "printed") is True
    assert match("Printed", "HW") is False
    assert match("Mixed", "Handwritten") is False


def test_legacy_yes_no_member_headers_read_as_presence():
    ground = pd.DataFrame([{"PDF ID": "1", "Page Number": 1, "Member Name (Yes/No)": "Yes",
                            "Member DOB (Yes/No)": "No"}])
    algo = pd.DataFrame([{"PDF ID": "1", "Page Number": 1, "Extracted Name": "Lisa Anderson",
                          "Extracted DOB": ""}])
    result = compare_files(_upload(ground, "g.csv"), _upload(algo, "a.csv"))
    assert result["active_comparison_fields"] == ["Extracted Name", "Extracted DOB"]
    assert result["result_rows"][0]["Status"] == "CORRECT"


def test_unique_pdf_ids_count_charts_not_pages():
    frame = pd.DataFrame({"PDF ID": ["A", "A", " A ", "B", None, ""], "Page Number": [1, 2, 3, 1, 1, 1]})
    assert unique_pdf_ids(frame) == {"A", "B"}


def test_summary_says_na_when_nothing_was_compared():
    rows = [{"Status": "UNVALIDATED"}]
    summary = build_summary(1, 1, rows, fields_were_compared=False, total_unique_ground_charts=1)
    by_metric = {row["Metric"]: row for row in summary.to_dict("records")}
    assert by_metric["Match Percentage"]["Percentage"] == "N/A"
    assert by_metric["Unvalidated Records"]["Count"] == 1
    assert by_metric["Total Unique Ground Truth Charts"]["Percentage"] == "-"
    # The screen gets null, not a crash, for both placeholders.
    as_json = {row["metric"]: row["percentage"] for row in _summary(summary)}
    assert as_json["Match Percentage"] is None
    assert as_json["Total Unique Ground Truth Charts"] is None


def _pdf_text(data):
    pypdf = pytest.importorskip("pypdf")
    return "\n".join(page.extract_text() for page in pypdf.PdfReader(io.BytesIO(data)).pages)


def test_master_summary_pdf_reconciles_with_the_comparison():
    pytest.importorskip("reportlab")
    from app.services.excel_compare.master_summary import build_master_summary_pdf

    result = compare_files(_upload(_qa_sheet(), "qa.csv"), _upload(_algo_sheet(), "algo.csv"))
    data = build_master_summary_pdf(result, ground_label="qa & review.csv")
    assert data[:4] == b"%PDF"
    text = _pdf_text(data)
    for heading in ("1. Overall Comparison Summary", "2. Chart-wise Summary",
                    "3. Field-wise Accuracy", "4. Top Mismatch Patterns",
                    "5. Missing / Extra Pages", "6. Columns Not Compared",
                    "Appendix: Every Field Mismatch"):
        assert heading in text
    assert "qa & review.csv" in text  # escaped, not parsed as markup
    assert "01/29/2025" in text
    # The QA sheet's ID columns are named as unread, so a misspelt header shows.
    assert "DocumentID, RecordID" in text
    assert "Page 1 of" in text


def test_patterns_group_identical_disagreements_with_page_ranges():
    from app.services.excel_compare.master_summary import _mismatch_patterns, _pages_by_chart

    evidence = [
        {"pdf_id": "A", "page_number": page, "field": "Page Type",
         "ground_truth_value": "ED Note", "system_value": "Progress note"}
        for page in (3, 4, 5, 9)
    ] + [
        {"pdf_id": "B", "page_number": 2, "field": "Page Type",
         "ground_truth_value": "ED Note", "system_value": "Progress note"},
        {"pdf_id": "A", "page_number": 7, "field": "DOS From",
         "ground_truth_value": "01/02/2025", "system_value": ""},
    ]
    top, other = _mismatch_patterns(evidence)
    assert (top["field"], top["gt"], top["algo"], top["count"]) == ("Page Type", "ED Note", "Progress note", 5)
    assert _pages_by_chart(top["pages"]) == "A: 3–5, 9; B: 2"
    assert _pages_by_chart(other["pages"], single_chart=True) == "7"


def test_missing_pages_read_as_ranges():
    from app.services.excel_compare.master_summary import _page_ranges

    assert _page_ranges([71, 1, 2, 3, 4, 5, 6]) == "1–6, 71"
    assert _page_ranges(["2", 3.0, 10]) == "2–3, 10"


def test_algo_dash_reads_as_nothing_found_for_a_no_label():
    def match(gt, algo):
        return compare_fields({"Extracted DOB": gt}, {"Extracted DOB": algo}, ["Extracted DOB"])[
            "Extracted DOB"
        ]["match"]

    assert match("No", "-") is True
    assert match("No", "N/A") is True
    assert match("Yes", "-") is False
    assert match("Yes", "04/05/1993") is True


def test_legacy_provider_signature_credentials_are_compared():
    ground = pd.DataFrame([
        {"PDF ID": "1", "Page Number": 1, "Provider Signature": "MD"},
        {"PDF ID": "1", "Page Number": 2, "Provider Signature": "DO"},
    ])
    algo = pd.DataFrame([
        {"PDF ID": "1", "Page Number": 1, "Provider Signature": "-"},
        {"PDF ID": "1", "Page Number": 2, "Provider Signature": "NP,DO"},
    ])
    result = compare_files(_upload(ground, "g.csv"), _upload(algo, "a.csv"))
    assert result["active_comparison_fields"] == ["Provider Credentials"]
    assert [row["Status"] for row in result["result_rows"]] == ["ERROR", "CORRECT"]


def test_yes_no_provider_signature_is_not_read_as_credentials():
    ground = pd.DataFrame([{"PDF ID": "1", "Page Number": 1, "Provider Signature": "Yes"}])
    result = compare_files(_upload(ground, "g.csv"), _upload(ground, "a.csv"))
    assert "Provider Credentials" not in result["active_comparison_fields"]


def test_master_summary_survives_markup_in_values():
    pytest.importorskip("reportlab")
    from app.services.excel_compare.master_summary import build_master_summary_pdf

    ground = pd.DataFrame([{"PDF ID": "1", "Page Number": 1, "Page Type": "Lab <A&B>"}])
    algo = pd.DataFrame([{"PDF ID": "1", "Page Number": 1, "Page Type": "Lab"}])
    result = compare_files(_upload(ground, "g.csv"), _upload(algo, "a.csv"))
    assert "Lab <A&B>" in _pdf_text(build_master_summary_pdf(result))


def test_summary_endpoint_returns_the_pdf():
    pytest.importorskip("reportlab")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    files = {
        "ground_file": ("qa.csv", io.BytesIO(_qa_sheet().to_csv(index=False).encode()), "text/csv"),
        "system_file": ("algo.csv", io.BytesIO(_algo_sheet().to_csv(index=False).encode()), "text/csv"),
    }
    res = TestClient(app).post("/api/excel-compare/summary", files=files, data={"system_source": "upload"})
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/pdf"
    assert res.content[:4] == b"%PDF"


def test_algo_type_is_the_chart_views_document_type():
    import csv

    from app.core.schemas import ImagingDocumentResponse, ImagingPageResult
    from app.services.excel_compare.database_system import system_csv

    pages = [
        ImagingPageResult(pageNumber=1, fileName="1.png", documentType="Visual",
                          handwrittenOrPrinted="Printed"),
        # A page classified before document type existed: its printed / handwritten label.
        ImagingPageResult(pageNumber=2, fileName="2.png", handwrittenOrPrinted="Handwritten"),
        ImagingPageResult(pageNumber=3, fileName="3.png"),
    ]
    doc = ImagingDocumentResponse.model_construct(pages=pages, verifications=[], verification=None)
    rows = list(csv.DictReader(io.StringIO(system_csv([("100", doc)]).decode())))
    assert [row["Type (Printed/HW)"] for row in rows] == ["Visual", "Handwritten", ""]


def test_qa_type_against_the_algo_document_type():
    ground = pd.DataFrame([{"PDF ID": "1", "Page Number": n, "Type (Printed/HW)": t}
                           for n, t in [(1, "Printed"), (2, "HW"), (3, "Printed")]])
    algo = pd.DataFrame([{"PDF ID": "1", "Page Number": n, "Type (Printed/HW)": t}
                         for n, t in [(1, "Printed"), (2, "Handwritten"), (3, "Visual")]])
    result = compare_files(_upload(ground, "g.csv"), _upload(algo, "a.csv"))
    assert [row["Status"] for row in result["result_rows"]] == ["CORRECT", "CORRECT", "ERROR"]


def test_database_ground_truth_leaves_type_out():
    from app.services.excel_compare.database_ground import ground_csv

    data = ground_csv([("100", 1, {"printed_or_handwritten": "Printed", "dos_from": "2025-01-28"})])
    header = data.decode().splitlines()[0]
    # Type is compared against an uploaded sheet only; database labels skip it.
    assert "Type (Printed/HW)" not in header


def test_database_algo_row_carries_document_type():
    import re

    from app.services.excel_compare import database_algo

    select = database_algo._PAGES.split("SELECT", 1)[1].split("\n  FROM", 1)[0]
    # Top-level commas only (COALESCE / NULLIF / regexp_replace hold their own).
    depth, columns = 0, 1
    for char in re.sub(r"'[^']*'", "''", select):
        depth += char == "("
        depth -= char == ")"
        columns += char == "," and depth == 0
    row = [None] * columns
    row[0], row[1], row[2] = "100", 4, "4.png"
    row[-3], row[-2] = "visual", "printed"
    page = database_algo._page(tuple(row))
    assert (page.documentType, page.handwrittenOrPrinted) == ("Visual", "Printed")
    row[-3], row[-2] = None, "handwritten"
    page = database_algo._page(tuple(row))
    assert (page.documentType, page.handwrittenOrPrinted) == (None, "Handwritten")
    # The last column: this Main page names the page it nearly copies (Partial).
    row[8], row[-1] = "not_blank_junk", True
    assert database_algo._page(tuple(row)).isPartialDuplicate is True
    row[8], row[-1] = "duplicate", True
    page = database_algo._page(tuple(row))
    assert (page.isDuplicate, page.isPartialDuplicate, page.blankOrJunk) == (True, False, "Yes (Duplicate)")


def test_names_match_in_any_order_and_punctuation():
    def compare(gt, algo):
        return compare_fields({"Extracted Name": gt}, {"Extracted Name": algo}, ["Extracted Name"])[
            "Extracted Name"
        ]

    for algo in ("Lisa Anderson", "Lisa, Anderson", "ANDERSON, LISA", "anderson  lisa"):
        assert compare("Anderson Lisa", algo)["match"] is True, algo
    # A matching name is one value, normalized: no commas, Title Case.
    assert compare("Anderson, Lisa", "lisa anderson")["display"] == "Anderson Lisa"
    assert compare("Anderson Lisa", "Anderson Lisa")["display"] == "Anderson Lisa"
    # A mismatch shows both sides, each normalized the same way.
    assert compare("ANDERSON, LISA", "Laura, Anderson")["display"] == "GT: Anderson Lisa | Algo: Laura Anderson"
    assert compare("Anderson Lisa", "Lisa M Anderson")["match"] is False
    assert compare("Anderson Lisa", "Laura Anderson")["match"] is False


def test_float_read_member_ids_match():
    def compare(gt, algo):
        return compare_fields({"Member ID": gt}, {"Member ID": algo}, ["Member ID"])["Member ID"]

    assert compare("204568", "204568.0") == {
        "match": True, "display": "204568", "gt_raw": "204568", "system_raw": "204568",
    }
    assert compare(204568.0, "204568")["match"] is True
    assert compare("00204568", "204568")["match"] is False  # leading zeros are part of the ID
    assert compare("204568.5", "204568")["match"] is False


def test_float_read_ids_from_a_sheet_with_blanks():
    # A blank Member ID makes pandas read the column as floats.
    ground = pd.DataFrame([{"PDF ID": "1", "Page Number": 1, "Member ID": 204568, "Current Sequence": 1},
                           {"PDF ID": "1", "Page Number": 2, "Member ID": None, "Current Sequence": None}])
    algo = pd.DataFrame([{"PDF ID": "1", "Page Number": 1, "Member ID": "204568", "Current Sequence": "1"},
                         {"PDF ID": "1", "Page Number": 2, "Member ID": "", "Current Sequence": ""}])
    result = compare_files(_upload(ground, "g.csv"), _upload(algo, "a.csv"))
    assert [row["Status"] for row in result["result_rows"]] == ["CORRECT", "CORRECT"]
    assert result["result_rows"][0]["Member ID"] == "204568"


def _api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def _qa_files():
    return {
        "ground_file": ("qa.csv", io.BytesIO(_qa_sheet().to_csv(index=False).encode()), "text/csv"),
        "system_file": ("algo.csv", io.BytesIO(_algo_sheet().to_csv(index=False).encode()), "text/csv"),
    }


def test_downloads_build_from_the_comparison_on_screen():
    import json

    pytest.importorskip("reportlab")
    pytest.importorskip("httpx")
    client = _api_client()
    upload = {"system_source": "upload"}

    streamed = client.post("/api/excel-compare/stream", files=_qa_files(), data=upload)
    done = [json.loads(line) for line in streamed.text.splitlines() if line.strip()][-1]
    whole = client.post("/api/excel-compare", files=_qa_files(), data=upload).json()

    for report_id in (done["reportId"], whole["reportId"]):
        # No inputs at all: only the kept comparison can answer.
        for path, kind in (("/api/excel-compare/report", b"PK"), ("/api/excel-compare/summary", b"%PDF")):
            res = client.post(path, data={**upload, "report_id": report_id})
            assert res.status_code == 200, res.text
            assert res.content[: len(kind)] == kind


def test_a_dropped_report_id_compares_again():
    pytest.importorskip("reportlab")
    pytest.importorskip("httpx")
    res = _api_client().post(
        "/api/excel-compare/summary", files=_qa_files(),
        data={"system_source": "upload", "report_id": "no-longer-kept"},
    )
    assert res.status_code == 200
    assert res.content[:4] == b"%PDF"


def test_only_the_last_few_comparisons_are_kept():
    import app.services.excel_compare as excel_compare

    ids = [excel_compare._remember({"n": n}) for n in range(excel_compare.KEEP_RESULTS + 2)]
    assert excel_compare.remembered(ids[0]) is None
    assert excel_compare.remembered(ids[-1]) == {"n": excel_compare.KEEP_RESULTS + 1}
    assert excel_compare.remembered(None) is None


def test_a_pattern_across_many_charts_still_builds():
    pytest.importorskip("reportlab")
    from app.services.excel_compare.master_summary import (
        MAX_PATTERN_CHARTS,
        _pages_by_chart,
        build_master_summary_pdf,
    )

    # The same disagreement on every page of 400 charts: its Where cell would
    # outgrow a page (a LayoutError) if every chart were listed.
    rows = [{"PDF ID": f"C{c:04d}", "Page Number": p, "Page Type": "ED Note"}
            for c in range(400) for p in range(1, 4)]
    algo = [dict(row, **{"Page Type": "Progress note"}) for row in rows]
    result = compare_files(_upload(pd.DataFrame(rows), "g.csv"), _upload(pd.DataFrame(algo), "a.csv"))
    text = _pdf_text(build_master_summary_pdf(result))
    assert f"and {400 - MAX_PATTERN_CHARTS} more charts" in text
    assert "The first 1500 of 1200" not in text  # 1200 mismatches: the appendix lists them all
    entries = [("A", 1), ("B", 2), ("C", 3)]
    assert _pages_by_chart(entries, max_charts=2) == "A: 1; B: 2 … and 1 more charts"


def test_display_name_normalizes_without_losing_labels():
    from app.services.excel_compare.normalizer import display_name

    assert display_name("ANDERSON,  LISA") == "Anderson Lisa"
    assert display_name("o'brien, mary-jo") == "O'Brien Mary-Jo"
    assert display_name("Lisa M. Anderson") == "Lisa M Anderson"
    # "No name" labels and presence answers are kept as written.
    assert [display_name(v) for v in ("-", "N/A", "Yes", "")] == ["-", "N/A", "Yes", ""]


def test_missing_rows_show_the_normalized_name():
    ground = pd.DataFrame([{"PDF ID": "1", "Page Number": 1, "Extracted Name": "ANDERSON, LISA"},
                           {"PDF ID": "1", "Page Number": 2, "Extracted Name": "ANDERSON, LISA"}])
    algo = pd.DataFrame([{"PDF ID": "1", "Page Number": 1, "Extracted Name": "Lisa Anderson"}])
    rows = compare_files(_upload(ground, "g.csv"), _upload(algo, "a.csv"))["result_rows"]
    assert [(row["Status"], row["Extracted Name"]) for row in rows] == [
        ("CORRECT", "Anderson Lisa"), ("MISSING", "Anderson Lisa"),
    ]


def test_a_duplicate_matches_a_page_labelled_junk():
    def match(field, gt, algo):
        return compare_fields({field: gt}, {field: algo}, [field])[field]["match"]

    for gt in ("Yes (Junk)", "Yes", "yes (blank)"):
        assert match("Is Blank or Junk?", gt, "Yes (Duplicate)") is True, gt
    assert match("Is Blank or Junk?", "No", "Yes (Duplicate)") is False
    # Duplicate: Yes / Partial / No, and the screen's "Yes (To Page 4)" is a Yes.
    assert match("Duplicate", "Yes", "Yes (To Page 4)") is True
    assert match("Duplicate", "Partial", "Partial") is True
    assert match("Duplicate", "Yes", "Partial") is False


def test_algo_duplicate_reads_yes_partial_no():
    import csv

    from app.core.schemas import ImagingDocumentResponse, ImagingPageResult
    from app.services.excel_compare.database_system import system_csv

    pages = [
        ImagingPageResult(pageNumber=1, fileName="1.png", isDuplicate=True, blankOrJunk="Yes (Duplicate)"),
        ImagingPageResult(pageNumber=2, fileName="2.png", isDuplicate=False, isPartialDuplicate=True, blankOrJunk="No"),
        ImagingPageResult(pageNumber=3, fileName="3.png", isDuplicate=False, blankOrJunk="No"),
    ]
    doc = ImagingDocumentResponse.model_construct(pages=pages, verifications=[], verification=None)
    rows = list(csv.DictReader(io.StringIO(system_csv([("100", doc)]).decode())))
    assert [(r["Duplicate"], r["Is Blank or Junk?"]) for r in rows] == [
        ("Yes", "Yes"), ("Partial", "No"), ("No", "No"),
    ]


def test_local_mode_reads_duplicates_and_partials_from_the_junk_csv():
    from app.services.imaging_overlays import index_junk_rows

    rows = [
        {"chart_name": "c", "page_name": "1.jpg", "page_number": "1", "page_classification": "Main",
         "page_group": "main", "page_classification_confidence": "0.7", "duplicate_of_page": ""},
        {"chart_name": "c", "page_name": "2.jpg", "page_number": "2", "page_classification": "Duplicate",
         "page_group": "duplicate", "page_classification_confidence": "0.99", "duplicate_of_page": "1.jpg"},
        {"chart_name": "c", "page_name": "3.jpg", "page_number": "3", "page_classification": "Main",
         "page_group": "main", "page_classification_confidence": "0.96", "duplicate_of_page": "1.jpg"},
    ]
    by = index_junk_rows(rows, "c")
    assert (by["#2"]["blankOrJunk"], by["#2"]["isDuplicate"], by["#2"]["isPartialDuplicate"]) == (
        "Yes (Duplicate)", True, False,
    )
    assert (by["#3"]["blankOrJunk"], by["#3"]["isDuplicate"], by["#3"]["isPartialDuplicate"]) == (
        "No", False, True,
    )
    assert by["#3"]["duplicateOf"] == "1.jpg"
    assert by["#1"]["isPartialDuplicate"] is False
