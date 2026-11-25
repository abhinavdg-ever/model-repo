"""MEMORY_PROFILE: off does nothing, on records RAM per stage (core-pipeline/common/memory_profile.py)."""
from __future__ import annotations

import csv
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "core-pipeline"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common import memory_profile  # noqa: E402


def test_off_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMORY_PROFILE", raising=False)
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    window = memory_profile.begin("chart_a", "ocr_prelim:1")
    assert window is None
    assert memory_profile.end(window) is None
    assert list(tmp_path.iterdir()) == []


def test_on_records_a_stage_row(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_PROFILE", "true")
    monkeypatch.setenv("LOG_TO_FILE", "true")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    chart = memory_profile.begin("chart_a", "chart", pages=3)
    stage = memory_profile.begin("chart_a", "ocr_prelim:1", pages=3)
    block = bytearray(64 * 1024 * 1024)  # a spike the stage row must see
    block[::4096] = b"x" * len(block[::4096])
    time.sleep(0.2)
    del block
    row = memory_profile.end(stage, error="")
    memory_profile.end(chart)

    assert row is not None and row["stage"] == "ocr_prelim:1" and row["pages"] == 3
    if row["rss_before_mb"] is not None:  # psutil or /proc available
        assert row["spike_mb"] >= 40
    files = sorted(p.name for p in tmp_path.glob("memory_stages.*.csv"))
    assert len(files) == 1
    with (tmp_path / files[0]).open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["stage"] for r in rows] == ["ocr_prelim:1", "chart"]
    assert set(memory_profile.STAGE_COLUMNS) == set(rows[0])


def test_no_file_when_log_to_file_off(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_PROFILE", "true")
    monkeypatch.setenv("LOG_TO_FILE", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    row = memory_profile.end(memory_profile.begin("chart_b", "dos_extract:1"))
    assert row is not None
    assert not list(tmp_path.glob("memory_stages.*.csv"))
