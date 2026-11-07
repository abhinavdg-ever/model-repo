"""Queue worker: runs charts from the ``chart-runs`` Service Bus queue, one at a time.

    python -m apps.worker.queue_worker          (from core-pipeline/)
    docker compose up -d --build --scale worker=3

Loop: take the next available session (session = chart name), take its messages one
by one, run each exactly as POST /api/charts/run does in inline mode
(``apps.worker.chart_run.execute_run``), then release the session and take the next.

- **Lock renewal.** A session lock lasts 5 minutes; an ``AutoLockRenewer`` keeps it
  for up to WORKER_LOCK_RENEW_HOURS while the chart runs.
- **Success** → ``complete_message``; the ``pipeline_jobs`` row (``run_id``) → completed.
- **Failure** → ``abandon_message``; Service Bus delivers it again (to this or another
  worker) and dead-letters it after the queue's max delivery count (5). The job row →
  failed with the error, and back to running on the next delivery.
- **Unknown kind** (e.g. ``{"kind": "smoke"}``) → completed with a warning. A body that
  is not JSON is dead-lettered at once: no retry can fix it.
- **SIGTERM / SIGINT** → finish the current chart, then exit. A worker killed mid-chart
  loses its lock; the message comes back after 5 minutes and another worker runs it.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("core-pipeline")

SESSION_WAIT_SECONDS = 20   # how long to wait for a session before checking for stop
MESSAGE_WAIT_SECONDS = 5    # an idle session is released after this
ERROR_PAUSE_SECONDS = 10    # after a connection error, before trying again

_stop = threading.Event()


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def request_stop(*_args: Any) -> None:
    if not _stop.is_set():
        logger.info("stop requested: finishing the current chart, then exiting")
    _stop.set()


def _update_job(run_id: Optional[int], **fields: Any) -> None:
    """Best-effort job update: a database hiccup must not lose the message outcome."""
    if run_id is None:
        return
    try:
        from common.db import connect, update_job

        with connect() as conn:
            update_job(conn, int(run_id), **fields)
    except Exception:
        logger.warning("could not update run %s (%s)", run_id, fields.get("status"), exc_info=True)


def _finished_already(chart_name: str, min_run_date: Optional[str]) -> Optional[str]:
    """skip_completed, checked again when the message is taken (the chart may have finished
    since it was queued). Returns the finished status, else None."""
    from datetime import date

    from apps.worker.jobs.batch_intake import finished_chart_names

    since = date.fromisoformat(min_run_date) if min_run_date else None
    return "finished" if chart_name in finished_chart_names([chart_name], since) else None


def run_message_body(body: dict[str, Any]) -> dict[str, Any]:
    """Run one ``kind: run`` message body. Raises on failure (→ abandon)."""
    from apps.orchestrator_api.schemas import RunRequest
    from apps.worker import run_stats
    from apps.worker.chart_run import execute_run

    payload = RunRequest.model_validate(body["request"])
    run_id = body.get("run_id")
    if payload.skip_completed and _finished_already(payload.chart_name, body.get("min_run_date")):
        logger.info("skip_completed: %s is already finished, not run", payload.chart_name)
        return {"chart_name": payload.chart_name, "skipped": True}

    trigger = (body.get("trigger_id") or run_stats.new_trigger("queue"), "queue")
    return run_stats.in_trigger(
        trigger, execute_run, payload, on_chart=lambda cid: _update_job(run_id, chart_id=cid)
    )


def handle_message(
    receiver: Any,
    message: Any,
    run: Callable[[dict[str, Any]], dict[str, Any]] = run_message_body,
) -> str:
    """Process one received message and settle it. Returns what happened:
    ``completed`` / ``failed`` (abandoned) / ``ignored`` / ``dead_lettered``."""
    from common.logging_setup import reset_current_chart, set_current_chart
    from common.queue import KIND_RUN, decode

    session = getattr(message, "session_id", None) or "-"
    try:
        body = decode(message)
    except Exception as exc:
        logger.error("session %s: message is not JSON (%s); dead-lettered", session, exc)
        receiver.dead_letter_message(message, reason="bad_body", error_description=str(exc)[:500])
        return "dead_lettered"

    kind = body.get("kind")
    if kind != KIND_RUN:
        logger.warning("session %s: unknown message kind %r, completed without running", session, kind)
        receiver.complete_message(message)
        return "ignored"

    run_id = body.get("run_id")
    delivery = getattr(message, "delivery_count", None)
    token = set_current_chart(session)
    try:
        logger.info("run %s: chart %s starting (delivery %s, worker %s)", run_id, session, delivery, worker_id())
        _update_job(run_id, status="running", started=True, error_message="")
        try:
            result = run(body) or {}
        except Exception as exc:
            logger.exception("run %s: chart %s FAILED; message abandoned (delivery %s)", run_id, session, delivery)
            from apps.worker.chart_run import CREDENTIAL_HINT, is_credential_failure

            error = f"{type(exc).__name__}: {exc}"
            if is_credential_failure(exc):
                error = f"{error} — {CREDENTIAL_HINT}"
            _update_job(run_id, status="failed", completed=True,
                        error_message=f"delivery {delivery}: {error}"[:4000])
            receiver.abandon_message(message)
            return "failed"
        _update_job(run_id, status="completed", completed=True, chart_id=result.get("chart_id"))
        receiver.complete_message(message)
        logger.info("run %s: chart %s completed", run_id, session)
        return "completed"
    finally:
        reset_current_chart(token)


def serve_session(receiver: Any, run: Callable[[dict[str, Any]], dict[str, Any]] = run_message_body) -> int:
    """Take messages from one session receiver until it is idle or a stop is requested."""
    handled = 0
    while not _stop.is_set():
        messages = receiver.receive_messages(max_message_count=1, max_wait_time=MESSAGE_WAIT_SECONDS)
        if not messages:
            break
        for message in messages:
            outcome = handle_message(receiver, message, run)
            handled += 1
            if outcome == "failed":
                # Release the session so the redelivery can go to any worker, not straight back here.
                return handled
    return handled


def _is_timeout(exc: BaseException) -> bool:
    # No session available within max_wait_time. Matched by name: the tests have no SDK.
    return type(exc).__name__ in {"OperationTimeoutError", "SessionCannotBeLockedError"}


def run_forever(client_factory: Optional[Callable[[], Any]] = None, renewer_factory: Optional[Callable[[], Any]] = None) -> None:
    from common.config import SERVICEBUS_QUEUE, WORKER_LOCK_RENEW_HOURS
    from common import queue as q

    from azure.servicebus import NEXT_AVAILABLE_SESSION, AutoLockRenewer

    client_factory = client_factory or q.get_client
    renewer_factory = renewer_factory or AutoLockRenewer

    renew_seconds = WORKER_LOCK_RENEW_HOURS * 3600
    with client_factory() as client:
        renewer = renewer_factory()
        try:
            while not _stop.is_set():
                try:
                    with client.get_queue_receiver(
                        queue_name=SERVICEBUS_QUEUE,
                        session_id=NEXT_AVAILABLE_SESSION,
                        max_wait_time=SESSION_WAIT_SECONDS,
                    ) as receiver:
                        renewer.register(receiver, receiver.session, max_lock_renewal_duration=renew_seconds)
                        serve_session(receiver)
                except Exception as exc:
                    if _is_timeout(exc):
                        continue
                    logger.warning("queue receive failed (%s); retrying in %ds", exc, ERROR_PAUSE_SECONDS)
                    _stop.wait(ERROR_PAUSE_SECONDS)
        finally:
            renewer.close()


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=True)
    except ImportError:
        pass

    from common.logging_setup import configure_logging, set_worker_name

    configure_logging(logging.INFO)
    set_worker_name("queue-worker")
    from common import queue as q
    from common.config import SERVICEBUS_QUEUE, WORKER_LOCK_RENEW_HOURS

    conn_str, _ = q.settings()
    logger.info("queue worker starting (%s)", worker_id())
    logger.info("  queue       : %s on %s", SERVICEBUS_QUEUE, q.mask(conn_str))
    logger.info("  lock renew  : up to %.1f h per session", WORKER_LOCK_RENEW_HOURS)
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    if not conn_str:
        # Idle rather than exit: under `restart: unless-stopped` an exit is a restart loop.
        logger.error("SERVICEBUS_CONNECTION_STRING is not set (.env section 14): idle until stopped")
        _stop.wait()
        return
    if hasattr(signal, "SIGUSR1"):
        import faulthandler

        faulthandler.register(signal.SIGUSR1, all_threads=True)

    # Phase 8: models (and optionally canon) from Blob before anything loads them.
    from common.resources import ensure_resources

    ensure_resources()
    run_forever()
    logger.info("queue worker stopped")


if __name__ == "__main__":
    main()
