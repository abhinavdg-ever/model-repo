"""Readability v2 (2026-11-28, user; docs/specials/READABILITY_V2_LOGIC.md): R0-R5, score, confidence, review."""
from __future__ import annotations

import json
import sys
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from modules.ocr import readability as r  # noqa: E402

GOOD = ("Patient seen today for follow up of hypertension and diabetes. Blood pressure is well controlled "
        "on current medications. Continue metformin and lisinopril. Return to clinic in three months for labs.")
NOISE = " ".join(["Xq7r", "llIl1", "wmrnp", "tbqz", "rnnm", "qkkz", "Hhhx", "zzpt", "vvwq", "bcdfg"] * 3)


def prof(**kw):
    base = dict(texts={"docling": GOOD, "tesseract": GOOD}, azure_raw=None, is_visible=True, visibility_conf=0.95,
                final_quality="high", match_rate=0.9, handwritten_pct=0.0)
    base.update(kw)
    return r.profile(**base)


def test_r0_blank_is_not_applicable():
    out = r.decide_v2(prof(), blank=True, blank_confidence=1.0)
    assert out["is_readable"] is None and out["rule"] == "R0" and out["confidence"] == 1.0


def test_r1_good_text_is_yes_even_when_the_model_says_not_visible():
    p = prof(is_visible=False, visibility_conf=0.95)
    out = r.decide_v2(p)
    assert out["is_readable"] is True and out["rule"] == "R1" and out["review"] and out["confidence"] <= 0.70


def test_r3_noise_with_low_quality_is_no_and_reviewed():
    out = r.decide_v2(prof(texts={"docling": NOISE, "tesseract": "a b c"}, final_quality="low", match_rate=0.1))
    assert out["is_readable"] is False and out["rule"] == "R3" and out["review"]


def test_r2_sure_not_visible_and_short_text_is_no():
    out = r.decide_v2(prof(texts={"docling": "a few words only"}, is_visible=False, visibility_conf=0.97))
    assert out["is_readable"] is False and out["rule"] == "R2"


def test_r5_middle_band_is_yes_with_review_and_missing_values_lean_yes():
    mid = " ".join((GOOD + " " + NOISE).split()[:60])
    out = r.decide_v2(prof(texts={"docling": mid}, final_quality="medium", match_rate=None))
    assert out["is_readable"] is True and out["rule"] in ("R1", "R5")
    nothing = r.decide_v2(prof(texts={}, is_visible=None, visibility_conf=None, match_rate=None))
    assert nothing["is_readable"] is True and nothing["rule"] == "R5" and nothing["confidence"] == 0.5


def test_r4_blurred_short_page(tmp_path):
    from PIL import Image, ImageDraw, ImageFilter

    image = Image.new("L", (800, 1000), 255)
    ImageDraw.Draw(image).rectangle((100, 100, 700, 400), fill=110)
    path = tmp_path / "p.png"
    image.filter(ImageFilter.GaussianBlur(8)).save(path)
    p = prof(texts={"docling": "few words"}, image_path=path)
    out = r.decide_v2(p)
    assert out["rule"] == "R4" and out["is_readable"] is False


def test_azure_word_confidence_and_agreement_pair():
    raw = json.dumps({"content": GOOD, "pagesMeta": [{"words": [{"content": "a", "confidence": 0.9},
                                                                {"content": "b", "confidence": 0.7}]}]})
    p = prof(texts={"azuredocintel": GOOD, "docling": GOOD, "tesseract": NOISE}, azure_raw=raw)
    assert p["best_engine"] == "azuredocintel" and p["azure_word_conf"] == 0.8
    assert p["agreement_pair"] == "azuredocintel vs docling"  # the closest engine, not the 3-way average


def test_logic_switch(monkeypatch):
    monkeypatch.setenv("READABILITY_LOGIC", "shadow")
    assert r.readability_logic() == "shadow"
    monkeypatch.delenv("READABILITY_LOGIC")
    assert r.readability_logic() == "v1"
