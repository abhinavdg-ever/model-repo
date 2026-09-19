"""Unit tests for Final1 section_headers semantic filter (lexical path)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))


def test_exact_canonical_passes_threshold():
    from stages.lib.imaging.section_header_match import filter_section_headers

    candidates = [
        {"text": "Chief Complaint", "level": 2, "norm": {"left": 0.1, "top": 0.1, "width": 0.4, "height": 0.02}},
        {"text": "Patient ate lunch at noon and feels fine overall today", "level": 2},
    ]
    kept = filter_section_headers(candidates, threshold=0.90, enabled=True)
    assert len(kept) == 1
    assert kept[0]["text"] == "Chief Complaint"
    assert kept[0]["match_score"] >= 0.90
    assert kept[0]["matched_canonical"]


def test_punctuation_variant_passes():
    from stages.lib.imaging.section_header_match import best_header_match

    score, label = best_header_match("History of Present Illness:")
    assert score >= 0.90
    assert "Present Illness" in label or label == "HPI" or "History" in label


def test_body_sentence_rejected():
    from stages.lib.imaging.section_header_match import best_header_match

    score, _ = best_header_match(
        "The patient reports intermittent chest pain for three days"
    )
    assert score < 0.90


def test_filter_disabled_keeps_all():
    from stages.lib.imaging.section_header_match import filter_section_headers

    candidates = [
        {"text": "Chief Complaint"},
        {"text": "random body text that is not a header at all"},
    ]
    kept = filter_section_headers(candidates, enabled=False)
    assert len(kept) == 2
    assert "match_score" not in kept[0]


def test_abbreviation_hpi():
    from stages.lib.imaging.section_header_match import best_header_match

    score, label = best_header_match("HPI")
    assert score >= 0.90
    assert label
