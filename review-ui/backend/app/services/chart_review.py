"""chart_review: a chart marked Reviewed from History (2026-10-28, user). While marked, its
ground truth is frozen: page labels, page sequence, encounters and the member decision
refuse a save until an admin clears the mark.

Also the per-user review log: page saves from page_ground_truth_entry (every manual save is
already appended there with its user), and chart-level actions -- Reviewed marked and
cleared, member decision, encounters -- from chart_review_entry. And each page's last
manual save.

Keyed by chart name, as page ground truth is, so a re-run keeps the mark.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

from app.core.schemas import ChartReview, ReviewLogEntry, ReviewLogResponse
from app.services.db import connection
from app.services.ground_truth import _chart_name, clean_label, ensure_entries

# Kept in step with schema/v1.sql and schema/changes_2026-10-28.sql. The users(id)
# reference is left out so a database without users still saves.
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS chart_review (
    id                      BIGSERIAL PRIMARY KEY,
    chart_name              VARCHAR(150) NOT NULL,
    user_id                 BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_chart_review_chart_name UNIQUE (chart_name)
);
"""

# Every chart-level review action, append-only, for the Review Log (schema/v1.sql,
# changes_2026-10-28.sql): Reviewed marked / cleared, member decision, encounters.
_CREATE_ENTRY_TABLE = """
CREATE TABLE IF NOT EXISTS chart_review_entry (
    id                      BIGSERIAL PRIMARY KEY,
    chart_name              VARCHAR(150) NOT NULL,
    action                  VARCHAR(30) NOT NULL
                            CHECK (action IN ('reviewed', 'review_cleared',
                                              'member_decision', 'encounters')),
    fields                  JSONB NOT NULL DEFAULT '[]'::jsonb,
    user_id                 BIGINT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chart_review_entry_chart_name ON chart_review_entry(chart_name);
CREATE INDEX IF NOT EXISTS idx_chart_review_entry_user_id ON chart_review_entry(user_id);
"""

# What was saved before chart_review_entry existed gets one entry, once: each Reviewed mark,
# each member decision and each chart's encounters, at the time it was saved.
_SEED_REVIEWED = """
INSERT INTO chart_review_entry (chart_name, action, user_id, created_at, updated_at)
SELECT r.chart_name, 'reviewed', r.user_id, r.updated_at, r.updated_at
  FROM chart_review r
 WHERE NOT EXISTS (SELECT 1 FROM chart_review_entry e
                    WHERE e.chart_name = r.chart_name AND e.action IN ('reviewed', 'review_cleared'))
"""

_SEED_DECISION = """
INSERT INTO chart_review_entry (chart_name, action, fields, user_id, created_at, updated_at)
SELECT g.chart_name, 'member_decision',
       jsonb_build_array(jsonb_build_object('field', 'member_decision', 'before', NULL,
                                            'after', initcap(g.member_decision))),
       g.user_id, g.updated_at, g.updated_at
  FROM chart_ground_truth g
 WHERE g.member_decision IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM chart_review_entry e
                    WHERE e.chart_name = g.chart_name AND e.action = 'member_decision')
"""

_SEED_ENCOUNTERS = """
INSERT INTO chart_review_entry (chart_name, action, fields, user_id, created_at, updated_at)
SELECT r.chart_name, 'encounters',
       jsonb_build_array(jsonb_build_object('field', 'encounters', 'before', NULL,
                                            'after', count(*) || ' encounter(s)')),
       (array_agg(r.user_id ORDER BY r.updated_at DESC))[1], max(r.updated_at), max(r.updated_at)
  FROM encounter_review r
 WHERE NOT EXISTS (SELECT 1 FROM chart_review_entry e
                    WHERE e.chart_name = r.chart_name AND e.action = 'encounters')
 GROUP BY r.chart_name
"""

_ready: set[tuple[str, str]] = set()
_lock = threading.Lock()


class ChartFrozen(RuntimeError):
    """The chart is marked Reviewed: its ground truth is not saved."""


def _ensure_table(conn, database_url: str, db_schema: str) -> None:
    key = (database_url, db_schema)
    if key in _ready:
        return
    with _lock:
        if key in _ready:
            return
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)
            cur.execute(_CREATE_ENTRY_TABLE)
            cur.execute(_SEED_REVIEWED)
            for table, seed in (("chart_ground_truth", _SEED_DECISION), ("encounter_review", _SEED_ENCOUNTERS)):
                cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
                if cur.fetchone()[0]:
                    cur.execute(seed)
        conn.commit()
        _ready.add(key)


def record_entry(
    conn,
    database_url: str,
    db_schema: str,
    chart: str,
    action: str,
    fields: list[dict[str, str | None]] | None = None,
    *,
    user_id: int | None = None,
) -> None:
    """Append one chart-level review action to chart_review_entry (the Review Log)."""
    _ensure_table(conn, database_url, db_schema)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO chart_review_entry (chart_name, action, fields, user_id) "
            "VALUES (%s, %s, %s::jsonb, %s)",
            (chart, action, json.dumps(fields or []), user_id),
        )


