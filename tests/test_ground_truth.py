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
                "Codable",
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
