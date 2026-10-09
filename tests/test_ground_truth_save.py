"""Ground-truth labels the imaging screen sends are stored as empty or text."""

from app.services.ground_truth import (
    LABEL_COLUMNS,
    _labelled_row_sql,
    clean_label,
    yes_no_label,
)


def test_a_chart_counts_once_any_one_label_cell_is_filled():
    sql = _labelled_row_sql()
    for column in LABEL_COLUMNS:
        assert f"->> '{column}'" in sql
    assert sql.count(" OR ") == len(LABEL_COLUMNS) - 1
    assert "'na'" in sql and "''" in sql


def test_blank_and_na_are_empty():
    assert clean_label(None) is None
    assert clean_label("  ") is None
    assert clean_label("NA") is None
    assert clean_label("n/a") is None
    assert clean_label("Not Found") is None


def test_a_typed_value_is_kept():
    assert clean_label(" Yes ") == "Yes"
    assert clean_label("Progress Notes") == "Progress Notes"


def test_signature_keeps_only_yes_or_no():
    assert yes_no_label("Yes") == "Yes"
    assert yes_no_label("Liane Kirchberger, PA") is None


def test_member_fields_keep_only_yes_or_no():
    assert yes_no_label(" yes ") == "Yes"
    assert yes_no_label("No") == "No"
    assert yes_no_label("Priya Nandakumar") is None
    assert yes_no_label("07/17/2025") is None
    assert yes_no_label("500-50") is None
    assert yes_no_label("NA") is None
