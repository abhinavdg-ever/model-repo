"""Run statistics, written next to the logs (2026-11-01, user).

Every trigger (one ``run`` / ``batch-run`` call, API or CLI) gets an id
(``T20261101-203045-ab12``); the API returns it as ``trigger_id``. Every chart run
appends one line to ``<LOG_DIR>/run_stats.jsonl``:

    trigger_id, trigger (run / batch-run), chart, started, ended, seconds, status, ocr (did the OCR engines run, or was OCR
    reused with skip_ocr), pages, docintel_pages (pages sent to Azure Document
    Intelligence, the billed stage), only / through when the run was partial.

and then rebuilds two CSVs from that file:

* ``run_stats_charts.csv`` -- one row per chart run;
* ``run_stats_hourly.csv`` -- per trigger, per hour (the hour a chart started) and OCR
  yes / no / all, plus a whole-trigger row (hour "total"): charts started, charts ended in that hour, failed, pages, Doc Intel pages,
  total and average seconds per chart and per page.

``python cli.py stats`` rebuilds the CSVs and prints the hourly table. Writing stats
never fails a run: an error here is logged and ignored.
"""
from __future__ import annotations

import contextvars
import csv
import json
import secrets
import logging
import threading
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_TRIGGER: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar("run_trigger", default=("", ""))


