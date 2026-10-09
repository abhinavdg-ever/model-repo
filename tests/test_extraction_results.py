"""Provider signature and additional page details follow the extractor output."""
from __future__ import annotations

import json

from stages.lib.extraction.results import (
    ADDITIONAL_CSV_COLS,
    SIGNATURE_CSV_COLS,
    additional_csv_row,
    additional_fields,
    signature_csv_row,
    signature_fields,
)


def test_signature_row_keeps_the_extractor_fields():
    fields = signature_fields(
        {
            "key": "Electronically signed by",
            "region": "footer",
            "scale": "line",
            "sentence": "Electronically signed by A. Patel on 03/15/2024",
            "ner_text": "A. Patel",
            "provider_name": "A. Patel",
            "signature_date": "03/15/2024",
            "score": 0.91,
            "source": "ner",
            "accepted": True,
            "selected": True,
        }
    )
    assert fields["signature_present"] is True
    assert fields["signature_key"] == "Electronically signed by"
    assert fields["provider_name"] == "A. Patel"
    assert fields["signature_date"] == "2024-03-15"
    assert fields["confidence"] == 0.91
    assert fields["source"] == "ner"
    row = signature_csv_row("chart8841", "1.jpg", 1, fields)
    assert set(row) == set(SIGNATURE_CSV_COLS)
    assert row["signature_present"] == "y"
    assert row["signature_date"] == "2024-03-15"


def test_a_written_month_converts_and_an_unreadable_date_is_empty():
    march = signature_fields({"signature_date": "March 15, 2024", "provider_name": "A. Patel"})
    assert march["signature_date"] == "2024-03-15"
    junk = signature_fields({"signature_date": "signed", "provider_name": "A. Patel"})
    assert junk["signature_date"] is None
    assert junk["signature_present"] is True


def test_several_providers_stay_in_page_order():
    fields = signature_fields(
        [
            {
                "key": "Electronically signed by",
                "provider_name": "Daniel J Hamilton, MD",
                "signature_date": "08/27/2025",
                "ner_text": "Daniel J Hamilton",
                "score": 0.9,
                "source": "ner",
            }
        ],
        [
            {"value": "Liane Kirchberger, PA"},
            {"value": "Daniel J Hamilton, MD"},
            {"value": "Liane Kirchberger, PA"},
        ],
    )
    assert fields["signature_present"] is True
    assert fields["provider_name"] == "Daniel J Hamilton, MD | Liane Kirchberger, PA"
    assert fields["signature_date"] == "2025-08-27"


def test_a_provider_without_a_signature_is_named_and_not_present():
    fields = signature_fields(
        None,
        [{"value": "Liane Kirchberger, PA"}, {"value": "Robert Erickson"}],
    )
    assert fields["signature_present"] is False
    assert fields["provider_name"] == "Liane Kirchberger, PA | Robert Erickson"


def test_empty_signature_is_not_present():
    fields = signature_fields(None)
    assert fields["signature_present"] is False
    assert fields["provider_name"] is None
    assert signature_csv_row("c", "1.jpg", 1, fields)["signature_present"] == "n"


def test_additional_row_keeps_page_number_and_header_json():
    headers = [
        {
            "text": "History of Present Illness",
            "level": 1,
            "bbox": [100, 200, 400, 260],
            "norm": {"left": 0.1, "top": 0.1, "width": 0.3, "height": 0.03},
        }
    ]
    fields = additional_fields(
        {
            "key": "Page",
            "region": "footer",
            "sentence": "Page 2 of 5",
            "value": "Page 2 of 5",
            "page_no": "2",
            "page_total": "5",
            "score": 0.8,
            "source": "rule",
        },
        headers,
    )
    assert fields["printed_page_no"] == "2"
    assert fields["printed_page_total"] == "5"
    assert fields["page_number_value"] == "Page 2 of 5"
    assert fields["section_headers"] == headers
    row = additional_csv_row("chart8841", "1.jpg", 2, fields)
    assert set(row) == set(ADDITIONAL_CSV_COLS)
    assert json.loads(row["section_headers"])[0]["text"] == "History of Present Illness"


