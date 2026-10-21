"""encounter_review: a reviewer's encounters for a chart (review-ui's Encounter tab).

Page ranges with their page type, encounter type and DOS. Not ground truth and not
pipeline output: a save replaces the chart's rows, and nothing else reads them.
"""

from __future__ import annotations

import threading

from app.core.schemas import EncounterReview, EncounterReviewRow
from app.services.chart_run_batch import database_url_usable
from app.services.db import connection
from app.services.ground_truth import _chart_name, clean_label

# Kept in step with schema/v1.sql (and schema/changes_2026-10-21.sql).
# The users(id) reference is left out so a database without users still saves.
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS encounter_review (
    id                      BIGSERIAL PRIMARY KEY,
    chart_name              VARCHAR(150) NOT NULL,
    seq                     INT NOT NULL,
    start_page              INT NOT NULL CHECK (start_page >= 1),
    end_page                INT NOT NULL,
    page_type               TEXT,
    encounter_type          TEXT,
    dos_from                TEXT,
    dos_to                  TEXT,
    user_id                 BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_encounter_review_chart_name_seq UNIQUE (chart_name, seq),
    CHECK (end_page >= start_page)
);
CREATE INDEX IF NOT EXISTS idx_encounter_review_chart_name ON encounter_review(chart_name);
"""

# Per (url, schema): the table exists, and whether a users table does (for the
# saved-by name). Checked once per process, so a load is a single query.
_table_ready: dict[tuple[str, str], bool] = {}
_table_lock = threading.Lock()


def _ensure_table(conn, database_url: str, db_schema: str) -> bool:
    """Create encounter_review when missing; True when a users table exists."""
    key = (database_url, db_schema)
    if key in _table_ready:
        return _table_ready[key]
    with _table_lock:
        if key in _table_ready:
            return _table_ready[key]
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)
            cur.execute("SELECT to_regclass('users') IS NOT NULL")
            has_users = bool(cur.fetchone()[0])
        conn.commit()
        _table_ready[key] = has_users
        return has_users


_SELECT = """
SELECT r.seq, r.start_page, r.end_page, r.page_type, r.encounter_type,
       r.dos_from, r.dos_to, r.updated_at, u.username
  FROM encounter_review r
  LEFT JOIN users u ON u.id = r.user_id
 WHERE r.chart_name = %s
 ORDER BY r.seq
"""

_SELECT_NO_USERS = """
SELECT seq, start_page, end_page, page_type, encounter_type,
       dos_from, dos_to, updated_at, NULL
  FROM encounter_review
 WHERE chart_name = %s
 ORDER BY seq
"""


def _read(cur, chart: str, has_users: bool) -> EncounterReview:
    # No users table (a database from before sign-in): no saved-by name.
    cur.execute(_SELECT if has_users else _SELECT_NO_USERS, (chart,))
    rows = cur.fetchall()
    latest = max(rows, key=lambda row: row[7]) if rows else None
    return EncounterReview(
        rows=[
            EncounterReviewRow(
                seq=int(row[0]),
                startPage=int(row[1]),
                endPage=int(row[2]),
                pageType=row[3],
                encounterType=row[4],
                dosFrom=row[5],
                dosTo=row[6],
            )
            for row in rows
        ],
        savedAt=latest[7].isoformat() if latest and latest[7] else None,
        savedBy=latest[8] if latest else None,
    )


def load_encounter_review(
    database_url: str | None, folder_id: str, db_schema: str = "public"
) -> EncounterReview:
    """The chart's reviewed encounters; empty when none were saved (or no database)."""
    if not database_url or not database_url_usable(database_url):
        return EncounterReview()
    chart = _chart_name(folder_id)
    with connection(database_url, db_schema) as conn:
        has_users = _ensure_table(conn, database_url, db_schema)
        with conn.cursor() as cur:
            return _read(cur, chart, has_users)


def save_encounter_review(
    database_url: str,
    folder_id: str,
    rows: list[EncounterReviewRow],
    db_schema: str = "public",
    *,
    user_id: int | None = None,
) -> EncounterReview:
    """Replace the chart's reviewed encounters with ``rows``, numbered 1.. in page order.

    Raises ValueError for a range that does not start at page 1 or later, ends before it
    starts, or overlaps another range.
    """
    chart = _chart_name(folder_id)
    ordered = sorted(rows, key=lambda row: (row.startPage, row.endPage))
    for row in ordered:
        if row.startPage < 1 or row.endPage < row.startPage:
            raise ValueError(f"invalid page range {row.startPage}–{row.endPage}")
    for before, after in zip(ordered, ordered[1:]):
        if after.startPage <= before.endPage:
            raise ValueError(
                f"pages {before.startPage}–{before.endPage} and "
                f"{after.startPage}–{after.endPage} overlap"
            )
    with connection(database_url, db_schema) as conn:
        has_users = _ensure_table(conn, database_url, db_schema)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM encounter_review WHERE chart_name = %s", (chart,))
            for seq, row in enumerate(ordered, start=1):
                cur.execute(
                    """
                    INSERT INTO encounter_review (
                        chart_name, seq, start_page, end_page, page_type,
                        encounter_type, dos_from, dos_to, user_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        chart, seq, row.startPage, row.endPage,
                        clean_label(row.pageType), clean_label(row.encounterType),
                        clean_label(row.dosFrom), clean_label(row.dosTo), user_id,
                    ),
                )
            return _read(cur, chart, has_users)
