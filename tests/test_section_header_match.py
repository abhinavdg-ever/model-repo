"""Unit tests for Final1 section_headers semantic filter (lexical path)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


@pytest.fixture(autouse=True)
def _lexical_only(monkeypatch):
    """Unit tests never load MiniLM (slow / may hang without weights)."""
    import stages.lib.imaging.section_header_match as m

    monkeypatch.setattr(m, "_model_tried", True)
    monkeypatch.setattr(m, "_model", None)
    monkeypatch.setattr(m, "_canon_embeddings", None)
    monkeypatch.setattr(m, "_canon_embeddings_len", 0)


def test_exact_canonical_passes_threshold():
    from stages.lib.imaging.section_header_match import filter_section_headers

    candidates = [
        {
            "text": "Chief Complaint",
            "level": 2,
            "norm": {"left": 0.1, "top": 0.1, "width": 0.4, "height": 0.02},
        },
        {"text": "Patient ate lunch at noon and feels fine overall today", "level": 2},
    ]
    kept = filter_section_headers(
        candidates, threshold=0.90, enabled=True, use_minilm=False
    )
    assert len(kept) == 1
    assert kept[0]["text"] == "Chief Complaint"
    assert kept[0]["match_score"] >= 0.90
    assert kept[0]["matched_canonical"]


def test_punctuation_variant_passes():
    from stages.lib.imaging.section_header_match import best_header_match

    score, label = best_header_match(
        "History of Present Illness:", use_minilm=False
    )
    assert score >= 0.90
    assert "Present Illness" in label or label == "HPI" or "History" in label


def test_body_sentence_rejected():
    from stages.lib.imaging.section_header_match import best_header_match

    score, _ = best_header_match(
        "The patient reports intermittent chest pain for three days",
        use_minilm=False,
    )
    assert score < 0.90


def test_bold_form_labels_below_threshold():
    """Docling marks many bold lines as section_header — list match must drop them."""
    from stages.lib.imaging.section_header_match import filter_section_headers

    candidates = [
        {"text": "PATIENT DATA"},
        {"text": "Patient Name"},
        {"text": "Home Phone"},
        {"text": "Cleveland Clinic"},
        {"text": "Gender: F"},
        {"text": "EMERGENCY CONTACT"},
        {"text": "GUARANTOR"},
    ]
    kept = filter_section_headers(
        candidates, threshold=0.90, enabled=True, use_minilm=False
    )
    texts = {k["text"] for k in kept}
    assert texts == {"PATIENT DATA", "EMERGENCY CONTACT", "GUARANTOR"}
    for k in kept:
        assert k["match_score"] >= 0.90


def test_short_token_containment_does_not_pass_plan():
    from stages.lib.imaging.section_header_match import best_header_match

    score, _ = best_header_match("Plan of day", use_minilm=False)
    assert score < 0.90


def test_short_ocr_cannot_match_longer_canon_label():
    """``Note:`` / ``Notes`` must not claim the catalog phrase ``ED Note``."""
    from stages.lib.imaging.section_header_match import best_header_match

    for text in ("Note:", "Note", "Notes", "notes"):
        score, label = best_header_match(text, use_minilm=False)
        assert score < 0.90, (text, score, label)

    score, label = best_header_match("ED Note", use_minilm=False)
    assert score >= 0.90
    assert label == "ED Note"


def test_longer_ocr_can_match_shorter_canon_label():
    """``QB Problem List`` may match catalog ``Problem List`` (OCR longer)."""
    from stages.lib.imaging.section_header_match import best_header_match

    score, label = best_header_match("QB Problem List", use_minilm=False)
    assert score >= 0.90
    assert label == "Problem List"


def test_filter_disabled_keeps_all():
    from stages.lib.imaging.section_header_match import filter_section_headers

    candidates = [
        {"text": "Chief Complaint"},
        {"text": "random body text that is not a header at all"},
    ]
    kept = filter_section_headers(candidates, enabled=False, use_minilm=False)
    assert len(kept) == 2
    assert "match_score" not in kept[0]


def test_abbreviation_hpi():
    from stages.lib.imaging.section_header_match import best_header_match

    score, label = best_header_match("HPI", use_minilm=False)
    assert score >= 0.90
    assert label


def test_catalog_hot_reload(tmp_path, monkeypatch):
    """Editing section_header_canon.json is picked up on the next filter call."""
    import time

    import stages.lib.imaging.section_header_match as m

    canon = tmp_path / "section_header_canon.json"
    canon.write_text(json.dumps(["Chief Complaint"]), encoding="utf-8")
    monkeypatch.setattr(m, "_CANON_PATH", canon)
    m._canon = []
    m._canon_norm = []
    m._canon_mtime = None
    m._canon_embeddings = None
    m._canon_embeddings_len = 0

    kept = m.filter_section_headers(
        [{"text": "Chief Complaint"}, {"text": "Guarantor"}],
        threshold=0.90,
        enabled=True,
        use_minilm=False,
    )
    assert [k["text"] for k in kept] == ["Chief Complaint"]

    time.sleep(0.05)
    canon.write_text(json.dumps(["Guarantor"]), encoding="utf-8")
    kept2 = m.filter_section_headers(
        [{"text": "Chief Complaint"}, {"text": "Guarantor"}],
        threshold=0.90,
        enabled=True,
        use_minilm=False,
    )
    assert [k["text"] for k in kept2] == ["Guarantor"]
