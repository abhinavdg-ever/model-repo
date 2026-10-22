import pandas as pd

from app.services.excel_compare.field_comparator import (
    ACTIVE_COMPARISON_FIELDS,
    ALWAYS_EXEMPT_FIELDS,
    ALWAYS_EXEMPT_REASON,
)
from app.services.excel_compare.compare import (
    compare_files,
    NOT_PRESENT_IN_BOTH_REASON,
)


COMMON_FIELDS = {
    "Extracted Name": "John Doe",
    "Extracted DOB": "1990-01-01",
    "Member ID": "M12345",
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
}


class FakeUploadedFile:
    """
    Minimal stand-in for a Streamlit UploadedFile backed by an
    in-memory Excel buffer, used so compare_files() is
    exercised through its upload code path too.
    """

    def __init__(self, name, dataframe):

        from io import BytesIO

        self.name = name
        self._buffer = BytesIO()
        dataframe.to_excel(self._buffer, index=False)
        self._buffer.seek(0)

    def __getattr__(self, attribute_name):
        return getattr(self._buffer, attribute_name)


def build_row(pdf_id, page_number, **overrides):

    row = {"PDF ID": pdf_id, "Page Number": page_number}
    row.update(COMMON_FIELDS)
    row.update(overrides)

    return row


def test_full_pipeline_correct_error_missing_extra_and_multi_pdf():

    ground_rows = [
        # PDF 111111, page 1: fully matching -> CORRECT
        build_row("111111", 1),
        # PDF 111111, page 2: one field differs -> ERROR
        build_row("111111", 2),
        # PDF 111111, page 3: no system counterpart -> MISSING
        build_row("111111", 3),
        # PDF 222222, page 1 (second PDF in the same run) -> CORRECT
        build_row("222222", 1),
    ]

    system_rows = [
        build_row("111111", 1),
        build_row("111111", 2, **{"Page Type": "Billing Record"}),
        build_row("222222", 1),
        # PDF 111111, page 4: no ground truth row -> left out (GT is the base)
        build_row("111111", 4),
        # PDF 333333: a chart the ground truth never lists -> left out
        build_row("333333", 1),
    ]

    ground_df = pd.DataFrame(ground_rows)
    system_df = pd.DataFrame(system_rows)

    ground_file = FakeUploadedFile("ground.xlsx", ground_df)
    system_file = FakeUploadedFile("system.xlsx", system_df)

    result = compare_files(ground_file, system_file)

    rows_by_key = {
        (str(row["PDF ID"]), str(row["Page Number"])): row
        for row in result["result_rows"]
    }

    assert rows_by_key[("111111", "1")]["Status"] == "CORRECT"

    error_row = rows_by_key[("111111", "2")]
    assert error_row["Status"] == "ERROR"
    assert "Page Type" in error_row["Reason"]
    assert error_row["Page Type"] == (
        "GT: Medical Record | Algo: Billing Record"
    )

    assert rows_by_key[("111111", "3")]["Status"] == "MISSING"
    # Ground truth is the base: unlabelled algo pages are not compared.
    assert ("111111", "4") not in rows_by_key
    assert ("333333", "1") not in rows_by_key

    # Second PDF ID in the same upload is matched and compared too
    assert rows_by_key[("222222", "1")]["Status"] == "CORRECT"

    assert result["ground_count"] == 4
    assert result["system_count"] == 3
    assert result["correct_count"] == 2
    assert result["error_count"] == 1
    assert result["missing_count"] == 1
    assert result["extra_count"] == 0


def test_exact_key_match_does_not_imply_correctness():
    """
    A row can be matched with total confidence (identical PDF ID
    and Page Number) and still be reported as ERROR if the
    compared field values disagree. Matching must never be used
    as a proxy for correctness.
    """

    ground_df = pd.DataFrame([
        build_row("999999", 1, **{"Duplicate": "Yes"}),
    ])

    system_df = pd.DataFrame([
        build_row("999999", 1, **{"Duplicate": "No"}),
    ])

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp_dir:

        ground_path = Path(tmp_dir) / "ground.xlsx"
        system_path = Path(tmp_dir) / "system.xlsx"

        ground_df.to_excel(ground_path, index=False)
        system_df.to_excel(system_path, index=False)

        result = compare_files(ground_path, system_path)

    assert len(result["result_rows"]) == 1
    row = result["result_rows"][0]

    assert row["Status"] == "ERROR"
    assert "Duplicate" in row["Reason"]


def test_header_variations_and_value_aliases_are_normalized_end_to_end():
    """
    Real-world files rarely use the exact canonical headers/values.
    Column headers with whitespace/case/punctuation differences or
    known aliases, and field values with equivalent Yes/No or
    Codable spellings, should still validate and compare correctly
    without the required-column check failing.
    """

    ground_df = pd.DataFrame([
        {
            "PDF ID": "555555",
            "Page Number": 1,
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
            "Codable / Non-Codable": "Non Codable",
            "Current Sequence": "1",
            "Actual Sequence": "1",
        },
    ])

    # System file uses header variations/aliases and equivalent
    # value spellings, but the same underlying data.
    system_df = pd.DataFrame([
        {
            "pdf id": "555555",
            "page #": 1,
            "Ext Name": "Jane Doe",
            "DOB": "01/01/1990",
            "Member ID": "M1",
            "Type (Printed/HW)": "Printed",
            "dos from": "07/25/2024",
            "DOS-To": "07/25/2024",
            "Is Blank or Junk": " NO ",
            "Dup": "n",
            "Page Type": "Medical Record",
            "Page Subtype": "Progress Note",
            "Codable/Non Codable": "Non-Codable",
            "Current Seq": "1",
            "Actual Seq": "1",
        },
    ])

    ground_file = FakeUploadedFile("ground.xlsx", ground_df)
    system_file = FakeUploadedFile("system.xlsx", system_df)

    result = compare_files(ground_file, system_file)

    assert len(result["result_rows"]) == 1
    row = result["result_rows"][0]

    assert row["Status"] == "CORRECT"


