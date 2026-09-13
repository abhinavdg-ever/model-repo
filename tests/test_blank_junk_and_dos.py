"""Blank/junk duplicate scoping and DOS date handling.

Both cover defects the v6 implementation had:

* duplicate detection rebuilt its fingerprint table per pass over only that
  pass's pages, so a page duplicating one handled in an earlier pass was missed;
* the DOS stage bypassed the reference driver, losing the LLM pass, the
  document-level carry-forward, and real ISO normalisation.
"""
from __future__ import annotations

import pytest

from stages.blank_junk_classify import SUBTYPE_DB, _classify, _to_db_flag
from stages.dos_extract import _date_rows, _split_dates


def page(page_id: int, number: int):
    return {"id": page_id, "page_name": f"{number}.jpg", "page_number": number}


class TestDuplicateScope:
    def test_duplicate_within_one_pass_is_found(self):
        pages = [page(1, 1), page(2, 2)]
        body = "Office visit note for the patient. Assessment and plan follow. " * 6
        texts = {1: body, 2: body}
        rows = _classify(pages, texts, {1, 2}, {})
        flags = {r["page_id"]: r["flag"] for r in rows}
        assert flags[1] == "not_blank_junk"
        assert flags[2] == "duplicate"
        assert next(r for r in rows if r["page_id"] == 2)["duplicate_of"] == 1

    def test_duplicate_of_a_page_judged_in_an_earlier_pass_is_found(self):
        """The v6 bug: page 2 is classified in a later pass, and page 1's
        fingerprint has to be carried in for the match to happen."""
        body = "Consultation note. History of present illness. Plan documented. " * 6
        seeded = {}
        # Pass 1 judged page 1.
        rows1 = _classify([page(1, 1)], {1: body}, {1}, seeded)
        assert rows1[0]["flag"] == "not_blank_junk"
        # Pass 2 judges page 2, carrying the fingerprint table forward.
        rows2 = _classify([page(2, 2)], {2: body}, {2}, seeded)
        assert rows2[0]["flag"] == "duplicate"
        assert rows2[0]["duplicate_of"] == 1

    def test_a_page_is_never_a_duplicate_of_itself(self):
        body = "Progress note with enough content to fingerprint properly. " * 6
        seeded: dict = {}
        _classify([page(1, 1)], {1: body}, {1}, seeded)
        again = _classify([page(1, 1)], {1: body}, {1}, seeded)
        assert again[0]["flag"] == "not_blank_junk"

    def test_earliest_page_stays_the_original(self):
        pages = [page(1, 1), page(2, 2), page(3, 3)]
        body = "Discharge summary text repeated across several pages of the chart. " * 6
        texts = {1: body, 2: body, 3: body}
        rows = _classify(pages, texts, {1, 2, 3}, {})
        by_id = {r["page_id"]: r for r in rows}
        assert by_id[1]["flag"] == "not_blank_junk"
        assert by_id[2]["duplicate_of"] == 1
        assert by_id[3]["duplicate_of"] == 1

    def test_only_requested_pages_are_classified(self):
        pages = [page(1, 1), page(2, 2)]
        texts = {1: "alpha content here", 2: "beta content here"}
        rows = _classify(pages, texts, {2}, {})
        assert [r["page_id"] for r in rows] == [2]

    def test_blank_page_is_flagged_blank(self):
        rows = _classify([page(1, 1)], {1: "   "}, {1}, {})
        assert rows[0]["flag"] == "blank"


