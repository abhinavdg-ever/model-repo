"""The database for db-utilities: one connection per job, and the manifest upsert.

Reads DATABASE_URL (plain postgresql:// or postgresql+psycopg://) from the environment
or db-utilities/.env. Shares nothing with core-pipeline but the tables (schema/schema.sql).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass


def database_url() -> str:
    url = (os.environ.get("DATABASE_URL") or "").strip()
    for prefix in ("postgresql+psycopg://", "postgres+psycopg://", "postgresql+psycopg2://", "postgres+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    if not url:
        raise RuntimeError("DATABASE_URL is not set (db-utilities/.env)")
    return url


@contextmanager
def connect() -> Iterator[Any]:
    """One connection; commits on a clean exit, rolls back on an exception. With
    DB_BACKEND=cosmos there is no Postgres connection: yields None and the writes go to
    Cosmos (dbutil/cosmos.py)."""
    from dbutil.cosmos import backend

    if backend() == "cosmos":
        yield None
        return
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(database_url(), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# manifest_member_list upsert (moved from core-pipeline/common/db, 2026-11-07)
# ---------------------------------------------------------------------------

MANIFEST_UPSERT_BATCH = 100

_MANIFEST_VALUE = "(%s, %s, %s, %s, %s, %s::date, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"

# Chart metadata columns carried from the client's sheet; a blank value keeps what is stored.
MANIFEST_EXTRA_COLUMNS = (
    "client_id", "project_id", "project_scope", "source_name", "rr_method", "measure", "service_note",
)

# Databases created before project_scope (2026-10-21) gain it on the first manifest load.
_ADD_PROJECT_SCOPE = "ALTER TABLE manifest_member_list ADD COLUMN IF NOT EXISTS project_scope VARCHAR(255)"

_MANIFEST_UPDATE = """
            member_name      = EXCLUDED.member_name,
            first_name       = COALESCE(EXCLUDED.first_name, manifest_member_list.first_name),
            middle_name      = COALESCE(EXCLUDED.middle_name, manifest_member_list.middle_name),
            last_name        = COALESCE(EXCLUDED.last_name, manifest_member_list.last_name),
            member_dob       = COALESCE(EXCLUDED.member_dob, manifest_member_list.member_dob),
            run_id           = COALESCE(EXCLUDED.run_id, manifest_member_list.run_id),
            batch_id         = COALESCE(EXCLUDED.batch_id, manifest_member_list.batch_id),
            source_file      = COALESCE(EXCLUDED.source_file, manifest_member_list.source_file),
            source_path      = COALESCE(EXCLUDED.source_path, manifest_member_list.source_path),
            client_id        = COALESCE(EXCLUDED.client_id, manifest_member_list.client_id),
            project_id       = COALESCE(EXCLUDED.project_id, manifest_member_list.project_id),
            project_scope    = COALESCE(EXCLUDED.project_scope, manifest_member_list.project_scope),
            source_name      = COALESCE(EXCLUDED.source_name, manifest_member_list.source_name),
            rr_method        = COALESCE(EXCLUDED.rr_method, manifest_member_list.rr_method),
            measure          = COALESCE(EXCLUDED.measure, manifest_member_list.measure),
            service_note     = COALESCE(EXCLUDED.service_note, manifest_member_list.service_note),
            updated_at       = now()
"""


def _manifest_conflict(has_id: bool) -> str:
    if has_id:
        return (
            "(record_id, external_member_id) "
            "WHERE external_member_id IS NOT NULL AND external_member_id <> ''"
        )
    return (
        "(record_id, lower(member_name), COALESCE(member_dob, 'epoch'::date)) "
        "WHERE external_member_id IS NULL OR external_member_id = ''"
    )


def _manifest_row_params(m: dict[str, Any], *, has_id: bool) -> tuple[Any, ...]:
    ext = (m.get("external_member_id") or "").strip() or None
    return (
        m["record_id"],
        m["member_name"],
        m.get("first_name"),
        m.get("middle_name"),
        m.get("last_name"),
        m.get("member_dob"),
        ext if has_id else None,
        m.get("run_id"),
        m.get("batch_id"),
        m.get("source_file"),
        m.get("source_path"),
        *((m.get(col) or None) for col in MANIFEST_EXTRA_COLUMNS),
    )


def _manifest_dedupe_key(m: dict[str, Any], *, has_id: bool) -> tuple[Any, ...]:
    if has_id:
        return (m["record_id"], (m.get("external_member_id") or "").strip())
    dob = m.get("member_dob") or "epoch"
    return (m["record_id"], (m["member_name"] or "").casefold(), dob)


def upsert_manifest_members(conn: Any, members: list[dict[str, Any]], **kwargs: Any) -> dict[str, int]:
    """manifest_member_list upsert on the backend DB_BACKEND names (both: Postgres, then Cosmos;
    the Postgres counts are returned)."""
    from dbutil import cosmos

    stats: Optional[dict[str, int]] = None
    if cosmos.wants_postgres():
        stats = _upsert_manifest_members_pg(conn, members, **kwargs)
    if cosmos.wants_cosmos():
        cosmos_stats = cosmos.upsert_manifest_members(cosmos.sink(), members)
        stats = stats or cosmos_stats
    return stats or {"inserted": 0, "updated": 0}


def _upsert_manifest_members_pg(
    conn: Any,
    members: list[dict[str, Any]],
    *,
    batch_size: int = MANIFEST_UPSERT_BATCH,
) -> dict[str, int]:
    """Bulk upsert manifesto rows in chunks of ``batch_size`` (default 100).

    Splits MemberID vs name+DOB keys (different partial unique indexes).
    Returns ``{inserted, updated}``.
    """
    if not members:
        return {"inserted": 0, "updated": 0}

    with_id: list[dict[str, Any]] = []
    without_id: list[dict[str, Any]] = []
    for m in members:
        if bool((m.get("external_member_id") or "").strip()):
            with_id.append(m)
        else:
            without_id.append(m)

    inserted = 0
    updated = 0
    chunk_n = max(1, int(batch_size) or MANIFEST_UPSERT_BATCH)
    conn.execute(_ADD_PROJECT_SCOPE)

    for group, has_id in ((with_id, True), (without_id, False)):
        # Last row wins within a chunk — Postgres rejects double ON CONFLICT.
        deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
        for m in group:
            deduped[_manifest_dedupe_key(m, has_id=has_id)] = m
        ordered = list(deduped.values())
        conflict = _manifest_conflict(has_id)
        for i in range(0, len(ordered), chunk_n):
            chunk = ordered[i : i + chunk_n]
            values_sql = ",".join([_MANIFEST_VALUE] * len(chunk))
            params: list[Any] = []
            for m in chunk:
                params.extend(_manifest_row_params(m, has_id=has_id))
            rows = conn.execute(
                f"""
                INSERT INTO manifest_member_list (
                    record_id, member_name, first_name, middle_name, last_name,
                    member_dob, external_member_id, run_id, batch_id,
                    source_file, source_path,
                    client_id, project_id, project_scope, source_name, rr_method, measure, service_note
                ) VALUES {values_sql}
                ON CONFLICT {conflict} DO UPDATE SET
                {_MANIFEST_UPDATE}
                RETURNING id, (xmax = 0) AS inserted
                """,
                params,
            ).fetchall()
            for row in rows:
                if row["inserted"]:
                    inserted += 1
                else:
                    updated += 1

    return {"inserted": inserted, "updated": updated}
