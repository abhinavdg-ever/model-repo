"""Duplicate Yes / May Be / No label mapping and display confidence."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "review-ui" / "backend"))

from app.services.duplicate_label import (  # noqa: E402
    duplicate_display_confidence,
    format_duplicate_label,
)


def test_duplicate_labels():
    assert format_duplicate_label(False, None) == "No"
    assert format_duplicate_label(True, 1.0) == "Yes"
    assert format_duplicate_label(True, 0.99) == "May Be"
    assert format_duplicate_label(True, 0.98) == "May Be"
    assert format_duplicate_label(True, 0.95) == "May Be"
    assert format_duplicate_label(True, 0.94) == "No"
    assert format_duplicate_label(None, 1.0) == "NA"


def test_duplicate_display_confidence():
    assert duplicate_display_confidence(False, None) == 1.0
    assert duplicate_display_confidence(True, 1.0) == 1.0
    assert abs(duplicate_display_confidence(True, 0.99) - 0.90) < 1e-9
    assert abs(duplicate_display_confidence(True, 0.98) - 0.80) < 1e-9
    assert abs(duplicate_display_confidence(True, 0.95) - 0.50) < 1e-9
    assert abs(duplicate_display_confidence(True, 0.985) - 0.85) < 1e-9
    assert duplicate_display_confidence(None, 1.0) is None
