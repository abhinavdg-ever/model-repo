"""2026-11-28 (user): text continuity is measured for any two pages, a junked short page too."""
from __future__ import annotations

import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.continuity.engine import PageInput, text_continuity  # noqa: E402


def _page(i, text, kind="main"):
    return PageInput(page_id=i, page_name=f"{i}.png", page_number=i, text=text, kind=kind)


def test_a_junked_short_page_gets_a_text_continuity_record():
    body = "the patient reports the pain in the lower back has improved since the last visit and the"
    lines = [f"Line {k} of the progress note about the lower back pain and the plan." for k in range(10)]
    first = "\n".join(["Clinic header", "Patient: Madeup"] + lines + [body, "Printed 1/2/2025", "Page 1"])
    second = "\n".join(["Clinic header", "Patient: Madeup", "medication was continued at the same dose",
                         "Printed 1/2/2025", "Page 2"])
    pages = [_page(1, first), _page(2, second, kind="junk"), _page(3, "Unrelated invoice total amount due")]
    out = text_continuity(pages, [(0, 1), (1, 2), (0, 2)])
    assert set(out) == {(0, 1), (1, 2), (0, 2)}
    assert set(out[(0, 1)]) >= {"char_similarity", "word_similarity", "wording_points", "ends_mid_sentence",
                                "starts_mid_thought"}
    assert out[(0, 1)]["starts_mid_thought"] is True
