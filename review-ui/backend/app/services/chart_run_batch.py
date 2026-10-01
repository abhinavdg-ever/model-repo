"""Optional Postgres lookups for Local Mode (Run/Batch + Manifest).

When ``DATABASE_URL`` is set, landing Run/Batch and imaging Manifest Details
prefer ``chart_list`` / ``manifest_member_list``, then fall back to
``metadata_R*_B*.csv``. Failures are soft — local disk UI still works without
a database.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

from app.services.db import connection, psycopg_url, valid_schema  # noqa: F401

logger = logging.getLogger("review_ui.chart_run_batch")


def database_url_usable(database_url: str | None) -> bool:
    """True when the URL looks like a real connection string (not the placeholder)."""
    url = (database_url or "").strip()
    if not url.lower().startswith("postgres"):
        return False
    # Default Settings placeholder — never attempt a connection.
    if "USER:PASSWORD@" in url or "@HOST:" in url:
        return False
    return True


# Paths with no RunN / BatchN segment store and display these.
DEFAULT_RUN_ID = "Test"
DEFAULT_BATCH_ID = "Test"


def with_run_batch_default(
    run_id: Optional[str],
    batch_id: Optional[str],
) -> tuple[str, str]:
    """Fill a missing run or batch. ``Run1`` / ``B1`` are kept; blanks become Test."""
    return (
        (run_id or "").strip() or DEFAULT_RUN_ID,
        (batch_id or "").strip() or DEFAULT_BATCH_ID,
    )


def prefer_db_run_batch(
    db: tuple[Optional[str], Optional[str]] | None,
    meta: tuple[Optional[str], Optional[str]] | None,
) -> tuple[str, str]:
    """DB wins when set; metadata fills gaps. A side still empty is Test."""
    db_run, db_batch = db or (None, None)
    meta_run, meta_batch = meta or (None, None)
    return with_run_batch_default(db_run or meta_run, db_batch or meta_batch)


def db_lookup_enabled(database_url: str, db_schema: str) -> bool:
    """True when a Postgres lookup should be attempted at all."""
    if not database_url_usable(database_url):
        return False
    try:
        valid_schema(db_schema)
    except ValueError:
        logger.warning("invalid DB_SCHEMA=%r — skipping Postgres lookup", db_schema)
        return False
    return True


def fetch_chart_run_batch_map(
    database_url: str,
    *,
    db_schema: str = "public",
) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """``chart_name → (run_id, batch_id)`` from ``chart_list``, or ``{}`` on failure."""
    if not db_lookup_enabled(database_url, db_schema):
        return {}
    try:
        with connection(database_url, db_schema) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT chart_name, run_id, batch_id
                      FROM chart_list
                     WHERE run_id IS NOT NULL OR batch_id IS NOT NULL
                    """
                )
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("chart_list run/batch lookup failed: %s", exc)
        return {}

    out: dict[str, tuple[Optional[str], Optional[str]]] = {}
    for chart_name, run_id, batch_id in rows:
        name = str(chart_name or "").strip()
        if not name:
            continue
        out[name] = (
            str(run_id).strip() if run_id else None,
            str(batch_id).strip() if batch_id else None,
        )
    return out


def _fmt_manifest_dob(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    return text or None


def fetch_manifest_for_record(
    database_url: str,
    record_id: str,
    *,
    db_schema: str = "public",
) -> dict[str, Optional[str]] | None:
    """``{member, dob, memberId}`` from ``manifest_member_list``, or None."""
    rid = (record_id or "").strip()
    if not rid:
        return None
    if not db_lookup_enabled(database_url, db_schema):
        return None
    try:
        with connection(database_url, db_schema) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT member_name, member_dob, external_member_id
                      FROM manifest_member_list
                     WHERE record_id = %s
                     ORDER BY id
                     LIMIT 1
                    """,
                    (rid,),
                )
                row = cur.fetchone()
    except Exception as exc:
        logger.warning("manifest_member_list lookup failed for %s: %s", rid, exc)
        return None
    if not row:
        return None
    name, dob, member_id = row
    member = str(name).strip() if name else None
    mid = str(member_id).strip() if member_id else None
    dob_s = _fmt_manifest_dob(dob)
    if not member and not dob_s and not mid:
        return None
    return {"member": member, "dob": dob_s, "memberId": mid}


def prefer_db_manifest(
    db: dict[str, Optional[str]] | None,
    csv: dict[str, Optional[str]] | None,
) -> dict[str, Optional[str]]:
    """DB wins per field when set; CSV fills gaps."""
    db = db or {}
    csv = csv or {}
    return {
        "member": db.get("member") or csv.get("member"),
        "dob": db.get("dob") or csv.get("dob"),
        "memberId": db.get("memberId") or csv.get("memberId"),
    }