def test_every_accepted_person_is_selected():
    from stages.lib.extraction.provider_name.extract import ProviderHit, mark_selected

    short = ProviderHit(key="Provider", region="header", sentence="", value="Liane Kirchberger", accepted=True)
    long = ProviderHit(key="Progress Notes", region="header", sentence="", value="Liane Kirchberger, PA", accepted=True)
    other = ProviderHit(key="Cosigner", region="header", sentence="", value="Daniel J Hamilton, MD", accepted=True)
    mark_selected([short, long, other])
    assert not short.selected
    assert long.selected
    assert other.selected


def test_role_without_id_keeps_the_highest_score_above_the_floor():
    from stages.lib.extraction.provider_name.extract import nearest_provider

    sentence = "physician was Dr. Hamilton"
    raws = [
        {"text": "Dr Hamilton", "start": 14, "end": 25, "score": 0.82},
        {"text": "was", "start": 10, "end": 13, "score": 0.40},
    ]
    value, score, _ner, source = nearest_provider(
        sentence, None, raws, require_suffix=False, by_score=True, floor=0.70
    )
    assert value == "Dr Hamilton"
    assert score == 0.82
    assert source == "ner"
    empty, _, _, _ = nearest_provider(
        sentence,
        None,
        [{"text": "Dr Hamilton", "start": 14, "end": 25, "score": 0.61}],
        require_suffix=False,
        by_score=True,
        floor=0.70,
    )
    assert empty == ""
    bare, _, _, _ = nearest_provider(
        sentence,
        None,
        [{"text": "Hamilton", "start": 18, "end": 26, "score": 0.91}],
        require_suffix=False,
        by_score=True,
        floor=0.70,
    )
    assert bare == ""
    plain, _, _, _ = nearest_provider(
        "referred by Robert Erickson today",
        None,
        [{"text": "Robert Erickson", "start": 12, "end": 27, "score": 0.91}],
        require_suffix=False,
        by_score=True,
        floor=0.0,
    )
    assert plain == ""
    low, _, _, _ = nearest_provider(
        "referred by Robert Erickson today",
        None,
        [{"text": "Robert Erickson", "start": 12, "end": 27, "score": 0.40}],
        require_suffix=False,
        by_score=True,
        floor=0.0,
    )
    assert low == ""
    beside, _, _, _ = nearest_provider(
        "Provider: Jane Smith",
        (0, 8),
        [{"text": "Jane Smith", "start": 10, "end": 20, "score": 0.40}],
        require_suffix=False,
    )
    assert beside == "Jane Smith"


def test_a_name_without_dr_or_a_credential_is_left_out():
    from stages.lib.extraction.provider_name.extract import credentialed_providers, has_title_or_credential

    assert has_title_or_credential("Dr Hamilton")
    assert has_title_or_credential("Liane Kirchberger, PA")
    assert has_title_or_credential("Daniel J Hamilton, MD")
    assert not has_title_or_credential("Liane Kirchberger")
    assert not has_title_or_credential("Robert Erickson")
    kept = credentialed_providers(
        [
            {"value": "Liane Kirchberger, PA", "score": 0.4},
            {"value": "Daniel J Hamilton, MD", "score": 0.4},
            {"value": "Liane Kirchberger", "score": 0.9},
            {"value": "Robert Erickson", "score": 0.97},
            {"value": "Jane Smith", "score": 0.4, "key": "Progress Notes"},
            {
                "value": "Ann Lee",
                "score": 0.4,
                "key": "Provider",
                "sentence": "Provider: Ann Lee",
            },
        ]
    )
    assert [row["value"] for row in kept] == [
        "Liane Kirchberger, PA",
        "Daniel J Hamilton, MD",
        "Ann Lee",
    ]
