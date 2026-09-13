from __future__ import annotations

from .name_common import find_three_word_name


def extract_name_3_words(
    ocr_text: str,
    first_name: str,
    middle_name: str,
    last_name: str,
) -> str:
    """Return the name phrase found on the page, else N/A."""
    return find_three_word_name(ocr_text, first_name, middle_name, last_name)
