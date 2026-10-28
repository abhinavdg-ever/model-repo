"""chart_ground_truth: the reviewer's chart-level ground truth -- today the member decision,
Accept or Reject, from review-ui's Rejection Rules tab (2026-10-28, user).

Keyed by chart name, as page ground truth is, so a re-run keeps it.
"""

from __future__ import annotations

import threading

from app.core.schemas import ChartGroundTruth
from app.services.db import connection
from app.services.ground_truth import _chart_name, clean_label

# Kept in step with schema/v1.sql (and schema/changes_2026-10-27.sql). The users(id)
# reference is left out so a database without users still saves.
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS chart_ground_truth (
    id                      BIGSERIAL PRIMARY KEY,
    chart_name              VARCHAR(150) NOT NULL,
    member_decision         VARCHAR(10) CHECK (member_decision IS NULL OR
                                               member_decision IN ('accept','reject')),
    member_decision_reason  TEXT,
    user_id                 BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_chart_ground_truth_chart_name UNIQUE (chart_name)
);
"""

_ready: set[tuple[str, str]] = set()
_lock = threading.Lock()


def _ensure_table(conn, database_url: str, db_schema: str) -> None:
    key = (database_url, db_schema)
    if key in _ready:
        return
    with _lock:
        if key in _ready:
            return
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)
        conn.commit()
        _ready.add(key)


_SELECT = """
SELECT g.member_decision, g.member_decision_reason, g.updated_at,
       (SELECT u.username FROM users u WHERE u.id = g.user_id)
  FROM chart_ground_truth g
 WHERE g.chart_name = %s
"""

_UPSERT = """
INSERT INTO chart_ground_truth (chart_name, member_decision, member_decision_reason, user_id)
VALUES (%s, %s, %s, %s)
ON CONFLICT (chart_name) DO UPDATE SET
    member_decision = EXCLUDED.member_decision,
    member_decision_reason = EXCLUDED.member_decision_reason,
    user_id = EXCLUDED.user_id,
    updated_at = now()
"""


def member_decision(value: str | None) -> str | None:
    """'accept' / 'reject', or None to clear; anything else is refused."""
    text = (clean_label(value) or "").lower()
    if not text:
        return None
    if text not in {"accept", "reject"}:
        raise ValueError("the member decision is Accept or Reject")
    return text


def load_chart_ground_truth(database_url: str, folder_id: str, db_schema: str = "public") -> ChartGroundTruth:
    chart = _chart_name(folder_id)
    with connection(database_url, db_schema) as conn:
        _ensure_table(conn, database_url, db_schema)
        with conn.cursor() as cur:
            try:
                cur.execute(_SELECT, (chart,))
            except Exception:  # a database without users: no saved-by name
                conn.rollback()
                cur.execute(
                    "SELECT member_decision, member_decision_reason, updated_at, NULL "
                    "FROM chart_ground_truth WHERE chart_name = %s",
                    (chart,),
                )
            row = cur.fetchone()
    if row is None:
        return ChartGroundTruth()
    return ChartGroundTruth(
        memberDecision=row[0],
        memberDecisionReason=row[1],
        savedAt=row[2].isoformat() if row[2] else None,
        savedBy=row[3],
    )


def save_chart_ground_truth(
    database_url: str,
    folder_id: str,
    body: ChartGroundTruth,
    db_schema: str = "public",
    *,
    user_id: int | None = None,
) -> ChartGroundTruth:
    from app.services.chart_review import record_entry

    chart = _chart_name(folder_id)
    decision = member_decision(body.memberDecision)
    reason = clean_label(body.memberDecisionReason)
    with connection(database_url, db_schema) as conn:
        _ensure_table(conn, database_url, db_schema)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT member_decision, member_decision_reason FROM chart_ground_truth "
                "WHERE chart_name = %s",
                (chart,),
            )
            before = cur.fetchone() or (None, None)
            cur.execute(_UPSERT, (chart, decision, reason, user_id))
        # The Review Log: what the save changed, Accept / Reject as the tab shows them.
        changed = [
            {"field": field, "before": shown(old), "after": shown(new)}
            for field, old, new, shown in (
                ("member_decision", before[0], decision, _decision_label),
                ("member_decision_reason", before[1], reason, lambda v: v),
            )
            if (old or None) != (new or None)
        ]
        if changed:
            record_entry(conn, database_url, db_schema, chart, "member_decision", changed, user_id=user_id)
    return load_chart_ground_truth(database_url, folder_id, db_schema)


def _decision_label(value: str | None) -> str | None:
    return value.capitalize() if value else None
