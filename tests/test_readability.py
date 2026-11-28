"""Readability (2026-11-28, user; was Visibility): High / Low."""
from __future__ import annotations

import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.ocr.readability import gibberish, readability  # noqa: E402
from modules.ocr.routing import route  # noqa: E402

CLEAN = ("Patient seen today for follow up of hypertension and diabetes. Blood pressure is well controlled "
         "on current medications. Continue metformin and lisinopril. Return to clinic in three months for labs.")
GARBAGE = " ".join(["Xq7r", "llIl1", "wmrnp", "tbqz", "rnnm", ";:'", "qkkz", "Hhhx", "zzpt", "vvwq", "bcdfg"] * 3)


def test_gibberish_combines_dictionary_and_shape():
    clean, junk = gibberish(CLEAN), gibberish(GARBAGE)
    assert clean["gibberish"] is False and clean["dictionary_share"] > 0.8
    assert junk["gibberish"] is True and junk["score"] < 0.5
    assert gibberish("Patient name DOB")["gibberish"] is None  # under 20 tokens: not judged


def test_readability_low_needs_all_three_or_not_visible():
    assert readability(is_visible=False, final_quality="high", match_rate=0.9, tesseract_gibberish=False)[0] == "Low"
    assert readability(is_visible=True, final_quality="low", match_rate=0.3, tesseract_gibberish=True)[0] == "Low"
    for quality, rate, gib in (("medium", 0.3, True), ("low", 0.6, True), ("low", 0.3, False), ("low", None, True)):
        assert readability(is_visible=True, final_quality=quality, match_rate=rate, tesseract_gibberish=gib)[0] == "High"


def test_route_carries_the_gibberish_measure():
    d = route({"document_type": "printed"}, quality="low", settled=False, docling=CLEAN, tesseract=GARBAGE)
    assert d["tesseract_gibberish"]["gibberish"] is True
