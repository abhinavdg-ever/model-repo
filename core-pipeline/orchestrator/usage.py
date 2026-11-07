"""Monitoring: what each chart run took and cost (2026-11-07, user).

One ``run_usage`` row per chart run, written when the run ends (``run_stats.record`` calls
:func:`write`):

* time: the run's seconds and each stage's (``stage_seconds``), pages, whether OCR ran;
* Azure Document Intelligence: pages sent (Final OCR 2) and their cost;
* LLM: prompt / completion tokens and cost per deployment, from :func:`add_llm_tokens`
  (nothing calls it yet; every future LLM call must).

Prices come from ``.env`` and are stored with the row:

    PRICE_CURRENCY=USD
    PRICE_DOCINTEL_PER_1000_PAGES=1.50            # prebuilt-read, Azure list price
    PRICE_LLM_<DEPLOYMENT>_IN_PER_1000=0.0025     # per 1,000 prompt tokens
    PRICE_LLM_<DEPLOYMENT>_OUT_PER_1000=0.01      # per 1,000 completion tokens

``<DEPLOYMENT>`` is the deployment name upper-cased, non-alphanumerics as ``_``. A deployment
without a price records its tokens with cost 0 and logs a warning once.

Read it back with ``python cli.py usage`` or the views ``v_run_usage_daily`` /
``v_run_usage_hourly``. Writing never fails a run. A database without the table
(``schema/migrate.sql`` not run yet) is logged once and skipped; run_stats.jsonl still has
the run.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_DOCINTEL_PRICE = "1.50"  # USD per 1,000 pages, prebuilt-read (0-1M pages tier)

_LOCK = threading.Lock()
_LLM: dict[int, dict[str, dict[str, int]]] = defaultdict(dict)
_WARNED: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key not in _WARNED:
        _WARNED.add(key)
        logger.warning(message, *args)


def _price(name: str, default: Optional[str] = None) -> Optional[Decimal]:
    raw = (os.environ.get(name) or default or "").strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except Exception:  # noqa: BLE001
        _warn_once(name, "%s=%r is not a number: no cost recorded for it", name, raw)
        return None


def currency() -> str:
    return (os.environ.get("PRICE_CURRENCY") or "USD").strip().upper()[:3] or "USD"


def docintel_price_per_1000() -> Optional[Decimal]:
    return _price("PRICE_DOCINTEL_PER_1000_PAGES", DEFAULT_DOCINTEL_PRICE)


def _deployment_key(deployment: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (deployment or "default").upper()).strip("_") or "DEFAULT"


def llm_prices(deployment: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
    key = _deployment_key(deployment)
    return (_price(f"PRICE_LLM_{key}_IN_PER_1000"), _price(f"PRICE_LLM_{key}_OUT_PER_1000"))


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def add_llm_tokens(chart_id: int, deployment: str, prompt_tokens: int, completion_tokens: int,
                   cached_tokens: int = 0) -> None:
    """Count one LLM call against the chart's current run. Safe from any thread."""
    with _LOCK:
        entry = _LLM[int(chart_id)].setdefault(
            deployment or "default", {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
        )
        entry["calls"] += 1
        entry["prompt_tokens"] += int(prompt_tokens or 0)
        entry["completion_tokens"] += int(completion_tokens or 0)
        entry["cached_tokens"] += int(cached_tokens or 0)


def take_llm(chart_id: Optional[int]) -> dict[str, dict[str, int]]:
    """The chart's LLM counts since its last run ended, and reset them."""
    if chart_id is None:
        return {}
    with _LOCK:
        return _LLM.pop(int(chart_id), {})


def build(run: dict[str, Any], results: dict[str, Any], llm: dict[str, dict[str, int]]) -> dict[str, Any]:
    """The run_usage row for one run. ``run`` is run_stats' line for it."""
    docintel_pages = int(run.get("docintel_pages") or 0)
    di_price = docintel_price_per_1000()
    docintel_cost = _money(Decimal(docintel_pages) * di_price / 1000) if di_price is not None else Decimal(0)

    llm_cost = Decimal(0)
    usage: dict[str, dict[str, Any]] = {}
    for deployment, counts in sorted(llm.items()):
        price_in, price_out = llm_prices(deployment)
        if price_in is None or price_out is None:
            _warn_once(f"llm:{deployment}", "no PRICE_LLM_%s_IN/OUT_PER_1000: its tokens are recorded "
                       "with cost 0", _deployment_key(deployment))
        cost = _money(Decimal(counts["prompt_tokens"]) * (price_in or 0) / 1000
                      + Decimal(counts["completion_tokens"]) * (price_out or 0) / 1000)
        llm_cost += cost
        usage[deployment] = {**counts, "price_in_per_1000": str(price_in) if price_in is not None else None,
                             "price_out_per_1000": str(price_out) if price_out is not None else None,
                             "cost": str(cost)}

    stage_seconds = {k: round(float(v), 1) for k, v in (results.get("stage_seconds") or {}).items()}
    return {
        "chart_id": run.get("chart_id"),
        "chart_name": run.get("chart_name") or "",
        "trigger_id": run.get("trigger_id"),
        "trigger_kind": run.get("trigger") or None,
        "status": run.get("status") or "unknown",
        "partial": run.get("partial") or None,
        "started_at": run.get("started"),
        "completed_at": run.get("ended"),
        "seconds": run.get("seconds") or 0,
        "pages": int(run.get("pages") or 0),
        "ocr_ran": bool(run.get("ocr")),
        "stage_seconds": stage_seconds,
        "docintel_pages": docintel_pages,
        "docintel_price_per_1000": di_price,
        "docintel_cost": docintel_cost,
        "llm_prompt_tokens": sum(c["prompt_tokens"] for c in llm.values()),
        "llm_completion_tokens": sum(c["completion_tokens"] for c in llm.values()),
        "llm_cost": _money(llm_cost),
        "llm_usage": usage,
        "currency": currency(),
        "error_message": run.get("error") or None,
    }


COLUMNS = (
    "chart_id", "chart_name", "trigger_id", "trigger_kind", "status", "partial", "started_at",
    "completed_at", "seconds", "pages", "ocr_ran", "stage_seconds", "docintel_pages",
    "docintel_price_per_1000", "docintel_cost", "llm_prompt_tokens", "llm_completion_tokens",
    "llm_cost", "llm_usage", "currency", "error_message",
)


def write(run: dict[str, Any], results: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Build and insert this run's row. Never raises; returns the row (None on failure)."""
    try:
        row = build(run, results, take_llm(run.get("chart_id")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("run usage not built: %s", exc)
        return None
    try:
        from db import connect, is_skip_db_write

        if is_skip_db_write():
            return row
        values = [json.dumps(row[c]) if c in ("stage_seconds", "llm_usage") else row[c] for c in COLUMNS]
        with connect() as conn:
            conn.execute(
                f"INSERT INTO run_usage ({', '.join(COLUMNS)}) VALUES ({', '.join(['%s'] * len(COLUMNS))})",
                values,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 - monitoring never fails a run
        if "run_usage" in str(exc) and "does not exist" in str(exc):
            _warn_once("table", "run_usage table missing: run schema/migrate.sql to record usage")
        else:
            logger.warning("run usage not written: %s", exc)
        return row
    logger.info(
        "usage — %ss, Doc Intel %d page(s) = %s %s, LLM %d token(s) = %s %s",
        row["seconds"], row["docintel_pages"], row["docintel_cost"], row["currency"],
        row["llm_prompt_tokens"] + row["llm_completion_tokens"], row["llm_cost"], row["currency"],
    )
    return row


def daily(days: int = 14) -> list[dict[str, Any]]:
    """The last ``days`` days of v_run_usage_daily, newest first."""
    from db import connect

    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM v_run_usage_daily WHERE day >= current_date - %s ORDER BY day DESC", (int(days),)
        )
        names = [d[0] for d in cur.description]
        return [r if isinstance(r, dict) else dict(zip(names, r)) for r in cur.fetchall()]
