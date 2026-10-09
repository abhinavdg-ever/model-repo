"""Review pane reads the staged extraction and does not rewrite it."""
from __future__ import annotations

import json

from app.services.dates import show_date, show_dates_in
from app.services.extraction_review import field_rows, heron_headers, load_extraction_review
from app.services.imaging_overlays import index_signature_rows, signature_people


def test_printed_dates_are_shown_as_iso():
    assert show_date("03/15/2024") == "2024-03-15"
    assert show_date("15 Mar 2024") == "2024-03-15"
    assert show_date("2024-03-15") == "2024-03-15"
    assert show_dates_in("A. Patel · 03/15/2024") == "A. Patel · 2024-03-15"
    assert show_dates_in("03/15/2024 – 03/20/2024") == "2024-03-15 – 2024-03-20"


def test_selected_values_are_shown_and_processed_matches_extracted():
    page = {
        "fields": {
            "name": [
                {"accepted": True, "selected": False, "value": "Other Person"},
                {"accepted": True, "selected": True, "value": "Jane Chen"},
            ],
            "provider_name": [{"accepted": True, "selected": True, "value": "A. Patel, MD"}],
            "electronic_signature": [
                {
                    "accepted": True,
                    "selected": True,
                    "provider_name": "A. Patel",
                    "signature_date": "03/15/2024",
                }
            ],
            "page_no": [{"accepted": True, "selected": True, "page_no": "2", "page_total": "5"}],
            "heading_heron": [
                {"accepted": True, "text": "History of Present Illness", "score": 0.8},
                {"accepted": False, "text": "Patient:"},
            ],
            "dob": [{"accepted": True, "selected": True, "value": "March 15, 1980"}],
            "dos": [{"accepted": True, "selected": True, "value": "03/15/2024", "dos_from": "03/15/2024", "dos_to": "03/20/2024"}],
        }
    }
    by_id = {row.id: row for row in field_rows(page)}
    assert by_id["name"].extracted == "Jane Chen"
    assert by_id["provider_name"].extracted == "A. Patel"
    assert by_id["electronic_signature"].extracted == "A. Patel · 2024-03-15"
    assert by_id["page_no"].extracted == "2 of 5"
    assert by_id["heading_heron"].extracted == "History of Present Illness"
    assert by_id["dos"].extracted == "2024-03-15"
    assert by_id["dob"].extracted == "1980-03-15"
    assert by_id["member_id"].extracted == ""
    for row in by_id.values():
        assert row.processed == row.extracted
        assert row.ground_truth == ""


def test_provider_name_drops_credentials_and_the_separator():
    from app.services.extraction_review import _provider_name

    assert _provider_name("Michael J Battaglia, DO") == "Michael J Battaglia"
    assert _provider_name("Davis, Alfred H III, PA") == "Davis, Alfred H III"
    assert _provider_name("MAHAJAN MD") == "MAHAJAN"
    assert _provider_name("Robert Erickson") == "Robert Erickson"
    assert _provider_name("Alfred H | MD") == "Alfred H"


def test_heron_boxes_become_overlay_fractions():
    page = {
        "width": 1000,
        "height": 2000,
        "fields": {
            "heading_heron": [
                {
                    "accepted": True,
                    "selected": True,
                    "text": "History of Present Illness",
                    "level": "Heading",
                    "box": [100, 200, 400, 260],
                },
                {"accepted": False, "text": "Patient:", "box": [10, 10, 40, 30]},
            ]
        },
    }
    headers = heron_headers(page)
    assert len(headers) == 1
    assert headers[0].text == "History of Present Illness"
    assert headers[0].level == 1
    assert headers[0].left == 0.1
    assert headers[0].top == 0.1
    assert headers[0].width == 0.3
    assert headers[0].height == 0.03


def test_section_headers_keep_a_common_match_and_read_left_to_right():
    page = {
        "width": 1000,
        "height": 2000,
        "fields": {
            "heading_heron": [
                {
                    "accepted": False,
                    "text": "Invoice Total",
                    "level": "Heading",
                    "score": 0.9,
                    "box": [500, 200, 800, 260],
                },
                {
                    "accepted": True,
                    "selected": True,
                    "text": "History of Present Illness",
                    "level": "Heading",
                    "score": 0.8,
                    "box": [40, 80, 400, 120],
                },
            ]
        },
    }
    headers = heron_headers(page)
    assert [header.text for header in headers] == ["History of Present Illness"]
    assert headers[0].left < 0.1


