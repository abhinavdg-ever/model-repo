from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z0-9]+")
_DUMMY_DOB = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def _date_parts(dummy_dob: str) -> tuple[str, str, str] | None:
    """DummyDOB is MM/DD/YYYY."""
    text = (dummy_dob or "").strip()
    match = _DUMMY_DOB.fullmatch(text)
    if not match:
        return None
    return match.group(1), match.group(2), match.group(3)


def _words(ocr_text: str) -> list[str]:
    return _WORD.findall(ocr_text or "")


def _norm(token: str) -> str:
    if token.isdigit():
        return str(int(token))
    return token


def _window_has_dob(window: list[str], year_index: int, month: str, day: str, year: str) -> bool:
    month_n, day_n, year_n = _norm(month), _norm(day), year
    orders = {
        (day_n, month_n, year_n),
        (month_n, day_n, year_n),
        (year_n, month_n, day_n),
        (year_n, day_n, month_n),
    }
    if year_index >= 2:
        before = tuple(_norm(token) for token in window[year_index - 2 : year_index + 1])
        if before in orders:
            return True
    if year_index + 2 < len(window):
        after = tuple(_norm(token) for token in window[year_index : year_index + 3])
        if after in orders:
            return True
    return False


def date_parts_match(text: str, dummy_dob: str) -> bool:
    parts = _date_parts(dummy_dob)
    if not parts:
        return False
    month, day, year = parts
    words = _words(text)
    for index, word in enumerate(words):
        if word != year:
            continue
        start = max(0, index - 3)
        stop = min(len(words), index + 4)
        window = words[start:stop]
        if _window_has_dob(window, index - start, month, day, year):
            return True
    return False


def extract_dob(ocr_text: str, dummy_dob: str) -> str:
    """Return DummyDOB as MM/DD/YYYY if the three parts sit together on the page, else N/A."""
    parts = _date_parts(dummy_dob)
    if not parts:
        return "N/A"
    month, day, year = parts
    if date_parts_match(ocr_text, dummy_dob):
        return f"{month}/{day}/{year}"
    return "N/A"
