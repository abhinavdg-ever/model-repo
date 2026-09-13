from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from extractors.rule_based.name_common import name_matches


def wrong_member_on_page(
    ocr_text: str,
    expected: dict[str, str],
    name_mode: str,
    ner_names: Sequence[str] = (),
) -> bool:
    """True when a patient-name sentence on the page names another member.

    ``ner_names`` are the people the NER pass read out of this page's
    patient-name sentences. The page carries a wrong member when there is at
    least one of them and not one verifies as the expected member.
    """
    del ocr_text  # the NER pass has already read the page
    names = [name for name in ner_names if name and name.strip()]
    if not names:
        return False
    first = expected.get("DummyFirstName", "")
    middle = expected.get("DummyMiddleName", "")
    last = expected.get("DummyLastName", "")
    return not any(
        name_matches(name, first, last, middle, name_mode) for name in names
    )