def test_always_exempt_fields_are_never_compared_or_shown():
    """
    Handwritten %, Visibility, Quality, Orient. (CW), Tilt and
    Mirrored are subjective and always exempted from comparison,
    regardless of whether both files have them.
    """

    ground_df = pd.DataFrame([
        build_row("777777", 1, **{
            "Handwritten %": "80",
            "Visibility": "Clear",
            "Quality": "Good",
            "Orient. (CW)": "0",
            "Tilt": "2",
            "Mirrored": "No",
        }),
    ])

    system_df = pd.DataFrame([
        build_row("777777", 1, **{
            "Handwritten %": "20",
            "Visibility": "Blurry",
            "Quality": "Poor",
            "Orient. (CW)": "90",
            "Tilt": "10",
            "Mirrored": "Yes",
        }),
    ])

    ground_file = FakeUploadedFile("ground.xlsx", ground_df)
    system_file = FakeUploadedFile("system.xlsx", system_df)

    result = compare_files(ground_file, system_file)

    row = result["result_rows"][0]

    # Wildly different values in always-exempt fields must not
    # cause an ERROR, and the fields aren't even in the row.
    assert row["Status"] == "CORRECT"
    for field_name in ALWAYS_EXEMPT_FIELDS:
        assert field_name not in row

    exempted_by_column = {
        entry["column"]: entry["reason"] for entry in result["exempted_columns"]
    }

    for field_name in ALWAYS_EXEMPT_FIELDS:
        assert exempted_by_column[field_name] == ALWAYS_EXEMPT_REASON


def test_column_missing_from_one_file_is_dynamically_exempted():
    """
    If a comparison field exists in only one of the two files,
    it's exempted for this run (not a hard validation error) and
    reported as such -- it must not appear in the per-row table.
    """

    ground_rows = [build_row("888888", 1)]
    system_rows = [build_row("888888", 1)]

    ground_df = pd.DataFrame(ground_rows)
    system_df = pd.DataFrame(system_rows).drop(columns=["Member ID"])

    ground_file = FakeUploadedFile("ground.xlsx", ground_df)
    system_file = FakeUploadedFile("system.xlsx", system_df)

    result = compare_files(ground_file, system_file)

    assert "Member ID" not in result["active_comparison_fields"]

    row = result["result_rows"][0]
    assert "Member ID" not in row

    exempted_by_column = {
        entry["column"]: entry["reason"] for entry in result["exempted_columns"]
    }
    assert exempted_by_column["Member ID"] == NOT_PRESENT_IN_BOTH_REASON

    # Every other active field is still compared and still CORRECT.
    assert row["Status"] == "CORRECT"


def test_file_with_no_comparison_fields_exempts_all_but_still_matches_on_key():
    """
    Comparison fields are no longer a hard requirement -- only
    the PDF ID / Page Number matching key is. A file with none
    of the comparison fields should exempt all of them (reported
    as such) rather than raising, and matching/record counts
    still work off the key alone.
    """

    ground_df = pd.DataFrame([
        {"PDF ID": "123456", "Page Number": 1},
    ])

    system_df = pd.DataFrame([build_row("123456", 1)])

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp_dir:

        ground_path = Path(tmp_dir) / "ground.xlsx"
        system_path = Path(tmp_dir) / "system.xlsx"

        ground_df.to_excel(ground_path, index=False)
        system_df.to_excel(system_path, index=False)

        result = compare_files(ground_path, system_path)

    assert result["active_comparison_fields"] == []
    # Matched on the key alone is not evidence the fields agree.
    assert result["result_rows"][0]["Status"] == "UNVALIDATED"
    assert result["unvalidated_count"] == 1
    assert result["exact_match_rate"] is None

    exempted_columns_names = {
        entry["column"] for entry in result["exempted_columns"]
    }
    for field_name in ACTIVE_COMPARISON_FIELDS:
        assert field_name in exempted_columns_names


def test_missing_pdf_id_column_raises_clear_error():

    ground_df = pd.DataFrame([
        {"Page Number": 1, **COMMON_FIELDS},
    ])

    system_df = pd.DataFrame([build_row("123456", 1)])

    import tempfile
    from pathlib import Path
    import pytest

    with tempfile.TemporaryDirectory() as tmp_dir:

        ground_path = Path(tmp_dir) / "ground.xlsx"
        system_path = Path(tmp_dir) / "system.xlsx"

        ground_df.to_excel(ground_path, index=False)
        system_df.to_excel(system_path, index=False)

        with pytest.raises(ValueError, match="Ground Truth file is missing"):
            compare_files(ground_path, system_path)
