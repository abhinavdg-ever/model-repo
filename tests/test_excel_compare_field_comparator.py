import pandas as pd

from app.services.excel_compare.field_comparator import (
    ACTIVE_COMPARISON_FIELDS,
    DATE_FIELDS,
    ALWAYS_EXEMPT_FIELDS,
    compare_fields,
    determine_status,
    build_reason,
)


def make_row(**overrides):

    base = {
        "Extracted Name": "John Doe",
        "Extracted DOB": "2024-07-25",
        "Member ID": "M12345",
        "Type (Printed/HW)": "Printed",
        "DOS From": "2024-07-25",
        "DOS To": "2024-07-25",
        "Is Blank or Junk?": "No",
        "Duplicate": "No",
        "Page Type": "Medical Record",
        "Page Subtype": "Progress Note",
        "Codable / Non-Codable": "Codable",
        "Provider Credentials": "MD",
        "Current Sequence": "1",
        "Actual Sequence": "1",
    }

    base.update(overrides)

    return pd.Series(base)


def test_all_fields_match_gives_correct_status():

    gt_row = make_row()
    system_row = make_row()

    field_results = compare_fields(gt_row, system_row)
    status = determine_status(field_results)

    assert status == "CORRECT"
    assert build_reason(status, field_results) == "All fields match."

    for field_name in ACTIVE_COMPARISON_FIELDS:
        assert field_results[field_name]["match"] is True
        expected = "07/25/2024" if field_name in DATE_FIELDS else gt_row[field_name]
        assert field_results[field_name]["display"] == expected


def test_single_mismatch_gives_error_status_and_display():

    gt_row = make_row()
    system_row = make_row(**{"Page Type": "Billing Record"})

    field_results = compare_fields(gt_row, system_row)
    status = determine_status(field_results)

    assert status == "ERROR"

    reason = build_reason(status, field_results)
    assert reason == "Mismatched fields: Page Type"

    assert field_results["Page Type"]["match"] is False
    assert field_results["Page Type"]["display"] == (
        "GT: Medical Record | Algo: Billing Record"
    )

    # Unaffected fields still show their common value
    assert field_results["Member ID"]["display"] == "M12345"


def test_multiple_mismatches_are_all_named_in_reason():

    gt_row = make_row()
    system_row = make_row(**{
        "Duplicate": "Yes",
        "Is Blank or Junk?": "Yes",
    })

    field_results = compare_fields(gt_row, system_row)
    status = determine_status(field_results)

    assert status == "ERROR"

    reason = build_reason(status, field_results)
    assert "Duplicate" in reason
    assert "Is Blank or Junk?" in reason


def test_whitespace_and_case_differences_are_not_errors():

    gt_row = make_row(**{"Page Subtype": "Progress Note"})
    system_row = make_row(**{"Page Subtype": " progress note "})

    field_results = compare_fields(gt_row, system_row)

    assert field_results["Page Subtype"]["match"] is True
    # Raw GT value is shown, not the normalized one
    assert field_results["Page Subtype"]["display"] == "Progress Note"


def test_dates_in_different_formats_still_match():

    gt_row = make_row(**{"DOS From": "2024-07-25"})
    system_row = make_row(**{"DOS From": "07/25/2024"})

    field_results = compare_fields(gt_row, system_row)

    assert field_results["DOS From"]["match"] is True


def test_extracted_dob_in_different_formats_still_matches():

    gt_row = make_row(**{"Extracted DOB": "2024-07-25"})
    system_row = make_row(**{"Extracted DOB": "07/25/2024"})

    field_results = compare_fields(gt_row, system_row)

    assert field_results["Extracted DOB"]["match"] is True


def test_missing_values_treated_as_empty_and_compared():

    gt_row = make_row(**{"Page Subtype": ""})
    system_row = make_row(**{"Page Subtype": pd.NA})

    field_results = compare_fields(gt_row, system_row)

    assert field_results["Page Subtype"]["match"] is True


def test_yes_no_field_equivalent_representations_match():

    gt_row = make_row(**{"Duplicate": "Yes"})
    system_row = make_row(**{"Duplicate": "Y"})

    field_results = compare_fields(gt_row, system_row)

    assert field_results["Duplicate"]["match"] is True


def test_yes_no_field_dash_and_na_are_not_treated_as_no():

    gt_row = make_row(**{"Is Blank or Junk?": "-"})
    system_row = make_row(**{"Is Blank or Junk?": "No"})

    field_results = compare_fields(gt_row, system_row)

    assert field_results["Is Blank or Junk?"]["match"] is False


def test_yes_no_field_dash_and_na_are_equivalent_to_each_other():

    gt_row = make_row(**{"Is Blank or Junk?": "-"})
    system_row = make_row(**{"Is Blank or Junk?": "N/A"})

    field_results = compare_fields(gt_row, system_row)

    assert field_results["Is Blank or Junk?"]["match"] is True


def test_codable_field_equivalent_representations_match():

    gt_row = make_row(**{"Codable / Non-Codable": "Non Codable"})
    system_row = make_row(**{"Codable / Non-Codable": "Non-Codable"})

    field_results = compare_fields(gt_row, system_row)

    assert field_results["Codable / Non-Codable"]["match"] is True


def test_fields_to_compare_restricts_which_fields_are_evaluated():
    """
    The caller (generate_report.compare_files) controls exactly
    which fields participate per run -- e.g. to exclude a field
    missing from one of the two files. compare_fields() must
    only ever touch the fields it's given.
    """

    gt_row = make_row(**{"Member ID": "M1", "Page Type": "A"})
    system_row = make_row(**{"Member ID": "M2", "Page Type": "A"})

    field_results = compare_fields(
        gt_row, system_row, fields_to_compare=["Page Type"]
    )

    assert set(field_results.keys()) == {"Page Type"}
    assert determine_status(field_results) == "CORRECT"


def test_always_exempt_fields_are_not_in_active_comparison_fields():

    for field_name in ALWAYS_EXEMPT_FIELDS:
        assert field_name not in ACTIVE_COMPARISON_FIELDS