def _has_users(cur) -> bool:
    cur.execute("SELECT to_regclass('users') IS NOT NULL")
    row = cur.fetchone()
    return bool(row and row[0])


def _who(has_users: bool, alias: str) -> str:
    """SQL for the display name of ``alias``.user_id, or NULL without a users table."""
    if not has_users:
        return "NULL"
    return (
        f"(SELECT COALESCE(NULLIF(btrim(u.name), ''), u.username) "
        f"FROM users u WHERE u.id = {alias}.user_id)"
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def reviewed_charts(database_url: str, db_schema: str = "public") -> dict[str, ChartReview]:
    """Every chart marked Reviewed, by chart name."""
    with connection(database_url, db_schema) as conn:
        _ensure_table(conn, database_url, db_schema)
        with conn.cursor() as cur:
            who = _who(_has_users(cur), "r")
            cur.execute(f"SELECT r.chart_name, {who}, r.updated_at, r.user_id FROM chart_review r")
            rows = cur.fetchall()
    return {
        str(name): ChartReview(reviewed=True, reviewedBy=by, reviewedAt=_iso(at), reviewedById=uid)
        for name, by, at, uid in rows
    }


def load_chart_review(database_url: str, folder_id: str, db_schema: str = "public") -> ChartReview:
    chart = _chart_name(folder_id)
    with connection(database_url, db_schema) as conn:
        _ensure_table(conn, database_url, db_schema)
        with conn.cursor() as cur:
            who = _who(_has_users(cur), "r")
            cur.execute(
                f"SELECT {who}, r.updated_at, r.user_id FROM chart_review r WHERE r.chart_name = %s",
                (chart,),
            )
            row = cur.fetchone()
    if row is None:
        return ChartReview()
    return ChartReview(reviewed=True, reviewedBy=row[0], reviewedAt=_iso(row[1]), reviewedById=row[2])


def set_chart_review(
    database_url: str,
    folder_id: str,
    reviewed: bool,
    db_schema: str = "public",
    *,
    user_id: int | None = None,
) -> ChartReview:
    """Mark the chart Reviewed (freezing its ground truth), or clear the mark."""
    chart = _chart_name(folder_id)
    with connection(database_url, db_schema) as conn:
        _ensure_table(conn, database_url, db_schema)
        with conn.cursor() as cur:
            if reviewed:
                cur.execute(
                    "INSERT INTO chart_review (chart_name, user_id) VALUES (%s, %s) "
                    "ON CONFLICT (chart_name) DO NOTHING RETURNING id",
                    (chart, user_id),
                )
            else:
                cur.execute("DELETE FROM chart_review WHERE chart_name = %s RETURNING id", (chart,))
            changed = cur.fetchone() is not None
        # Only a real change is logged: marking a marked chart again is not one.
        if changed:
            record_entry(
                conn, database_url, db_schema, chart,
                "reviewed" if reviewed else "review_cleared", user_id=user_id,
            )
    return load_chart_review(database_url, folder_id, db_schema)


def ensure_not_frozen(database_url: str, folder_id: str, db_schema: str = "public") -> None:
    """Raise ChartFrozen when the chart is marked Reviewed."""
    review = load_chart_review(database_url, folder_id, db_schema)
    if review.reviewed:
        raise ChartFrozen(
            f"This chart was marked Reviewed by {review.reviewedBy or 'a reviewer'}: "
            "its ground truth is frozen. Reach out to an Admin to enable review."
        )


# Each page's newest manual save, with who made it.
_LAST_MANUAL = """
SELECT DISTINCT ON (g.page_number) g.page_number, {who}, e.created_at
  FROM page_ground_truth g
  JOIN page_ground_truth_entry e
    ON e.page_ground_truth_id = g.id AND e.entry_mode = 'manual_entry'
 WHERE g.chart_name = %s
 ORDER BY g.page_number, e.created_at DESC, e.id DESC
"""


def last_reviews(conn, database_url: str, chart: str, db_schema: str = "public") -> dict[int, tuple[str | None, str | None]]:
    """Page number -> (who, when as UTC ISO) of the page's last manual save."""
    ensure_entries(conn, database_url, db_schema)
    with conn.cursor() as cur:
        cur.execute(_LAST_MANUAL.format(who=_who(_has_users(cur), "e")), (chart,))
        rows = cur.fetchall()
    return {int(page): (by, _iso(at)) for page, by, at in rows}


# Every entry of every page the user (or anyone, for all users) saved by hand, oldest
# first, so each save can be compared with the page's labels just before it.
_LOG_ENTRIES = """
SELECT e.id, g.id, g.chart_name, g.page_number, e.entry_mode, e.labels, e.created_at,
       e.user_id, {who}
  FROM page_ground_truth_entry e
  JOIN page_ground_truth g ON g.id = e.page_ground_truth_id
 WHERE g.id IN (
       SELECT page_ground_truth_id FROM page_ground_truth_entry
        WHERE entry_mode = 'manual_entry' {user_filter}
 )
 ORDER BY g.id, e.created_at, e.id
"""


_YES = {"yes", "y", "true", "1"}
_NO = {"no", "n", "false", "0"}
# Page Details shows these empty as No and a save writes No: not a change.
_EMPTY_IS_NO = {"provider_signature", "blank_page", "junk_page", "is_invoice"}


def _canon(field: str, value: object) -> str:
    """A label as compared in the log, so it lists only values that changed: case and
    Yes / TRUE / Y spellings fold together, and an empty Signature or Blank / Junk is No.
    The client sheet's Provider Signature holds the credentials ("MD"): signed, so Yes."""
    text = (_text(value) or "").casefold()
    if not text and field in _EMPTY_IS_NO:
        return "no"
    if text in _YES:
        return "yes"
    if text in _NO:
        return "no"
    if field == "provider_signature" and text:
        return "yes"
    return text


def _same(field: str, a: object, b: object, before: dict | None = None) -> bool:
    if field == "provider_credentials" and not _text(a) and before is not None:
        # Credentials the sheet kept in Provider Signature, now in their own column.
        signature = (_text(before.get("provider_signature")) or "").casefold()
        if signature and signature not in _YES | _NO:
            a = before.get("provider_signature")
    return _canon(field, a) == _canon(field, b)


def log_entries(rows, *, user_id: int | None, all_users: bool) -> list[ReviewLogEntry]:
    """Turn every entry of the pages concerned, oldest first per page, into the user's
    manual saves, each with the fields it changed against the page's labels just before it
    (a bulk upload counts as the labels before, not as a save)."""
    items: list[ReviewLogEntry] = []
    state: dict[int, dict] = {}
    for _entry_id, page_id, chart, page_number, mode, labels, created_at, entry_user, by in rows:
        if isinstance(labels, str):
            labels = json.loads(labels)
        labels = labels or {}
        before = state.setdefault(page_id, {})
        changed = [
            {"field": key, "before": _text(before.get(key)), "after": _text(value)}
            for key, value in labels.items()
            # page_type beside encounter_type is the client sheet's Accept / Unaccept;
            # Page Details' page type is encounter_type.
            if not (key == "page_type" and "encounter_type" in labels)
            and not _same(key, before.get(key), value, before)
        ]
        before.update(labels)
        if mode != "manual_entry" or (not all_users and entry_user != user_id):
            continue
        items.append(
            ReviewLogEntry(
                chartName=str(chart),
                pageNumber=int(page_number),
                at=_iso(created_at) or "",
                user=by,
                action="edit",
                fields=changed,
            )
        )
    return items


def _text(value: object) -> str | None:
    return clean_label(None if value is None else str(value))


def review_log(
    database_url: str,
    db_schema: str = "public",
    *,
    user_id: int | None,
    all_users: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> ReviewLogResponse:
    """The user's ground-truth saves, newest first: chart, page, when, and the fields each
    changed (before -> after). ``all_users`` (admins) lists everyone's."""
    if not all_users and user_id is None:
        return ReviewLogResponse(items=[], total=0)
    params: tuple = () if all_users else (user_id,)
    user_filter = "" if all_users else "AND user_id = %s"
    with connection(database_url, db_schema) as conn:
        ensure_entries(conn, database_url, db_schema)
        _ensure_table(conn, database_url, db_schema)
        with conn.cursor() as cur:
            has_users = _has_users(cur)
            cur.execute(
                _LOG_ENTRIES.format(who=_who(has_users, "e"), user_filter=user_filter), params
            )
            rows = cur.fetchall()
            who = _who(has_users, "r")
            chart_sql = f"SELECT r.chart_name, r.created_at, r.action, r.fields, {who} FROM chart_review_entry r"
            if all_users:
                cur.execute(chart_sql)
            else:
                cur.execute(chart_sql + " WHERE r.user_id = %s", (user_id,))
            actions = cur.fetchall()
            users: list[dict[str, str | int]] = []
            if all_users and has_users:
                cur.execute(
                    "SELECT id, COALESCE(NULLIF(btrim(name), ''), username) FROM users ORDER BY 2"
                )
                users = [{"id": int(uid), "name": str(name)} for uid, name in cur.fetchall()]

    items = log_entries(rows, user_id=user_id, all_users=all_users)
    for chart, at, action, fields, by in actions:
        if isinstance(fields, str):
            fields = json.loads(fields)
        items.append(
            ReviewLogEntry(
                chartName=str(chart), at=_iso(at) or "", user=by, action=str(action),
                fields=list(fields or []),
            )
        )
    items.sort(key=lambda item: item.at, reverse=True)
    return ReviewLogResponse(
        items=items[offset: offset + limit], total=len(items), users=users
    )
