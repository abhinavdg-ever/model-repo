"""KV extractor rules that need no model: page numbers, member ids, signer credentials."""
from __future__ import annotations

from stages.lib.extraction.electronic_signature.extract import split_name_designation
from stages.lib.extraction.member_id.extract import _id_pieces, accept
from stages.lib.extraction.member_name.extract import _ADDRESS
from stages.lib.extraction.page_no.extract import _INLINE_SLASH, extract_page
from stages.lib.extraction.util.geometry import words_from_page

PAGE_W, PAGE_H = 1000, 1000


def _page(*lines: tuple[float, str]) -> list:
    """Words laid out left to right, one line per (y, text)."""
    words = []
    for y, text in lines:
        x = 50.0
        for part in text.split():
            right = x + 12 * len(part)
            words.append({"content": part, "polygon": [x, y, right, y, right, y + 14, x, y + 14]})
            x = right + 8
    return words_from_page({"words": words, "width": PAGE_W, "height": PAGE_H})


def _page_numbers(*lines: tuple[float, str]) -> list[str]:
    return sorted(hit.value for hit in extract_page(_page(*lines), PAGE_H))


class TestPageNo:
    def test_a_slash_pair_inside_a_date_is_not_a_page(self):
        assert _INLINE_SLASH.search("Order date/time: 07/11/24 1432") is None
        assert _INLINE_SLASH.search("6/6-6/20/2024: mBCG x 3") is None
        assert _INLINE_SLASH.search("Smith, John 2/5").group(0) == "2/5"

    def test_a_page_word_label_beats_an_inline_pair_in_the_other_band(self):
        values = _page_numbers((20, "MRN: 023466 Dose 1/3"), (500, "body text"), (970, "Pg 2 of 10"))
        assert values == ["Pg 2 of 10"]

    def test_n_of_m_beats_a_slash_pair(self):
        values = _page_numbers((20, "MRN: 023466 Dose 3/3"), (500, "body text"), (970, "Smith 6 of 10"))
        assert values == ["6 of 10"]

    def test_an_inline_page_counts_when_nothing_else_does(self):
        assert _page_numbers((20, "Smith, John 01/02/2024 2 of 5"), (500, "body text")) == ["2 of 5"]


class TestMemberId:
    def test_an_id_glued_to_its_key_is_read(self):
        assert [value for value, _, _ in _id_pieces("MRN:88234.0")] == ["88234.0"]
        assert [value for value, _, _ in _id_pieces("(ID #718,")] == ["718"]

    def test_a_decimal_mrn_is_an_id_but_a_short_decimal_is_not(self):
        assert accept("88234.0") == "88234.0"
        assert accept("3.14") == ""


class TestSignature:
    def test_the_credential_is_split_out_wherever_it_sits(self):
        assert split_name_designation("LAUBERT CNP, MATTHEW") == ("LAUBERT MATTHEW", "CNP")
        assert split_name_designation("Reilly, Thomas J MD") == ("Reilly, Thomas J", "MD")


class TestMemberNameEdgeLine:
    def test_a_street_address_is_not_a_demographic_strip(self):
        assert _ADDRESS.search("MF Lab 800 E. DAWSON TYLER 1612 TX 75701 USA")
        assert not _ADDRESS.search("Patient: Smith, John DOB: 01/02/1950 MRN: 023466")
