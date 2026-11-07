"""2026-11-07 (user): core-pipeline/outputs is scratch; chart folders not run for 7 days go."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from apps.worker.jobs import outputs_cleanup  # noqa: E402

OLD = 1_700_000_000  # 2023


def _chart(root: Path, name: str, *, old: bool) -> Path:
    folder = root / name
    (folder / "ocr").mkdir(parents=True)
    (folder / "ocr" / "1.json").write_text("{}", encoding="utf-8")
    if old:
        for p in (folder, folder / "ocr"):
            os.utime(p, (OLD, OLD))
    return folder


def test_charts_not_run_for_seven_days_are_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUTS_PURGE_ANYWHERE", "true")
    _chart(tmp_path, "old_chart", old=True)
    _chart(tmp_path, "new_chart", old=False)
    stamped = _chart(tmp_path, "old_but_run_today", old=True)
    outputs_cleanup.mark_chart_used("old_but_run_today", root=tmp_path)
    removed = outputs_cleanup.purge_old_charts(7, root=tmp_path, today=date.today())
    assert [p.name for p in removed] == ["old_chart"]
    assert stamped.is_dir() and (tmp_path / "new_chart").is_dir()


def test_only_once_a_day(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUTS_PURGE_ANYWHERE", "true")
    (tmp_path / ".last_purge").write_text(date.today().isoformat(), encoding="utf-8")
    _chart(tmp_path, "old_chart", old=True)
    assert outputs_cleanup.purge_old_charts(7, root=tmp_path) == []


def test_never_outside_core_pipeline_outputs(tmp_path, monkeypatch):
    monkeypatch.delenv("OUTPUTS_PURGE_ANYWHERE", raising=False)
    _chart(tmp_path, "old_chart", old=True)
    assert outputs_cleanup.purge_old_charts(7, root=tmp_path, force=True) == []
    assert (tmp_path / "old_chart").is_dir()
