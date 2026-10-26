"""review-ui's copy of the taxonomy's L1s and L2s (review-ui/frontend/src/pageTaxonomy.json)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import export_review_taxonomy as export  # noqa: E402


def test_the_review_ui_copy_is_current():
    assert export.OUT.read_text(encoding="utf-8") == export.build(), \
        "run python training/taxonomy/export_review_taxonomy.py"
