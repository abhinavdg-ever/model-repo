"""Provider credentials are read from the hardcoded canon beside a name."""
from __future__ import annotations

from stages.lib.extraction.provider_credentials.extract import extract_page
from stages.lib.extraction.util.geometry import Box, Word


def _word(index: int, content: str, top: float) -> Word:
    left = index * 40
    return Word(index, content, Box(left, top, left + 30, top + 12))


def test_a_credential_after_a_name_is_kept():
    words = [
        _word(0, "Jane", 0),
        _word(1, "Smith,", 0),
        _word(2, "MD", 0),
        _word(3, "Lee,", 20),
        _word(4, "DM", 20),
    ]
    selected = [hit.value for hit in extract_page(words) if hit.selected]
    assert selected == ["MD", "DM"]


def test_a_state_abbreviation_before_a_zip_is_not_a_credential():
    words = [
        _word(0, "Baltimore,", 0),
        _word(1, "MD", 0),
        _word(2, "21201", 0),
    ]
    assert extract_page(words) == []


def test_a_hyphenated_credential_is_kept():
    words = [
        _word(0, "Patel,", 0),
        _word(1, "PA-C", 0),
    ]
    hits = extract_page(words)
    assert [hit.value for hit in hits if hit.selected] == ["PA-C"]


def _line(text: str, top: float, start: int = 0) -> list[Word]:
    return [_word(start + n, token, top) for n, token in enumerate(text.split())]


def test_the_provider_is_the_name_before_the_credential_up_to_a_colon():
    words = _line("CC Provider: Jacqueline M Gomez, NP", 0) + _line("Chad Kohl, M.D.", 20, start=10)
    hits = [hit for hit in extract_page(words) if hit.selected]
    assert [(hit.provider_name, hit.value) for hit in hits] == [
        ("Jacqueline M Gomez", "NP"),
        ("Chad Kohl", "MD"),
    ]


def test_at_most_four_words_and_the_persons_other_credentials_are_skipped():
    words = _line("Seen today by the attending Mary Ann Jo Lee, MD, PhD", 0)
    hits = extract_page(words)
    assert {hit.provider_name for hit in hits} == {"Mary Ann Jo Lee"}


def test_two_providers_with_the_same_credential_are_both_selected():
    words = _line("Jane Smith, NP", 0) + _line("Ann Lee, NP", 20, start=10) + _line("Ann Lee, NP", 40, start=20)
    hits = [hit for hit in extract_page(words) if hit.selected]
    assert [(hit.provider_name, hit.value) for hit in hits] == [("Jane Smith", "NP"), ("Ann Lee", "NP")]