class TestSubtypeMapping:
    def test_junk_always_gets_a_legal_subtype(self):
        """JUNK_CODES includes CODE_BLANK; blank is its own flag and takes
        precedence, so only the remaining codes become flag='junk'."""
        from classify import CODE_BLANK, JUNK_CODES

        for code in JUNK_CODES - {CODE_BLANK}:
            flag, subtype = _to_db_flag(code)
            assert flag == "junk"
            assert subtype in set(SUBTYPE_DB.values())

    def test_blank_wins_over_junk_even_though_it_is_in_junk_codes(self):
        from classify import CODE_BLANK, JUNK_CODES

        assert CODE_BLANK in JUNK_CODES
        flag, subtype = _to_db_flag(CODE_BLANK)
        assert flag == "blank"
        assert subtype is None

    def test_non_junk_has_no_subtype(self):
        from classify import CODE_BLANK, CODE_DUPLICATE, CODE_MAIN

        for code in (CODE_BLANK, CODE_DUPLICATE, CODE_MAIN):
            _flag, subtype = _to_db_flag(code)
            assert subtype is None


class TestDosDates:
    def test_single_date_makes_one_row(self):
        rows = _date_rows({"dos_from": "03-14-2024", "dos_to": "03-14-2024"})
        assert len(rows) == 1
        assert rows[0]["dos_from"] == "2024-03-14"
        assert rows[0]["dos_to"] == "2024-03-14"

    def test_multi_date_page_makes_one_row_per_date(self):
        """v6's single from/to columns could hold only one pair, while the CSV
        contract already allowed comma-separated lists."""
        rows = _date_rows(
            {"dos_from": "03-14-2024, 04-02-2024", "dos_to": "03-14-2024, 04-02-2024"}
        )
        assert len(rows) == 2
        assert [r["dos_from"] for r in rows] == ["2024-03-14", "2024-04-02"]

    def test_range_without_matching_to_list_reuses_the_last(self):
        rows = _date_rows({"dos_from": "03-14-2024, 04-02-2024", "dos_to": "04-05-2024"})
        assert len(rows) == 2
        assert rows[1]["dos_to"] == "2024-04-05"

    def test_no_dates_makes_no_rows(self):
        assert _date_rows({"dos_from": "", "dos_to": ""}) == []
        assert _date_rows({}) == []

    def test_keyword_is_carried_through(self):
        rows = _date_rows(
            {"dos_from": "03-14-2024", "dos_to": "03-14-2024", "keyword": "Date Of Service"}
        )
        assert rows[0]["source_keyword"] == "Date Of Service"

    def test_split_dates_trims(self):
        assert _split_dates(" 01-01-2024 ,02-02-2024 ") == ["01-01-2024", "02-02-2024"]


class TestDosDriverIsTheV1One:
    def test_document_level_carry_forward_happens(self):
        """A page with no DOS of its own inherits the previous encounter's —
        behaviour that lives in the reference driver the stage now calls."""
        from dos_logic import detect_dos_per_page

        text = (
            "===== 1.jpg =====\n"
            "Office Visit  Date of Service: 03/14/2024\n"
            "Patient seen for follow up.\n"
            "\n"
            "===== 2.jpg =====\n"
            "Continued progress note with no date on it.\n"
        )
        hits = detect_dos_per_page(text, None, use_llm=False)
        assert len(hits) == 2
        assert hits[0]["dos_from"]
        # Page 2 has no page-level DOS but does carry a document-level one.
        assert hits[1]["dos_from"] == ""
        assert hits[1]["doc_dos_from"]

    def test_iso_columns_are_actually_iso(self):
        """v6 wrote doc_dos_from_iso as a copy of the un-normalised value."""
        from dos_logic import detect_dos_per_page

        text = "===== 1.jpg =====\nDate of Service: 03/14/2024\nVisit note.\n"
        hits = detect_dos_per_page(text, None, use_llm=False)
        iso = hits[0]["doc_dos_from_iso"]
        if iso:
            assert iso.count("-") == 2
            year = iso.split("-")[0]
            assert len(year) == 4 and year.isdigit()

    def test_llm_is_not_called_when_no_client(self):
        from dos_logic import detect_dos_per_page

        text = "===== 1.jpg =====\nNo dates whatsoever on this page.\n"
        hits = detect_dos_per_page(text, None, use_llm=False)
        assert len(hits) == 1
        assert hits[0]["match_type"] != "llm"
