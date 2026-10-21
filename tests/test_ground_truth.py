"""Client page ground truth: Chart_Name + Id, where Id 1 is 1.jpg / 1.png / 1.tif."""

from jobs.ground_truth_load import page_number_from_id, parse_rows, read_table


def test_page_id_matches_the_image_stem():
    assert page_number_from_id(1) == 1
    assert page_number_from_id("1") == 1
    assert page_number_from_id("1.jpg") == 1
    assert page_number_from_id("1.PNG") == 1
    assert page_number_from_id("1.tif") == 1
    assert page_number_from_id("1.tiff") == 1
    assert page_number_from_id("page_1.jpg") is None
    assert page_number_from_id("") is None
    assert page_number_from_id(None) is None


def test_parse_keeps_client_yes_no_and_dates():
    headers = [
        "Chart_Name",
        "Id",
        "Member Name",
        "Member DOB",
        "DOS from",
        "DOS To",
        "Encounter Type",
        "Page Type",
        "Codable Or Non Codable",
        "Blank Page",
        "Junk Page",
        "IsInvoicePage",
        "Rotation",
    ]
    rows = parse_rows(
        headers,
        [
            [
                "70066727_56507239",
                "1.jpg",
                "Yes",
                "Yes",
                "07/17/2025",
                "07/17/2025",
                "Progress note",
                "Accept",
                "Codeable",
                "No",
                "No",
                "",
                "0",
            ],
            ["70066727_56507239", "2.png", "No", "No", "", "", "", "Accept", "Non Codable", "Yes", "No", "", "90"],
            ["70066727_56507239", "", "Yes", "Yes", "", "", "", "", "", "", "", "", ""],
        ],
    )
    kept = [row for row in rows if not row.get("_skip")]
    assert len(kept) == 2
    assert kept[0]["chart_name"] == "70066727_56507239"
    assert kept[0]["page_number"] == 1
    assert kept[0]["source_page_id"] == "1.jpg"
    assert kept[0]["member_name"] == "Yes"
    assert kept[0]["dos_from"] == "07/17/2025"
    assert kept[0]["page_type"] == "Accept"
    assert kept[0]["codeable"] == "Codable"
    assert kept[1]["codeable"] == "Non-Codable"
    assert kept[0]["is_invoice"] is None
    assert kept[1]["page_number"] == 2
    assert kept[1]["blank_page"] == "Yes"
    assert sum(1 for row in rows if row.get("_skip")) == 1


def test_csv_file_round_trip(tmp_path):
    path = tmp_path / "labels.csv"
    path.write_text(
        "Chart_Name,Id,Member Name,Blank Page\n"
        "chart_a,1.tif,Yes,No\n"
        "chart_a,4,No,Yes\n",
        encoding="utf-8",
    )
    rows = [row for row in read_table(path) if not row.get("_skip")]
    assert [row["page_number"] for row in rows] == [1, 4]
    assert rows[0]["source_path"] == str(path)
    assert rows[1]["member_name"] == "No"


def test_every_spelling_of_codable_reads_as_codable_or_non_codable():
    """Sheets and older runs say Codeable / Non Codeable / NonCodeable. They
    are stored and shown as Codable / Non-Codable."""
    from jobs.ground_truth_load import spell_codeable

    for value in ("Codeable", "codeable", "Codable", "CODEABLE"):
        assert spell_codeable(value) == "Codable"
    for value in ("Non Codeable", "Non-Codeable", "NonCodeable", "non_codeable",
                  "Non Codable", "Non-Codable"):
        assert spell_codeable(value) == "Non-Codable"
    assert spell_codeable("Discharge") == "Discharge"
    assert spell_codeable("uncodeable") == "uncodeable"
    assert spell_codeable(None) is None


def test_the_codeable_header_spelling_is_accepted():
    from jobs.ground_truth_load import parse_rows

    rows = parse_rows(
        ["Chart Name", "Id", "Codeable or Non Codeable"],
        [["c1", "1.jpg", "Non Codeable"]],
    )
    assert rows[0]["codeable"] == "Non-Codable"


def test_page_sequence_comes_from_the_sequence_column_only():
    from jobs.ground_truth_load import parse_rows

    rows = parse_rows(
        ["Chart_Name", "Id", "Sequence", "Page Sequence"],
        [["c1", "1.png", "3.0", "Page 7"], ["c1", "2.png", "", "Page 8"]],
    )
    assert rows[0]["page_sequence"] == "3"
    assert rows[1]["page_sequence"] is None


def test_only_the_columns_in_the_file_are_upserted():
    from jobs.ground_truth_load import parse_rows, upsert_sql

    rows = parse_rows(["Chart_Name", "Id", "Sequence"], [["c1", "1.png", "4"]])
    assert rows[0]["_columns"] == ("page_sequence",)
    assert "member_name" not in rows[0]
    sql = upsert_sql(rows[0]["_columns"])
    assert "page_sequence = EXCLUDED.page_sequence" in sql
    assert "member_name" not in sql and "dos_from" not in sql
    # The bulk entry keeps the page's earlier bulk labels and adds this file's.
    assert "|| %(labels)s::jsonb" in sql


def test_upsert_sql_takes_ground_truth_columns_only():
    import pytest

    from jobs.ground_truth_load import upsert_sql

    with pytest.raises(ValueError):
        upsert_sql(("page_sequence", "id; drop table x"))


def test_a_load_appends_a_bulk_upload_entry_with_the_labels_it_wrote():
    import json

    from jobs.ground_truth_load import _TEXT_FIELDS, _UPSERT, entry_labels

    assert "'bulk_upload'" in _UPSERT and "%(labels)s::jsonb" in _UPSERT
    labels = json.loads(entry_labels({"rotation": "90", "member_name": None}))
    assert set(labels) == set(_TEXT_FIELDS)
    assert labels["rotation"] == "90" and labels["member_name"] is None


def test_a_cp1252_csv_from_excel_loads(tmp_path):
    from jobs.ground_truth_load import read_table

    path = tmp_path / "gt.csv"
    path.write_bytes("Chart_Name,Id,Member Name,Sequence\nc1,1.png,Renée O’Neil,2\n".encode("cp1252"))
    rows = read_table(path)
    assert rows[0]["member_name"] == "Renée O’Neil"
    assert rows[0]["page_sequence"] == "2"


def test_the_review_ui_reader_takes_a_cp1252_csv():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review-ui" / "backend"))
    from app.services.excel_compare.file_reader import read_csv_any_encoding

    frame = read_csv_any_encoding("Name,Sequence\nRenée,1\n".encode("cp1252"))
    assert frame.iloc[0]["Name"] == "Renée"
