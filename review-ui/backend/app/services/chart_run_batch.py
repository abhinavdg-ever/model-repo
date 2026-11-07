"""Optional Postgres lookups for Local Mode (Run/Batch + Manifest).

When ``DATABASE_URL`` is set, landing Run/Batch and imaging Manifest Details
prefer ``chart_list`` / ``manifest_member_list``, then fall back to
``metadata_R*_B*.csv``. Failures are soft — local disk UI still works without
a database.
"""

from __future__ import annotations

import logging
from typing import Optional

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


