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


SIGNED_BY = "Electronically signed by"


def test_signature_present_only_with_an_accepted_signature():
    # A provider name or credentials alone do not make a signature; they name the page.
    fields = signature_fields(
        [{"key": SIGNED_BY, "provider_name": "A. Patel, MD", "accepted": False}],
        [{"value": "Liane Kirchberger, PA", "score": 0.95}],
        [{"value": "PA", "accepted": True}],
    )
    assert (fields["signature_present"], fields["provider_name"], fields["signers"]) == (False, "Liane Kirchberger", [])
    # Accepted but not selected still counts.
    fields = signature_fields([{"key": SIGNED_BY, "provider_name": "A. Patel, MD", "accepted": True, "selected": False}])
    assert (fields["signature_present"], fields["provider_name"], fields["provider_credentials"]) == (True, "A. Patel", "MD")


def test_every_accepted_signer_is_listed_the_selected_one_first():
    fields = signature_fields([
        {"key": "Cosigned by", "provider_name": "Jane Roe, DNP", "score": 0.95, "accepted": True, "selected": False},
        {"key": SIGNED_BY, "provider_name": "WILLIAM J WALL III, MD", "designation": "MD", "score": 0.9,
         "accepted": True, "selected": True, "signature_date": "01/12/2026"},
        {"key": "Signed", "sentence": "Signed", "score": 0.5, "accepted": True, "selected": False},  # no name: left out
        {"key": "Cosigned by", "provider_name": "Jane Roe", "score": 0.4, "accepted": True},  # the same person
    ])
    assert [(s["name"], s["credentials"], s["signature_date"]) for s in fields["signers"]] == [
        ("WILLIAM J WALL III", "MD", "2026-01-12"),
        ("Jane Roe", "DNP", None),
    ]
    assert (fields["provider_name"], fields["provider_credentials"]) == ("WILLIAM J WALL III", "MD")
    row = signature_csv_row("c", "1.jpg", 1, fields)
    assert json.loads(row["signers"])[1]["name"] == "Jane Roe"


def test_the_signature_names_the_provider_and_its_designation_the_credentials():
    # 66949125 p?: 'AND ELECTRONICALLY SIGNED BY: WILLIAM J WALL III, MD on ...'
    fields = signature_fields(
        [{"key": SIGNED_BY, "provider_name": "WILLIAM J WALL III, MD", "designation": "MD",
          "signature_date": "01/12/2026", "score": 0.92, "selected": True}],
        [{"value": "Someone Else, DO", "score": 0.99}],
        [{"value": "DO", "accepted": True}],
    )
    assert (fields["provider_name"], fields["provider_credentials"]) == ("WILLIAM J WALL III", "MD")
    assert fields["signature_date"] == "2026-01-12"


def test_credentials_missing_from_the_fields_are_read_from_the_sentence():
    # The signature row has the name but no designation; its sentence has ', DNP'.
    fields = signature_fields(
        [{"key": SIGNED_BY, "provider_name": "Meghan Newman", "designation": "",
          "sentence": "Electronically signed by Meghan Newman , DNP on 05/19/2025 at 09:09 AM MDT",
          "score": 0.96, "selected": True}],
        [{"value": "Meghan Newman, FNP", "score": 0.92}],
        [{"value": "FNP", "provider_name": "Meghan Newman", "accepted": True}],
    )
    assert (fields["provider_name"], fields["provider_credentials"]) == ("Meghan Newman", "DNP")


def test_a_missing_name_comes_from_ner_text_and_credentials_from_the_sentence():
    fields = signature_fields([{"key": SIGNED_BY, "provider_name": "", "ner_text": "WILLIAM J WALL III",
                                "sentence": "AND ELECTRONICALLY SIGNED BY: WILLIAM J WALL III, MD on at 03:36 PM",
                                "score": 0.92, "accepted": True, "selected": True}])
    assert (fields["provider_name"], fields["provider_credentials"]) == ("WILLIAM J WALL III", "MD")


def test_one_signature_falls_back_to_the_page_provider_and_credentials():
    nameless = [{"key": "Signed", "sentence": "Electronically Signed", "score": 0.7, "accepted": True}]
    fields = signature_fields(
        nameless,
        [{"value": "Not Accepted, DO", "score": 0.6, "accepted": False},  # neither accepted nor > 0.7
         {"value": "Daniel J Hamilton, MD", "score": 0.8, "accepted": True}],
        [],
    )
    assert (fields["provider_name"], fields["provider_credentials"]) == ("Daniel J Hamilton", "MD")
    no_creds = [{"key": SIGNED_BY, "provider_name": "Meghan Newman", "sentence": "Signed by Meghan Newman",
                 "score": 0.9, "accepted": True}]
    fields = signature_fields(no_creds, [], [{"value": "NP", "provider_name": "Other Person", "accepted": True},
                                              {"value": "FNP", "provider_name": "Meghan Newman", "accepted": True}])
    assert fields["provider_credentials"] == "FNP"  # the same provider's first


def test_several_signatures_stop_at_their_own_fields_and_drop_incomplete_ones():
    fields = signature_fields(
        [{"key": SIGNED_BY, "provider_name": "WILLIAM J WALL III, MD", "designation": "MD",
          "score": 0.9, "accepted": True, "selected": True},
         {"key": "Cosigned by", "provider_name": "Jane Roe", "sentence": "Cosigned by Jane Roe",
          "score": 0.8, "accepted": True}],  # no credentials anywhere: removed
        [{"value": "Jane Roe, DNP", "score": 0.9, "accepted": True}],
        [{"value": "DNP", "provider_name": "Jane Roe", "accepted": True}],
    )
    assert [s["name"] for s in fields["signers"]] == ["WILLIAM J WALL III"]


