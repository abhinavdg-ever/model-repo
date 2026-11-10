"""The API's view of the charts it queued (2026-11-10, user): which worker picked each one up,
and when it finished -- start, end, time taken -- in the API's own log, not only the worker's.

The worker stamps its name on the queued run when it takes the message
(``pipeline_jobs.worker_id``, queue_worker.handle_message). After a send, one background thread
reads those runs every ``POLL_SECONDS`` (one minute) and logs each change once:

    run 812: chart 52743839_44976074 PICKED UP by worker-2
    run 812: CHART COMPLETED: 52743839_44976074 | status completed | 42 page(s) | started ... | took 11m 43s | worker-2

It stops when every run it watches has finished, or after ``GIVE_UP_HOURS``. Never raises.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger("core-pipeline")

POLL_SECONDS = 60.0
GIVE_UP_HOURS = 48
FINISHED = ("completed", "failed")


def _as_local(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.astimezone() if value.tzinfo else value
    return None


def _pages(conn: Any, chart_id: Any) -> int:
    from common.db import get_chart

    if chart_id is None:
        return 0
    try:
        return int((get_chart(conn, int(chart_id)) or {}).get("page_count") or 0)
    except Exception:  # noqa: BLE001 -- a count is not worth a failed line
        return 0


def report(conn: Any, runs: dict[int, dict[str, Any]]) -> None:
    """Log what changed on each watched run since the last look. ``runs``: run id ->
    {"chart_name", "seen": set of what was already logged}; finished runs are removed."""
    from apps.worker.run_stats import completion_line
    from common.db import get_job

    for run_id in list(runs):
        item = runs[run_id]
        job = get_job(conn, run_id)
        if not job:
            continue
        chart, worker = item["chart_name"], job.get("worker_id") or "a worker"
        status = str(job.get("status") or "")
        if status in ("running", *FINISHED) and "picked" not in item["seen"] and job.get("worker_id"):
            logger.info("run %s: chart %s PICKED UP by %s", run_id, chart, worker)
            item["seen"].add("picked")
        if status in FINISHED:
            started, ended = _as_local(job.get("started_at")), _as_local(job.get("completed_at"))
            if started and ended:
                line = completion_line(chart, status, started, ended, _pages(conn, job.get("chart_id")),
                                       job.get("error_message") if status == "failed" else None)
            else:
                line = f"CHART {'FAILED' if status == 'failed' else 'COMPLETED'}: {chart} | status {status}"
            logger.info("run %s: %s | %s", run_id, line, worker)
            del runs[run_id]


def watch(queued: Iterable[dict[str, Any]], *, connect: Optional[Callable[[], Any]] = None,
          poll_seconds: float = POLL_SECONDS, give_up_hours: float = GIVE_UP_HOURS) -> Optional[threading.Thread]:
    """Start the thread that reports on ``queued`` (the items _enqueue_charts returns)."""
    runs = {int(q["run_id"]): {"chart_name": q.get("chart_name"), "seen": set()}
            for q in queued if q.get("run_id") is not None}
    if not runs:
        return None
    if connect is None:
        from common.db import connect as db_connect

        connect = db_connect

    def loop() -> None:
        deadline = time.monotonic() + give_up_hours * 3600
        while runs and time.monotonic() < deadline:
            time.sleep(poll_seconds)
            try:
                with connect() as conn:
                    report(conn, runs)
            except Exception as exc:  # noqa: BLE001 -- watching must never hurt the API
                logger.debug("run watch: %s", exc)
        if runs:
            logger.info("run watch: stopped after %sh with %d run(s) not finished", give_up_hours, len(runs))

    thread = threading.Thread(target=loop, name="run-watch", daemon=True)
    thread.start()
    return thread
