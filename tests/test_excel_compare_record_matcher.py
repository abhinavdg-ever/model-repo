import pandas as pd
import pytest

from app.services.excel_compare.record_matcher import match_records


def make_df(rows):
    return pd.DataFrame(rows)


def test_matched_pair_found():

    ground = make_df([
        {"PDF ID": "123456", "Page Number": 1, "Member Name (Yes/No)": "Yes"},
    ])

    system = make_df([
        {"PDF ID": "123456", "Page Number": 1, "Member Name (Yes/No)": "Yes"},
    ])

    matched, missing, extra = match_records(ground, system)

    assert matched == [(0, 0)]
    assert missing == []
    assert extra == []


def test_missing_ground_truth_row():

    ground = make_df([
        {"PDF ID": "123456", "Page Number": 1},
        {"PDF ID": "123456", "Page Number": 2},
    ])

    system = make_df([
        {"PDF ID": "123456", "Page Number": 1},
    ])

    matched, missing, extra = match_records(ground, system)

    assert matched == [(0, 0)]
    assert missing == [1]
    assert extra == []


def test_extra_system_row():

    ground = make_df([
        {"PDF ID": "123456", "Page Number": 1},
    ])

    system = make_df([
        {"PDF ID": "123456", "Page Number": 1},
        {"PDF ID": "123456", "Page Number": 2},
    ])

    matched, missing, extra = match_records(ground, system)

    assert matched == [(0, 0)]
    assert missing == []
    assert extra == [1]


def test_multiple_pdf_ids_in_one_run():

    ground = make_df([
        {"PDF ID": "111111", "Page Number": 1},
        {"PDF ID": "222222", "Page Number": 1},
    ])

    system = make_df([
        {"PDF ID": "111111", "Page Number": 1},
        {"PDF ID": "222222", "Page Number": 1},
    ])

    matched, missing, extra = match_records(ground, system)

    assert set(matched) == {(0, 0), (1, 1)}
    assert missing == []
    assert extra == []


def test_page_number_is_not_confused_across_different_pdf_ids():

    ground = make_df([
        {"PDF ID": "111111", "Page Number": 1},
    ])

    system = make_df([
        {"PDF ID": "222222", "Page Number": 1},
    ])

    matched, missing, extra = match_records(ground, system)

    assert matched == []
    assert missing == [0]
    assert extra == [0]


def test_duplicate_key_within_ground_truth_raises():

    ground = make_df([
        {"PDF ID": "123456", "Page Number": 1},
        {"PDF ID": "123456", "Page Number": 1},
    ])

    system = make_df([
        {"PDF ID": "123456", "Page Number": 1},
    ])

    with pytest.raises(ValueError, match="Duplicate PDF ID/Page Number"):
        match_records(ground, system)


def test_duplicate_key_within_system_raises():

    ground = make_df([
        {"PDF ID": "123456", "Page Number": 1},
    ])

    system = make_df([
        {"PDF ID": "123456", "Page Number": 1},
        {"PDF ID": "123456", "Page Number": 1},
    ])

    with pytest.raises(ValueError, match="Duplicate PDF ID/Page Number"):
        match_records(ground, system)