def test_split_name_and_credentials():
    from stages.lib.extraction.results import credentials_in_sentence, split_name_credentials

    assert split_name_credentials("LAUBERT CNP, MATTHEW") == ("LAUBERT, MATTHEW", "CNP")
    assert split_name_credentials("Agati, Alicia, APRN.CNP") == ("Agati, Alicia", "APRN.CNP")
    assert split_name_credentials("Ramsey Luce, R.N.") == ("Ramsey Luce", "R.N.")
    assert split_name_credentials("Doe, Ed") == ("Doe, Ed", None)
    assert credentials_in_sentence("AND ELECTRONICALLY SIGNED BY: WILLIAM J WALL III, MD on at 03:36 PM") == "MD"
    assert credentials_in_sentence("Signed by Jane Doe Rd 12 on Md Ave") is None  # mis-cased: not credentials


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


def test_credentials_must_be_in_capitals():
    from stages.lib.extraction.results import caps_credentials_only

    assert caps_credentials_only("Jane Doe, RD") == "Jane Doe, RD"
    assert caps_credentials_only("Jane Doe R.D., LD") == "Jane Doe R.D., LD"
    assert caps_credentials_only("Jane Doe Rd") == "Jane Doe"
    assert caps_credentials_only("Jane Doe, Md") == "Jane Doe"
    assert caps_credentials_only("Jane Doe, Md, NP") == "Jane Doe, NP"
    assert caps_credentials_only("Doe, Ed") == "Doe, Ed"
    assert caps_credentials_only("Jane Doe, PhD") == "Jane Doe, PhD"
    assert caps_credentials_only("Jane Doe, Ph.D.") == "Jane Doe, Ph.D."
    assert caps_credentials_only("Jane Doe, PharmD") == "Jane Doe, PharmD"
    assert caps_credentials_only("Jane Doe, phd") == "Jane Doe"
    assert caps_credentials_only("Richard Roe, MD") == "Richard Roe, MD"
    assert caps_credentials_only(None) is None
    fields = signature_fields({"provider_name": "Jane Doe Rd", "score": 0.9})
    assert fields["provider_name"] == "Jane Doe"


def test_confidence_is_the_score_lowered_for_outside_values_and_several_signatures():
    own = signature_fields([{"key": SIGNED_BY, "provider_name": "A. Patel, MD", "score": 0.8, "accepted": True}])
    assert own["confidence"] == 0.8
    outside = signature_fields(
        [{"key": "Signed", "sentence": "Electronically Signed", "score": 0.8, "accepted": True}],
        [{"value": "Daniel J Hamilton, MD", "score": 0.9, "accepted": True}],
    )
    assert outside["confidence"] == 0.6  # 0.8 x 0.75
    several = signature_fields([
        {"key": SIGNED_BY, "provider_name": "A. Patel, MD", "score": 0.8, "accepted": True, "selected": True},
        {"key": "Cosigned by", "provider_name": "Jane Roe, DNP", "score": 0.5, "accepted": True},
    ])
    assert [s["confidence"] for s in several["signers"]] == [0.72, 0.45]  # each x 0.9
    assert several["confidence"] == 0.585  # their average


def test_a_row_counts_when_accepted_or_scored_above_0_7():
    high = signature_fields([{"key": SIGNED_BY, "provider_name": "A. Patel, MD", "score": 0.75, "accepted": False}])
    assert (high["signature_present"], high["provider_name"]) == (True, "A. Patel")
    low = signature_fields([{"key": SIGNED_BY, "provider_name": "A. Patel, MD", "score": 0.7, "accepted": False}])
    assert low["signature_present"] is False


def test_a_missing_name_comes_from_a_credential_row_that_names_its_provider():
    fields = signature_fields(
        [{"key": "Signed", "sentence": "Electronically Signed", "score": 0.8, "accepted": True}],
        [{"value": "Other Doctor, DO", "score": 0.95, "accepted": True}],
        [{"value": "FNP", "provider_name": "Meghan Newman", "score": 0.9, "accepted": False}],  # counts: > 0.7
    )
    assert (fields["provider_name"], fields["provider_credentials"]) == ("Meghan Newman", "FNP")
    assert fields["confidence"] == 0.6  # from outside the signature: 0.8 x 0.75


def test_a_signature_with_no_name_and_no_credentials_anywhere_is_no():
    fields = signature_fields([{"key": "Signed", "sentence": "Electronically Signed", "score": 0.8, "accepted": True}])
    assert (fields["signature_present"], fields["provider_name"], fields["signers"]) == (False, None, [])


def test_no_signature_names_the_page_provider_with_signature_no():
    # A consult page with a lab section: no electronic signature; the credentials row names
    # the lab's physician (Weidong Huang, MD), which wins over the provider_name row.
    providers = [{"key": "Physician", "value": "Finkelstein Tom I, MD", "score": 0.7728, "accepted": True, "selected": True},
                 {"key": "Performed by", "value": "", "score": 0, "accepted": False}]
    credentials = [{"key": "Credential", "value": "MD", "provider_name": "Weidong Huang", "score": 0.9, "accepted": True}]
    fields = signature_fields([], providers, credentials)
    assert (fields["signature_present"], fields["provider_name"], fields["provider_credentials"]) == (False, "Weidong Huang", "MD")
    assert (fields["signers"], fields["confidence"]) == ([], 0.675)  # 0.9 x 0.75
    # Without a credentials row naming someone: the provider_name row.
    fields = signature_fields([], providers, [])
    assert (fields["provider_name"], fields["provider_credentials"]) == ("Finkelstein Tom I", "MD")
    # Nothing counted anywhere: all empty.
    assert signature_fields([], [], [])["provider_name"] is None
