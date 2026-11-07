"""2026-11-07 (VM): a chart failed in DOS with 'not enough values to unpack (expected 3, got 1)'.
canonical_date returns the text itself when it cannot read a date (an OCR misread such as
'02/30/2025'), and that is 10 characters, so a length check let it through as an ISO date."""
from __future__ import annotations

import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from module.dos import dos_logic  # noqa: E402
from module.extraction.util.dates import canonical_date, is_iso_date  # noqa: E402


def test_an_unreadable_ten_character_date_is_not_iso():
    assert canonical_date("02/30/2025") == "02/30/2025"  # 30 February: not a date
    assert not is_iso_date("02/30/2025") and not is_iso_date("2025-13-40")
    assert is_iso_date("2025-11-03")
    assert dos_logic.iso_to_mdy("02/30/2025") == ""


def test_signature_and_key_value_dates_skip_it():
    assert dos_logic.signature_page_date(["02/30/2025"]) is None
    assert dos_logic.kv_dated([{"iso": "02/30/2025", "tier": "visit"}]) == []


def test_a_bad_page_date_never_fails_the_chart():
    bad = dos_logic.PageDate("02/30/2025", "02/30/2025", 0.9, "Signed", "signature")
    row = dos_logic._row({"page_name": "1.jpg", "page": 1}, bad, dos_logic.profile())
    assert row["dos_from"] == "" and row["is_default"] is True
