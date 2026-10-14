import pandas as pd

from app.services.excel_compare.normalizer import (
    clean_date,
    clean_text,
    normalize_codable,
    normalize_yes_no,
)


# =============================================================
# YES/NO NORMALIZATION
# =============================================================

def test_yes_no_common_equivalent_spellings_normalize_the_same():

    for value in ["Yes", " yes ", "YES", "Y", "y", "TRUE", "true", "1"]:
        assert normalize_yes_no(value) == "yes"

    for value in ["No", " no ", "NO", "N", "n", "FALSE", "false", "0"]:
        assert normalize_yes_no(value) == "no"


def test_yes_no_blank_dash_and_na_are_not_coerced_to_no():

    for value in ["", "-", "--", "N/A", "n/a", "NA", "None", pd.NA]:
        result = normalize_yes_no(value)
        assert result == ""
        assert result != "no"


def test_yes_no_blank_dash_and_na_variants_are_equivalent_to_each_other():

    assert normalize_yes_no("-") == normalize_yes_no("N/A")
    assert normalize_yes_no("N/A") == normalize_yes_no("")
    assert normalize_yes_no("NA") == normalize_yes_no("--")


def test_yes_no_explicit_no_is_distinct_from_blank_or_na():

    assert normalize_yes_no("No") != normalize_yes_no("-")
    assert normalize_yes_no("No") != normalize_yes_no("N/A")
    assert normalize_yes_no("No") != normalize_yes_no("")


def test_yes_no_unrecognized_value_is_not_guessed():

    assert normalize_yes_no("Maybe") == "maybe"


# =============================================================
# CODABLE / NON CODABLE NORMALIZATION
# =============================================================

def test_codable_equivalent_spellings_normalize_the_same():

    for value in ["Codable", " codable ", "CODABLE"]:
        assert normalize_codable(value) == "codable"

    for value in [
        "Non Codable",
        "Non-Codable",
        "NonCodable",
        "Not Codable",
        "not-codable",
    ]:
        assert normalize_codable(value) == "non codable"


def test_codable_blank_is_preserved_not_guessed():

    assert normalize_codable("") == ""
    assert normalize_codable(pd.NA) == ""


def test_codable_unrecognized_value_is_not_guessed():

    assert normalize_codable("Partially Codable") == "partially codable"


# =============================================================
# DATE NORMALIZATION
# =============================================================

def test_equivalent_date_formats_normalize_the_same():

    expected = "2024-07-25"

    assert clean_date("2024-07-25") == expected
    assert clean_date("07/25/2024") == expected
    assert clean_date("July 25, 2024") == expected
    assert clean_date("25-Jul-2024") == expected


def test_blank_date_is_preserved_as_blank():

    assert clean_date("") == ""
    assert clean_date(pd.NA) == ""


# =============================================================
# TEXT NORMALIZATION (existing behavior, unchanged)
# =============================================================

def test_clean_text_trims_and_collapses_whitespace_and_case():

    assert clean_text("  New   Patient ") == "new patient"
    assert clean_text(pd.NA) == ""
