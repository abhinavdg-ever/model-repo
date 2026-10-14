import tempfile
from pathlib import Path

import pandas as pd

from app.services.excel_compare.field_comparator import ACTIVE_COMPARISON_FIELDS
from app.services.excel_compare.result_builder import (
    build_associated_result,
    build_missing_result,
    build_extra_result,
    build_summary,
    build_exempted_columns_df,
    write_report,
)


def make_field_results(fields, all_match=True):

    field_results = {}

    for field_name in fields:

        if all_match:
            field_results[field_name] = {
                "match": True,
                "display": "Yes",
            }
        else:
            field_results[field_name] = {
                "match": False,
                "display": "GT: Yes | Algo: No",
            }

    return field_results


def test_build_associated_result_shape():

    field_results = make_field_results(ACTIVE_COMPARISON_FIELDS, all_match=True)

    row = build_associated_result(
        "123456", 1, field_results, "CORRECT", "All fields match.",
        ACTIVE_COMPARISON_FIELDS,
    )

    assert row["PDF ID"] == "123456"
    assert row["Page Number"] == 1
    assert row["Status"] == "CORRECT"
    assert row["Reason"] == "All fields match."

    for field_name in ACTIVE_COMPARISON_FIELDS:
        assert row[field_name] == "Yes"


def test_build_associated_result_only_includes_given_fields():

    fields = ["Member ID", "Page Type"]
    field_results = make_field_results(fields, all_match=True)

    row = build_associated_result(
        "123456", 1, field_results, "CORRECT", "All fields match.", fields,
    )

    assert set(row.keys()) == {"PDF ID", "Page Number", "Status", "Reason"} | set(fields)


def test_build_missing_result_uses_gt_values():

    gt_row = pd.Series({
        "PDF ID": "123456",
        "Page Number": 2,
        "Extracted Name": "Jane Doe",
        "Extracted DOB": "1990-01-01",
        "Member ID": "M1",
        "Type (Printed/HW)": "Printed",
        "DOS From": "2024-07-25",
        "DOS To": "2024-07-25",
        "Is Blank or Junk?": "No",
        "Duplicate": "No",
        "Page Type": "Medical Record",
        "Page Subtype": "Progress Note",
        "Codable / Non-Codable": "Codable",
        "Current Sequence": "1",
        "Actual Sequence": "1",
    })

    row = build_missing_result(gt_row, ACTIVE_COMPARISON_FIELDS)

    assert row["PDF ID"] == "123456"
    assert row["Page Number"] == "2"
    assert row["Status"] == "MISSING"
    assert "No corresponding Algo Generated record" in row["Reason"]
    assert row["Extracted Name"] == "Jane Doe"


def test_build_extra_result_uses_system_values():

    system_row = pd.Series({
        "PDF ID": "123456",
        "Page Number": 3,
        "Extracted Name": "John Smith",
        "Extracted DOB": "1985-05-05",
        "Member ID": "M2",
        "Type (Printed/HW)": "Handwritten",
        "DOS From": "2024-07-25",
        "DOS To": "2024-07-25",
        "Is Blank or Junk?": "Yes",
        "Duplicate": "No",
        "Page Type": "Billing Record",
        "Page Subtype": "Invoice",
        "Codable / Non-Codable": "Non Codable",
        "Current Sequence": "2",
        "Actual Sequence": "2",
    })

    row = build_extra_result(system_row, ACTIVE_COMPARISON_FIELDS)

    assert row["PDF ID"] == "123456"
    assert row["Page Number"] == "3"
    assert row["Status"] == "EXTRA"
    assert "No corresponding Ground Truth record" in row["Reason"]
    assert row["Page Type"] == "Billing Record"


def test_build_summary_metrics():

    result_rows = [
        {"Status": "CORRECT"},
        {"Status": "CORRECT"},
        {"Status": "ERROR"},
        {"Status": "MISSING"},
        {"Status": "EXTRA"},
    ]

    # ground_count = correct + error + missing = 4
    # system_count = correct + error + extra = 4
    summary_df = build_summary(4, 4, result_rows)

    metrics = dict(zip(summary_df["Metric"], summary_df["Count"]))
    percentages = dict(zip(summary_df["Metric"], summary_df["Percentage"]))

    assert metrics["Total Ground Truth Records"] == 4
    assert metrics["Total Algo Records"] == 4
    assert metrics["Correct Records"] == 2
    assert metrics["Errors"] == 1
    assert metrics["Missing Records"] == 1
    assert metrics["Extra Records"] == 1

    assert percentages["Match Percentage"] == 50.0
    assert percentages["Overall Disparity Rate"] == 50.0
    assert percentages["Extra Records"] == 25.0


def test_build_exempted_columns_df_lists_name_and_reason():

    exempted_columns = [
        {"column": "Tilt", "reason": "Subjective field; comparison would not produce meaningful insights."},
        {"column": "Member ID", "reason": "Not present in both files."},
    ]

    df = build_exempted_columns_df(exempted_columns)

    assert list(df.columns) == ["Column", "Reason"]
    assert set(df["Column"]) == {"Tilt", "Member ID"}


def test_build_exempted_columns_df_empty_when_nothing_exempted():

    df = build_exempted_columns_df([])

    assert list(df.columns) == ["Column", "Reason"]
    assert len(df) == 0


def test_write_report_round_trip():

    fields = ACTIVE_COMPARISON_FIELDS
    field_results = make_field_results(fields, all_match=True)

    result_rows = [
        build_associated_result(
            "123456", 1, field_results, "CORRECT", "All fields match.", fields,
        ),
    ]

    summary_df = build_summary(1, 1, result_rows)
    exempted_columns = [
        {"column": "Tilt", "reason": "Subjective field; comparison would not produce meaningful insights."},
    ]

    with tempfile.TemporaryDirectory() as tmp_dir:

        output_path = Path(tmp_dir) / "report.xlsx"

        write_report(output_path, result_rows, summary_df, fields, exempted_columns)

        written = pd.read_excel(output_path, sheet_name="Comparison Results")
        written_summary = pd.read_excel(output_path, sheet_name="Summary")
        written_exempted = pd.read_excel(output_path, sheet_name="Exempted Columns")

        expected_columns = (
            ["PDF ID", "Page Number"] + fields + ["Status", "Reason"]
        )

        assert list(written.columns) == expected_columns
        assert "Match Percentage" in set(written_summary["Metric"])
        assert "Tilt" in set(written_exempted["Column"])


def test_write_report_defaults_to_active_comparison_fields():

    fields = ACTIVE_COMPARISON_FIELDS
    field_results = make_field_results(fields, all_match=True)

    result_rows = [
        build_associated_result(
            "123456", 1, field_results, "CORRECT", "All fields match.", fields,
        ),
    ]

    summary_df = build_summary(1, 1, result_rows)

    with tempfile.TemporaryDirectory() as tmp_dir:

        output_path = Path(tmp_dir) / "report.xlsx"

        write_report(output_path, result_rows, summary_df)

        written = pd.read_excel(output_path, sheet_name="Comparison Results")

        expected_columns = (
            ["PDF ID", "Page Number"] + fields + ["Status", "Reason"]
        )

        assert list(written.columns) == expected_columns