def new_trigger(kind: str) -> str:
    """A fresh trigger id, bound to the current context (and to threads started with
    ``in_trigger``). One per run / batch-run call."""
    trigger_id = datetime.now().strftime("T%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    _TRIGGER.set((trigger_id, kind))
    try:  # 2026-11-07 (user): the day's first trigger deletes logs older than 7 days
        from common.logging_setup import purge_old_logs

        purge_old_logs()
    except Exception as exc:  # noqa: BLE001 - housekeeping never fails a run
        logger.warning("log purge skipped: %s", exc)
    try:  # and chart folders under core-pipeline/outputs not run for 7 days
        from apps.worker.jobs.outputs_cleanup import purge_old_charts

        purge_old_charts()
    except Exception as exc:  # noqa: BLE001
        logger.warning("outputs purge skipped: %s", exc)
    return trigger_id


def current_trigger() -> tuple[str, str]:
    return _TRIGGER.get()


def in_trigger(trigger: tuple[str, str], fn, *args, **kwargs):
    """Run ``fn`` with ``trigger`` bound: for background tasks and worker threads,
    which do not inherit the caller's context."""
    token = _TRIGGER.set(trigger)
    try:
        return fn(*args, **kwargs)
    finally:
        _TRIGGER.reset(token)
OCR_STAGES = ("ocr_prelim:1", "ocr_final1:1", "ocr_final2:1")
CHART_FIELDS = (
    "trigger_id", "trigger", "chart_name", "chart_id", "run_id", "batch_id", "started", "ended", "seconds", "status",
    "ocr", "pages", "docintel_pages", "seconds_per_page", "partial", "error",
)
HOURLY_FIELDS = (
    "trigger_id", "trigger", "hour", "ocr", "charts_started", "charts_ended", "charts_failed", "pages", "docintel_pages",
    "total_seconds", "avg_seconds_per_chart", "avg_seconds_per_page",
)


def stats_dir() -> Path:
    from common.logging_setup import resolve_log_dir

    return resolve_log_dir()


def ocr_ran(results: dict[str, Any]) -> bool:
    """True when an OCR engine actually ran (not every OCR stage skipped by skip_ocr)."""
    stages = results.get("stages") or {}
    return any(
        key in stages and not (isinstance(stages[key], dict) and stages[key].get("skipped"))
        for key in OCR_STAGES
    )


def docintel_pages(results: dict[str, Any]) -> int:
    final2 = (results.get("stages") or {}).get("ocr_final2:1")
    if not isinstance(final2, dict) or final2.get("skipped") or not final2.get("used_azure"):
        return 0
    return int(final2.get("pages_done") or 0)


def record(
    chart: dict[str, Any],
    results: dict[str, Any],
    *,
    started: datetime,
    ended: datetime,
    status: str,
    only: Optional[Iterable[str]] = None,
    through: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Append this run's line and rebuild the CSVs. Never raises."""
    try:
        seconds = round((ended - started).total_seconds(), 1)
        pages = int((results.get("images") or {}).get("page_count") or chart.get("page_count") or 0)
        partial = ",".join(only or []) or (f"through {through}" if through else "")
        trigger_id, kind = current_trigger()
        row = {
            "trigger_id": trigger_id or "untagged",
            "trigger": kind,
            "chart_name": chart.get("chart_name"),
            "chart_id": chart.get("id"),
            "run_id": chart.get("run_id"),
            "batch_id": chart.get("batch_id"),
            "started": started.isoformat(timespec="seconds"),
            "ended": ended.isoformat(timespec="seconds"),
            "seconds": seconds,
            "status": status,
            "ocr": ocr_ran(results),
            "pages": pages,
            "docintel_pages": docintel_pages(results),
            "partial": partial,
            "error": (error or "")[:300],
        }
        folder = stats_dir()
        folder.mkdir(parents=True, exist_ok=True)
        with _LOCK:
            with open(folder / "run_stats.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            write_reports(folder)
        from apps.worker import usage

        usage.write(row, results)  # run_usage: the database copy, with cost (never raises)
        logger.info(
            "run stats — %ss, %d page(s), OCR %s, Doc Intel %d page(s)",
            seconds, pages, "yes" if row["ocr"] else "no", row["docintel_pages"],
        )
    except Exception as exc:  # stats must never sink a run
        logger.warning("run stats not written: %s", exc)


def read_rows(folder: Optional[Path] = None) -> list[dict[str, Any]]:
    path = (folder or stats_dir()) / "run_stats.jsonl"
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _per_page(seconds: float, pages: int) -> Optional[float]:
    return round(seconds / pages, 2) if pages else None


def chart_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        r = dict(r)
        r["ocr"] = "yes" if r.get("ocr") else "no"
        r["seconds_per_page"] = _per_page(float(r.get("seconds") or 0), int(r.get("pages") or 0))
        out.append(r)
    return out


def hourly_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per trigger x hour x OCR (yes / no / all), plus hour "total" for the whole
    trigger. A chart counts as started in the hour it started (its pages, seconds and
    Doc Intel pages go there) and as ended in the hour it ended."""
    def hour(stamp: str) -> str:
        return (stamp or "")[:13] + ":00" if stamp else ""

    blank = lambda: {"charts_started": 0, "charts_ended": 0, "charts_failed": 0,  # noqa: E731
                     "pages": 0, "docintel_pages": 0, "total_seconds": 0.0}
    groups: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(blank)
    kinds: dict[str, str] = {}
    first: dict[str, str] = {}
    for r in rows:
        trig = r.get("trigger_id") or "untagged"
        kinds.setdefault(trig, r.get("trigger") or "")
        first[trig] = min(first.get(trig, r.get("started") or ""), r.get("started") or "")
        ocr = "yes" if r.get("ocr") else "no"
        for which in (ocr, "all"):
            for h in (hour(r.get("started")), "total"):
                g = groups[(trig, h, which)]
                g["charts_started"] += 1
                g["pages"] += int(r.get("pages") or 0)
                g["docintel_pages"] += int(r.get("docintel_pages") or 0)
                g["total_seconds"] += float(r.get("seconds") or 0)
                if r.get("status") == "failed":
                    g["charts_failed"] += 1
            for h in (hour(r.get("ended")), "total"):
                groups[(trig, h, which)]["charts_ended"] += 1
    order = {"yes": 0, "no": 1, "all": 2}

    def key(kv):
        trig, h, which = kv[0]
        return (first.get(trig, ""), trig, h == "total", h, order[which])

    out = []
    for (trig, h, which), g in sorted(groups.items(), key=key):
        started = g["charts_started"]
        out.append({
            "trigger_id": trig, "trigger": kinds.get(trig, ""), "hour": h, "ocr": which, **g,
            "total_seconds": round(g["total_seconds"], 1),
            "avg_seconds_per_chart": round(g["total_seconds"] / started, 1) if started else None,
            "avg_seconds_per_page": _per_page(g["total_seconds"], g["pages"]),
        })
    return out


def _write(path: Path, fields: Iterable[str], rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_reports(folder: Optional[Path] = None) -> dict[str, Path]:
    folder = folder or stats_dir()
    rows = read_rows(folder)
    charts, hourly = folder / "run_stats_charts.csv", folder / "run_stats_hourly.csv"
    _write(charts, CHART_FIELDS, chart_rows(rows))
    _write(hourly, HOURLY_FIELDS, hourly_rows(rows))
    return {"charts": charts, "hourly": hourly}
