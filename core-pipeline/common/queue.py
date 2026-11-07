"""The ``chart-runs`` Service Bus queue (phase 4, docs/specials/PLATFORM_PLAN.md §4.3).

One message per chart, ``session_id`` = the chart name, so two runs of one chart are
processed in order and never at the same time, while different charts run in parallel.
The body is JSON::

    {"kind": "run", "run_id": 123, "trigger_id": "T20261108-...",
     "request": {<RunRequest as JSON>}, "min_run_date": null}

``run_id`` is the ``pipeline_jobs`` row the API created (stage_name ``queued_run``); the
worker moves it to running / completed / failed. Any other ``kind`` is completed with a
warning and otherwise ignored (``{"kind": "smoke"}`` is the connectivity check).

The SDK (azure-servicebus) is imported lazily, so the API in inline mode and the tests
never need it. Tests replace ``get_client`` and ``new_message`` with fakes.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Optional

logger = logging.getLogger("core-pipeline")

KIND_RUN = "run"
JOB_STAGE = "queued_run"


def mask(connection_string: str) -> str:
    """``sb://<namespace>/ (key ***)``: what may be logged about a connection string."""
    match = re.search(r"Endpoint=sb://([^/;]+)", connection_string or "")
    return f"sb://{match.group(1)}/ (key ***)" if match else ("(set, ***)" if connection_string else "(not set)")


def settings() -> tuple[str, str]:
    from common.config import SERVICEBUS_CONNECTION_STRING, SERVICEBUS_QUEUE

    return SERVICEBUS_CONNECTION_STRING, SERVICEBUS_QUEUE


def is_configured() -> bool:
    return bool(settings()[0])


def get_client() -> Any:
    """A ``ServiceBusClient`` for SERVICEBUS_CONNECTION_STRING (use it as a context manager)."""
    conn_str, _queue = settings()
    if not conn_str:
        raise RuntimeError("SERVICEBUS_CONNECTION_STRING is not set (core-pipeline/.env, section 14)")
    from azure.servicebus import ServiceBusClient

    return ServiceBusClient.from_connection_string(conn_str)


def new_message(body: dict[str, Any], session_id: str) -> Any:
    from azure.servicebus import ServiceBusMessage

    return ServiceBusMessage(
        json.dumps(body, default=str),
        session_id=session_id,
        content_type="application/json",
        subject=str(body.get("kind") or ""),
    )


def run_message(
    run_id: Optional[int],
    request: dict[str, Any],
    trigger_id: Optional[str],
    *,
    min_run_date: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "kind": KIND_RUN,
        "run_id": run_id,
        "trigger_id": trigger_id,
        "request": request,
        "min_run_date": min_run_date,
    }


def send(items: Iterable[tuple[str, dict[str, Any]]]) -> int:
    """Send ``(session_id, body)`` pairs to the queue; returns how many were sent."""
    items = list(items)
    if not items:
        return 0
    _conn, queue = settings()
    sent = 0
    with get_client() as client:
        with client.get_queue_sender(queue_name=queue) as sender:
            # One send per message, on one link: a batched send stamps the FIRST message's
            # session id on the batch envelope, and every chart here has its own session.
            for sid, body in items:
                sender.send_messages(new_message(body, sid))
                sent += 1
    logger.info("queued %d message(s) on %s", sent, queue)
    return sent


def decode(message: Any) -> dict[str, Any]:
    """A received message's JSON body (raises ValueError when it is not a JSON object)."""
    raw = message.body if hasattr(message, "body") else message
    if not isinstance(raw, (str, bytes, bytearray)):
        raw = b"".join(bytes(part) for part in raw)  # the SDK yields body sections
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("message body is not a JSON object")
    return data
