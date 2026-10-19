"""utilities/harvest_keys.py: member_labels (the words printed before the expected member's values)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "utilities" / "harvest_keys.py"
_spec = importlib.util.spec_from_file_location("harvest_keys", _PATH)
harvest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harvest)  # type: ignore[union-attr]

MEMBER = {"first": "Richard", "last": "Lane", "dob": (1978, 4, 15), "member_id": "993544760"}


def test_dates_parse_in_every_printed_format():
    for text in ("4/15/1978", "04-15-78", "1978-04-15", "Apr 15, 1978", "15th April 1978"):
        assert harvest.parse_date(text) == (1978, 4, 15), text
    assert harvest.parse_date("not a date") is None


def test_the_label_before_each_member_value_is_found():
    text = (
        "Patient Name: LANE, RICHARD   DOB: 04/15/1978\n"
        "MRN 993544760 | Lane Richard\n"
        "Richard said he was fine\n"  # first name alone: not the member
        "Visit 05/01/2024\n"  # another date: not the DOB
    )
    hits = harvest.member_hits(text, MEMBER, 4)
    assert ("name", "Patient Name", "Patient Name: LANE, RICHARD DOB: 04/15/1978") in hits
    assert any(f == "dob" and label.endswith("DOB") for f, label, _ in hits)
    assert any(f == "member_id" and label == "MRN" for f, label, _ in hits)
    # After a table cell: no words before the name in its cell.
    assert ("name", "", "MRN 993544760 | Lane Richard") in hits
    assert sum(1 for f, *_ in hits if f == "dob") == 1
    assert sum(1 for f, *_ in hits if f == "name") == 2


def test_the_member_preset_names_the_three_fields():
    assert harvest.wanted_fields("member") == {"name", "dob", "member_id"}  # staging ids
