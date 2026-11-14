"""2026-11-01 (user): per-chart and per-hour run statistics written next to the logs
(core-pipeline/apps/worker/run_stats.py)."""
import csv
from datetime import datetime, timedelta, timezone

from apps.worker import run_stats

T0 = datetime(2026, 11, 1, 10, 50, tzinfo=timezone.utc)


def _results(ocr=True, azure_pages=5, pages=20):
    final2 = {"used_azure": True, "pages_done": azure_pages} if ocr else {"skipped": True, "reason": "skip_ocr_disk"}
    return {"images": {"page_count": pages},
            "stages": {"ocr_prelim:1": {} if ocr else {"skipped": True}, "ocr_final2:1": final2}}


def test_a_run_writes_its_line_and_both_csvs(tmp_path, monkeypatch):
    monkeypatch.setattr(run_stats, "stats_dir", lambda: tmp_path)
    batch = ("T1", "batch-run")
    rec = lambda *a, **k: run_stats.in_trigger(batch, run_stats.record, *a, **k)  # noqa: E731
    rec({"chart_name": "c1", "id": 1}, _results(), started=T0, ended=T0 + timedelta(minutes=20), status="completed")
    rec({"chart_name": "c2", "id": 2}, _results(ocr=False, pages=10),
        started=T0 + timedelta(minutes=5), ended=T0 + timedelta(minutes=6), status="completed")
    rec({"chart_name": "c3", "id": 3}, {}, started=T0 + timedelta(hours=1),
        ended=T0 + timedelta(hours=1, minutes=1), status="failed", error="boom")
    run_stats.in_trigger(("T2", "run"), run_stats.record, {"chart_name": "c4", "id": 4}, _results(pages=4),
                         started=T0, ended=T0 + timedelta(minutes=2), status="completed")

    charts = list(csv.DictReader(open(tmp_path / "run_stats_charts.csv", encoding="utf-8")))
    assert [(c["trigger_id"], c["chart_name"], c["ocr"], c["pages"], c["docintel_pages"]) for c in charts] == [
        ("T1", "c1", "yes", "20", "5"), ("T1", "c2", "no", "10", "0"), ("T1", "c3", "no", "0", "0"),
        ("T2", "c4", "yes", "4", "5")]
    assert charts[0]["seconds"] == "1200.0" and charts[0]["seconds_per_page"] == "60.0"

    rows = list(csv.DictReader(open(tmp_path / "run_stats_hourly.csv", encoding="utf-8")))
    assert {r["trigger_id"] for r in rows} == {"T1", "T2"}
    hourly = {(r["hour"], r["ocr"]): r for r in rows if r["trigger_id"] == "T1"}
    ten, eleven = "2026-11-01T10:00", "2026-11-01T11:00"
    assert hourly[(ten, "yes")]["charts_started"] == "1" and hourly[(ten, "yes")]["charts_ended"] == "0"
    assert hourly[(eleven, "yes")]["charts_ended"] == "1"  # c1 ended at 11:10
    assert hourly[(ten, "all")]["charts_started"] == "2" and hourly[(ten, "all")]["pages"] == "30"
    assert hourly[(ten, "all")]["docintel_pages"] == "5"
    assert hourly[(eleven, "no")]["charts_failed"] == "1"
    total = hourly[("total", "all")]
    assert (total["charts_started"], total["charts_ended"], total["pages"]) == ("3", "3", "30")


def test_docintel_pages_counts_azure_calls_not_skips():
    """2026-11-14 (user): the log's Doc Intel count was wrong. A stage result's "skipped" is a
    page count, not a skipped stage; pages with no Azure call are not counted."""
    final2 = {"used_azure": True, "pages_done": 12, "azure_pages": 9, "skipped": 30}
    assert run_stats.docintel_pages({"stages": {"ocr_final2:1": final2}}) == 9
    assert run_stats.docintel_pages({"stages": {"ocr_final2:1": {"skipped": True}}}) == 0
    assert run_stats.docintel_pages({"stages": {"ocr_final2:1": {**final2, "used_azure": False}}}) == 0
    assert run_stats.ocr_ran({"stages": {"ocr_prelim:1": {"skipped": 4}}})
    assert not run_stats.ocr_ran({"stages": {"ocr_prelim:1": {"skipped": True}}})


def test_stats_never_raise(monkeypatch):
    monkeypatch.setattr(run_stats, "stats_dir", lambda: (_ for _ in ()).throw(OSError("read-only")))
    run_stats.record({"chart_name": "c"}, {}, started=T0, ended=T0, status="completed")
