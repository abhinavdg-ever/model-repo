"""2026-11-07 (user): logs older than 7 days are deleted, checked on the day's first trigger."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from logging_setup import purge_old_logs  # noqa: E402


def _files(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    for name in ("README.md", "core-pipeline.log", "run_stats.jsonl",
                 "core-pipeline.log.2026-10-30", "core-pipeline.log.2026-10-31", "core-pipeline.log.2026-11-06"):
        (tmp_path / name).write_text("x\n", encoding="utf-8")
    old = tmp_path / "stray.txt"
    old.write_text("x\n", encoding="utf-8")
    stamp = 1_700_000_000  # 2023
    os.utime(old, (stamp, stamp))
    (tmp_path / "rotation.log").write_text(
        "2026-10-29T10:00:00+00:00\tchart=a\n2026-11-05T10:00:00+00:00\tchart=b\n", encoding="utf-8")


def test_older_than_seven_days_is_deleted(tmp_path, monkeypatch):
    _files(tmp_path, monkeypatch)
    purge_old_logs(7, today=date(2026, 11, 7))
    left = sorted(p.name for p in tmp_path.iterdir())
    assert left == [".last_purge", "README.md", "core-pipeline.log", "core-pipeline.log.2026-10-31",
                    "core-pipeline.log.2026-11-06", "rotation.log", "run_stats.jsonl"]
    assert (tmp_path / "rotation.log").read_text(encoding="utf-8").count("\n") == 1


def test_only_the_first_call_of_the_day_purges(tmp_path, monkeypatch):
    _files(tmp_path, monkeypatch)
    (tmp_path / ".last_purge").write_text("2026-11-07\n", encoding="utf-8")
    assert purge_old_logs(7, today=date(2026, 11, 7)) == []
    assert (tmp_path / "core-pipeline.log.2026-10-30").exists()
    assert purge_old_logs(7, today=date(2026, 11, 8))
