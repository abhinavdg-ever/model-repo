"""Member key lists: DOB / Sex keys read only the date; Policy / Plan Number are member IDs."""
from __future__ import annotations

import json
from pathlib import Path

from stages.lib.extraction.member_dob.extract import nearest_date
from stages.lib.extraction.member_id.id_types import guess_id_type

EXTRACTION = Path(__file__).resolve().parents[1] / "core-pipeline" / "stages" / "lib" / "extraction"


def keys(field: str) -> set[str]:
    return {k.casefold() for k in json.loads((EXTRACTION / field / "keys.json").read_text(encoding="utf-8"))}


def test_dob_sex_keys_are_listed_and_read_only_the_date():
    assert {"dob/sex", "dob / sex", "date of birth/sex", "dob/age/sex"} <= keys("member_dob")
    for sentence, key in (
        ("DOB/Sex: 04/15/1978 F", "DOB/Sex"),
        ("DOB / Sex 04/15/1978 / Male", "DOB / Sex"),
        ("DOB/Age/Sex: 04/15/1978 48 Y F", "DOB/AGE/SEX"),
    ):
        value, _score = nearest_date(sentence, key)
        assert value and "F" not in value and "Male" not in value and "48 Y" not in value, (sentence, value)
        assert "1978" in value


def test_policy_and_plan_number_are_member_id_keys():
    assert {"policy number", "policy no", "plan number", "plan #"} <= keys("member_id")
    for key in ("Policy Number", "Policy No.", "Plan Number", "Plan #"):
        assert guess_id_type(key) == "member_id", key
