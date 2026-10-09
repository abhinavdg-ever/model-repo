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
