"""Tests for run/batch inference from path segments."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core-pipeline"
for path in (str(CORE), str(ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from db.path_ids import infer_run_batch_from_path, resolve_run_batch  # noqa: E402


def test_run_batch_either_order():
    assert infer_run_batch_from_path("Raw_Input/Run1/Batch1/DEID_PNGs") == ("R1", "B1")
    assert infer_run_batch_from_path("Batch1/Run1") == ("R1", "B1")
    assert infer_run_batch_from_path("run_2", "batch_3") == ("R2", "B3")


def test_short_forms():
    assert infer_run_batch_from_path("R12/B4") == ("R12", "B4")


def test_explicit_wins():
    assert resolve_run_batch("R9", None, "Run1/Batch1") == ("R9", "B1")
    assert resolve_run_batch(None, "B8", "Run1/Batch1") == ("R1", "B8")


def test_missing_segments():
    assert infer_run_batch_from_path("Raw_Input/DEID_PNGs") == (None, None)