def test_missing_staging_returns_empty_fields(tmp_path):
    chart = tmp_path / "chart8841"
    chart.mkdir()
    result = load_extraction_review(tmp_path, "chart8841", "1.jpg")
    assert result.available is False
    assert [row.label for row in result.fields] == [
        "Member Name",
        "Member DOB",
        "Member ID",
        "Provider Name",
        "Provider Credentials",
        "Provider Signature",
        "Date of Service",
        "Page Number",
        "Headings",
    ]


def test_two_of_three_heading_checks_is_enough():
    page = {
        "width": 1000,
        "height": 2000,
        "fields": {
            "heading_heron": [
                {
                    "accepted": True,
                    "text": "OFFICE VISIT REPORT 03/28/2024",
                    "score": 0.77,
                    "level": "Heading",
                    "box": [100, 100, 400, 140],
                },
                {
                    "accepted": False,
                    "text": "PLAN:",
                    "score": 0.69,
                    "level": "Heading",
                    "box": [40, 200, 140, 240],
                },
                {
                    "accepted": True,
                    "text": "Mr.",
                    "score": 0.37,
                    "note": "low score",
                    "level": "Subheading",
                    "box": [100, 300, 180, 340],
                },
            ]
        },
    }
    assert [header.text for header in heron_headers(page)] == [
        "Office Visit Report 03/28/2024",
        "Plan",
    ]


def test_headers_drop_a_numeric_opening_and_strip_punctuation():
    page = {
        "width": 1000,
        "height": 2000,
        "fields": {
            "heading_heron": [
                {
                    "accepted": True,
                    "text": "8/14/2019: RUS:",
                    "score": 0.8,
                    "box": [40, 80, 300, 120],
                },
                {
                    "accepted": True,
                    "text": "023466",
                    "score": 0.8,
                    "box": [40, 140, 200, 180],
                },
                {
                    "accepted": True,
                    "text": "** * Signed by | on 03/29/24 at 9:15 AM (CDT) ** * *",
                    "score": 0.8,
                    "box": [40, 200, 700, 240],
                },
                {
                    "accepted": True,
                    "text": "CC/HPI:",
                    "score": 0.8,
                    "box": [40, 260, 200, 300],
                },
            ]
        },
    }
    assert [header.text for header in heron_headers(page)] == [
        "Signed by on 03/29/24 at 9 15 AM CDT",
        "CC/HPI",
    ]


def test_page_file_joins_the_staged_page_by_name(tmp_path):
    chart = tmp_path / "chart8841"
    (chart / "staging").mkdir(parents=True)
    payload = {
        "version": 1,
        "model_version": "v002",
        "pages": {
            "1.png": {
                "fields": {
                    "dob": [{"accepted": True, "selected": True, "value": "01/02/1950"}],
                }
            }
        },
    }
    (chart / "staging" / "extraction.json").write_text(json.dumps(payload), encoding="utf-8")
    result = load_extraction_review(tmp_path, "chart8841", "1.JPG")
    assert result.available is True
    assert result.model_version == "v002"
    dob = next(row for row in result.fields if row.id == "dob")
    assert dob.extracted == "1950-01-02"
    assert dob.processed == "1950-01-02"


def test_signature_csv_splits_each_person():
    assert signature_people("Liane Kirchberger, PA | Daniel J Hamilton, MD") == (
        "Liane Kirchberger | Daniel J Hamilton",
        "PA | MD",
    )
    assert signature_people("Dr Hamilton") == ("Dr Hamilton", "")
    assert signature_people("Michael J Battaglia, DO") == ("Michael J Battaglia", "DO")
    assert signature_people("Robert Erickson") == ("Robert Erickson", "")
    rows = index_signature_rows(
        [
            {
                "chart_name": "60140965.tif",
                "page_name": "6.jpg",
                "page_number": "6",
                "provider_name": "Liane Kirchberger, PA | Daniel J Hamilton, MD",
                "signature_present": "n",
                "confidence": "",
            },
            {
                "chart_name": "60140965.tif",
                "page_name": "4.jpg",
                "page_number": "4",
                "provider_name": "",
                "signature_present": "n",
            },
        ],
        "60140965.tif",
    )
    assert rows["6.jpg"]["providerName"] == "Liane Kirchberger | Daniel J Hamilton"
    assert rows["6.jpg"]["providerCredentials"] == "PA | MD"
    assert rows["6.jpg"]["providerSignature"] == "No"
    assert rows["4.jpg"]["providerSignature"] == "No"
    assert "providerName" not in rows["4.jpg"]
