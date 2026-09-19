"""Unit tests for OCR text preference (final2 → final1 → prelim restricted)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
for path in (str(CORE), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from db import page_blocks_prelim  # noqa: E402
from stages._support import best_page_text  # noqa: E402


def test_prefers_final2_over_final1_and_prelim():
    assert (
        best_page_text(
            final2='{"content": "azure"}',
            final1='{"content": "docling"}',
            prelim="tesseract",
            quality_row={"printed_or_handwritten": "printed", "quality_tag": "high"},
        )
        == "azure"
    )


def test_falls_back_to_final1_when_final2_empty():
    assert (
        best_page_text(
            final2="",
            final1='{"content": "docling"}',
            prelim="tesseract",
            quality_row={"printed_or_handwritten": "printed", "quality_tag": "high"},
        )
        == "docling"
    )


def test_no_prelim_for_handwritten():
    assert (
        best_page_text(
            final2="",
            final1="",
            prelim="tesseract garbage",
            quality_row={"printed_or_handwritten": "handwritten", "quality_tag": "low"},
        )
        == ""
    )


def test_no_prelim_for_low_quality_printed():
    assert (
        best_page_text(
            final2="",
            final1="",
            prelim="tesseract",
            quality_row={"printed_or_handwritten": "printed", "quality_tag": "low"},
        )
        == ""
    )


def test_prelim_ok_for_high_quality_printed():
    assert (
        best_page_text(
            final2="",
            final1="",
            prelim="tesseract",
            quality_row={"printed_or_handwritten": "printed", "quality_tag": "high"},
        )
        == "tesseract"
    )


def test_page_blocks_prelim_matrix():
    assert page_blocks_prelim({"printed_or_handwritten": "handwritten"}) is True
    assert page_blocks_prelim({"printed_or_handwritten": "mixed"}) is True
    assert page_blocks_prelim({"quality_tag": "low"}) is True
    assert page_blocks_prelim(
        {"printed_or_handwritten": "printed", "quality_tag": "high"}
    ) is False
