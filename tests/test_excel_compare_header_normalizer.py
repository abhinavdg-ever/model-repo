import pandas as pd

from app.services.excel_compare.header_normalizer import (
    CANONICAL_COLUMNS,
    build_header_rename_map,
    normalize_headers,
)


def test_canonical_headers_are_left_unchanged():

    rename_map = build_header_rename_map(CANONICAL_COLUMNS)

    assert rename_map == {}


def test_trim_and_collapse_whitespace_in_headers():

    columns = ["  PDF ID  ", "Page   Number", " DOS From"]

    rename_map = build_header_rename_map(columns)

    assert rename_map["  PDF ID  "] == "PDF ID"
    assert rename_map["Page   Number"] == "Page Number"
    assert rename_map[" DOS From"] == "DOS From"


def test_case_insensitive_header_matching():

    columns = ["pdf id", "page number", "member id"]

    rename_map = build_header_rename_map(columns)

    assert rename_map["pdf id"] == "PDF ID"
    assert rename_map["page number"] == "Page Number"
    assert rename_map["member id"] == "Member ID"


def test_punctuation_and_spacing_variations():

    columns = [
        "Page #",
        "Type Printed/HW",
        "Codable/Non Codable",
        "DOS-From",
        "Is Blank or Junk",
    ]

    rename_map = build_header_rename_map(columns)

    assert rename_map["Page #"] == "Page Number"
    assert rename_map["Type Printed/HW"] == "Type (Printed/HW)"
    assert rename_map["Codable/Non Codable"] == "Codable / Non-Codable"
    assert rename_map["DOS-From"] == "DOS From"
    assert rename_map["Is Blank or Junk"] == "Is Blank or Junk?"


def test_known_aliases_and_typos_map_to_canonical_names():

    columns = [
        "PDF No",
        "Ext Name",
        "DOB",
        "Orientation",
        "Dup",
        "Current Seq",
        "Actual Seq",
    ]

    rename_map = build_header_rename_map(columns)

    assert rename_map["PDF No"] == "PDF ID"
    assert rename_map["Ext Name"] == "Extracted Name"
    assert rename_map["DOB"] == "Extracted DOB"
    assert rename_map["Orientation"] == "Orient. (CW)"
    assert rename_map["Dup"] == "Duplicate"
    assert rename_map["Current Seq"] == "Current Sequence"
    assert rename_map["Actual Seq"] == "Actual Sequence"


def test_unrecognized_headers_are_left_out_of_rename_map():

    columns = ["PDF ID", "Some Totally Unknown Column"]

    rename_map = build_header_rename_map(columns)

    assert "Some Totally Unknown Column" not in rename_map


def test_normalize_headers_renames_dataframe_columns_without_mutating_input():

    df = pd.DataFrame(
        {
            " pdf id ": ["123456"],
            "page #": [1],
            "Mystery Column": ["x"],
        }
    )

    normalized = normalize_headers(df)

    assert list(normalized.columns) == ["PDF ID", "Page Number", "Mystery Column"]
    # Original DataFrame (standing in for the uploaded file) is untouched.
    assert list(df.columns) == [" pdf id ", "page #", "Mystery Column"]


def test_normalize_headers_returns_same_object_when_nothing_to_rename():

    df = pd.DataFrame({"PDF ID": ["123456"], "Page Number": [1]})

    normalized = normalize_headers(df)

    assert normalized is df
