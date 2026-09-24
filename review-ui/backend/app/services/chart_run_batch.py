"""Optional chart_list.run_id / batch_id lookup for Local Mode.

Landing Run/Batch columns prefer Postgres when ``DATABASE_URL`` is set, then
fall back to ``metadata_R*_B*.csv``. Failures are soft — local disk UI still
works without a database.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("review_ui.chart_run_batch")


def psycopg_url(database_url: str) -> str:
    """Accept sqlalchemy-style postgresql+psycopg:// and plain postgresql://."""
    url = (database_url or "").strip()
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgres+psycopg://"):
        return "postgresql://" + url[len("postgres+psycopg://") :]
    return url


def database_url_usable(database_url: str | None) -> bool:
    """True when the URL looks like a real connection string (not the placeholder)."""
    url = (database_url or "").strip()
    if not url.lower().startswith("postgres"):
        return False
    # Default Settings placeholder — never attempt a connection.
    if "USER:PASSWORD@" in url or "@HOST:" in url:
        return False
    return True


def prefer_db_run_batch(
    db: tuple[Optional[str], Optional[str]] | None,
    meta: tuple[Optional[str], Optional[str]] | None,
) -> tuple[Optional[str], Optional[str]]:
    """DB wins when set; metadata fills gaps."""
    db_run, db_batch = db or (None, None)
    meta_run, meta_batch = meta or (None, None)
    return (db_run or meta_run), (db_batch or meta_batch)


def fetch_chart_run_batch_map(
    database_url: str,
    *,
    db_schema: str = "public",
) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """``chart_name → (run_id, batch_id)`` from ``chart_list``, or ``{}`` on failure."""
    if not database_url_usable(database_url):
        return {}
    schema = (db_schema or "public").strip() or "public"
    if not schema.replace("_", "").isalnum():
        logger.warning("invalid DB_SCHEMA=%r — skipping run/batch lookup", schema)
        return {}
    try:
        import psycopg
    except ImportError:
        logger.debug("psycopg not installed — run/batch stay on metadata CSV")
        return {}
    try:
        with psycopg.connect(psycopg_url(database_url)) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
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
