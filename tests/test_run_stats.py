"""2026-11-01 (user): per-chart and per-hour run statistics written next to the logs
(core-pipeline/orchestrator/run_stats.py)."""
import csv
from datetime import datetime, timedelta, timezone

from orchestrator import run_stats

T0 = datetime(2026, 11, 1, 10, 50, tzinfo=timezone.utc)


def _results(ocr=True, azure_pages=5, pages=20):
    final2 = {"used_azure": True, "pages_done": azure_pages} if ocr else {"skipped": True, "reason": "skip_ocr_disk"}
    return {"images": {"page_count": pages},
            "stages": {"ocr_prelim:1": {} if ocr else {"skipped": True}, "ocr_final2:1": final2}}


def test_a_run_writes_its_line_and_both_csvs(tmp_path, monkeypatch):
    monkeypatch.setattr(run_stats, "stats_dir", lambda: tmp_path)
    chart = {"chart_name": "c1", "id": 1}
    run_stats.record(chart, _results(), started=T0, ended=T0 + timedelta(minutes=20), status="completed")
    run_stats.record({"chart_name": "c2", "id": 2}, _results(ocr=False, pages=10),
                     started=T0 + timedelta(minutes=5), ended=T0 + timedelta(minutes=6), status="completed")
    run_stats.record({"chart_name": "c3", "id": 3}, {}, started=T0 + timedelta(hours=1),
                     ended=T0 + timedelta(hours=1, minutes=1), status="failed", error="boom")

    charts = list(csv.DictReader(open(tmp_path / "run_stats_charts.csv", encoding="utf-8")))
    assert [(c["chart_name"], c["ocr"], c["pages"], c["docintel_pages"]) for c in charts] == [
        ("c1", "yes", "20", "5"), ("c2", "no", "10", "0"), ("c3", "no", "0", "0")]
    assert charts[0]["seconds"] == "1200.0" and charts[0]["seconds_per_page"] == "60.0"

    hourly = {(r["hour"], r["ocr"]): r for r in csv.DictReader(open(tmp_path / "run_stats_hourly.csv", encoding="utf-8"))}
    ten, eleven = "2026-11-01T10:00", "2026-11-01T11:00"
    assert hourly[(ten, "yes")]["charts_started"] == "1" and hourly[(ten, "yes")]["charts_ended"] == "0"
    assert hourly[(eleven, "yes")]["charts_ended"] == "1"  # c1 ended at 11:10
    assert hourly[(ten, "all")]["charts_started"] == "2" and hourly[(ten, "all")]["pages"] == "30"
    assert hourly[(ten, "all")]["docintel_pages"] == "5"
    assert hourly[(eleven, "no")]["charts_failed"] == "1"


def test_stats_never_raise(monkeypatch):
    monkeypatch.setattr(run_stats, "stats_dir", lambda: (_ for _ in ()).throw(OSError("read-only")))
    run_stats.record({"chart_name": "c"}, {}, started=T0, ended=T0, status="completed")
