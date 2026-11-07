"""Postgres helpers for core-pipeline (schema v7).

Three things changed from the v6 helper module:

* **A connection pool.** v6 opened a fresh connection per page per stage; a
  400-page chart cost ~3000 TCP+auth round trips. ``connect()`` now leases from
  a pool and the stages hold one lease for a batch of pages.
* **page_stage_status.** The 11 ``page_list.*_status`` columns are gone. Stage
  progress is rows keyed ``(page_id, stage_name, pass_no)``, so a stage that
  runs twice is representable and adding a stage is an INSERT into
  ``pipeline_stage``, not a CHECK-constraint migration.
* **Real upserts.** ``upsert_chart`` (and the manifest upsert, now in db-utilities) were
  SELECT-then-UPDATE, which raced. They are ``ON CONFLICT`` now, against the
  unique indexes v7 adds.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
from typing import Any, Iterator, Optional, Sequence

from common.config import DATABASE_URL

from common.db.backend import StoreBackend  # noqa: E402
from common.db.memory_store import (  # noqa: E402
    MemoryStore,
    disable_skip_db_write,
    enable_skip_db_write,
    get_memory_store,
    is_skip_db_write,
    test_chart_name,
    source_record_id,
)

logger = logging.getLogger(__name__)

# Re-export for callers (cli / runner / api).
__all_memory__ = (
    "MemoryStore",
    "disable_skip_db_write",
    "enable_skip_db_write",
    "get_memory_store",
    "is_skip_db_write",
    "test_chart_name",
    "source_record_id",
)


def _mem(conn: Any) -> bool:
    """True for any non-Postgres backend (MemoryStore today, Cosmos next): see db/backend.py."""
    return isinstance(conn, StoreBackend)


# Writes that run inside their own savepoint (2026-11-04, user: one bad page must not sink a
# chart). A failed write rolls back only itself, so the caller can record the page's error
# and go on with the same connection; without it Postgres refuses every later statement of
# the transaction ("current transaction is aborted").
_WRITE_PREFIXES = ("upsert_", "insert_", "set_", "mark_", "revise_", "delete_", "clear_", "replace_", "record_")
_SAVEPOINT = "page_write"


def _dispatch(fn):
    """Route to MemoryStore.<same name> when conn is the in-memory backend; run a Postgres
    write inside its own savepoint."""
    import functools

    is_write = fn.__name__.startswith(_WRITE_PREFIXES)

    @functools.wraps(fn)
    def wrapper(conn, *args, **kwargs):
        if _mem(conn):
            method = getattr(conn, fn.__name__)
            return method(*args, **kwargs)
        if not is_write or not hasattr(conn, "execute"):
            return fn(conn, *args, **kwargs)
        conn.execute(f"SAVEPOINT {_SAVEPOINT}")
        try:
            result = fn(conn, *args, **kwargs)
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {_SAVEPOINT}")
            conn.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
            raise
        conn.execute(f"RELEASE SAVEPOINT {_SAVEPOINT}")
        return result

    return wrapper


DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN") or "1")
# Default sized for BATCH_WORKERS=4 × STAGE_WORKERS=4 + headroom. A batch that
# violates workers × STAGE_WORKERS + headroom ≤ DB_POOL_MAX is rejected at the
# API rather than discovered as a PoolTimeout under load.
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX") or "20")

_pool: Any = None
_pool_lock = threading.Lock()


def _psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError(
            "psycopg is required. Install with: pip install 'psycopg[binary]'"
        ) from exc
    return psycopg, dict_row


def _get_pool() -> Any:
    """Lazily build the connection pool; None when psycopg_pool is absent."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        try:
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError:
            logger.info(
                "psycopg_pool not installed; falling back to per-call connections. "
                "Install with: pip install 'psycopg_pool>=3.2'"
            )
            _pool = False
            return _pool
        _pool = ConnectionPool(
            DATABASE_URL,
            min_size=DB_POOL_MIN,
            max_size=DB_POOL_MAX,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        return _pool


def close_pool() -> None:
    """Close the pool. Call on process shutdown."""
    global _pool
    with _pool_lock:
        if _pool:
            _pool.close()
        _pool = None


@contextlib.contextmanager
def connect() -> Iterator[Any]:
    """Lease a connection. Commits on clean exit, rolls back on exception.

    When ``--skip-db-write`` / ``SKIP_DB_WRITE`` is active, yields the process
    MemoryStore and never opens Postgres.
    """
    if is_skip_db_write():
        yield get_memory_store()
        return
    from common.config import DB_BACKEND_ACTIVE

    if DB_BACKEND_ACTIVE == "cosmos":
        from common.db.cosmos_store import get_cosmos_store

        yield get_cosmos_store()
        return

    pool = _get_pool()
    if pool:
        with pool.connection() as conn:
            # psycopg_pool commits on clean block exit and rolls back on error.
            yield conn
        return

    psycopg, dict_row = _psycopg()
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Page quality placeholder
# ---------------------------------------------------------------------------
# There is no longer a quality placeholder. The ocr_quality stage writes a
# measured grade from quality_analyzer (0–10 → quality_score in [0,1]) plus
# quality_detail JSONB. See modules/image_preprocess/quality_analyzer.py.


def sha256_text(text: Optional[str]) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def sha256_file(path: Any, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Stage registry
# ---------------------------------------------------------------------------

PAGE_STAGE_STATUSES = ("pending", "processing", "completed", "failed", "skipped")
DONE_STATUSES = frozenset({"completed", "skipped"})


@_dispatch
def list_stages(conn: Any, *, phase1_only: bool = True) -> list[dict[str, Any]]:
    """Ordered pipeline stages, straight out of the pipeline_stage table."""
    sql = "SELECT * FROM pipeline_stage"
    if phase1_only:
        sql += " WHERE is_phase1"
    sql += " ORDER BY seq"
    return list(conn.execute(sql).fetchall())


# ---------------------------------------------------------------------------
# chart_list / page_list
# ---------------------------------------------------------------------------


@_dispatch
def upsert_chart(
    conn: Any,
    *,
    chart_name: str,
    page_count: Optional[int] = None,
    status: Optional[str] = None,
    current_stage: Optional[str] = None,
    current_pass: Optional[int] = None,
    source: Optional[str] = None,
    blob_container: Optional[str] = None,
    blob_path: Optional[str] = None,
    output_path: Optional[str] = None,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> dict[str, Any]:
    """Insert or update one chart. Atomic on the UNIQUE (chart_name) index.

    `source` never downgrades: a chart first seen by the manifest sweeper and
    later actually ingested becomes 'blob'/'local' and stays there.

    A call that changes nothing (a bulk run re-registering a chart it already has)
    does not write the row, so updated_at, which review-ui shows as Last Run At,
    keeps the chart's last real change instead of the time the batch was queued.
    """
    new = {
        "page_count": "COALESCE(EXCLUDED.page_count, chart_list.page_count)",
        "status": "COALESCE(%(status)s, chart_list.status)",
        "current_stage": "COALESCE(EXCLUDED.current_stage, chart_list.current_stage)",
        "current_pass": "COALESCE(EXCLUDED.current_pass, chart_list.current_pass)",
        "source": """CASE
                        WHEN chart_list.source = 'manifest' AND EXCLUDED.source <> 'manifest'
                        THEN EXCLUDED.source
                        WHEN chart_list.source = 'manifest' THEN chart_list.source
                        ELSE COALESCE(%(source)s, chart_list.source)
                     END""",
        "blob_container": "COALESCE(EXCLUDED.blob_container, chart_list.blob_container)",
        "blob_path": "COALESCE(EXCLUDED.blob_path, chart_list.blob_path)",
        "output_path": "COALESCE(EXCLUDED.output_path, chart_list.output_path)",
        "run_id": "COALESCE(EXCLUDED.run_id, chart_list.run_id)",
        "batch_id": "COALESCE(EXCLUDED.batch_id, chart_list.batch_id)",
    }
    columns = ", ".join(f"chart_list.{c}" for c in new)
    row = conn.execute(
        f"""
        INSERT INTO chart_list (
            chart_name, page_count, status, current_stage, current_pass,
            source, blob_container, blob_path, output_path, run_id, batch_id
        ) VALUES (
            %(chart_name)s, %(page_count)s, COALESCE(%(status)s, 'received'), %(current_stage)s,
            %(current_pass)s, COALESCE(%(source)s, 'blob'), %(blob_container)s, %(blob_path)s,
            %(output_path)s, %(run_id)s, %(batch_id)s
        )
        ON CONFLICT (chart_name) DO UPDATE SET
            {", ".join(f"{c} = {e}" for c, e in new.items())}
        WHERE ({columns}) IS DISTINCT FROM ({", ".join(new.values())})
        RETURNING *
        """,
        {
            "chart_name": chart_name, "page_count": page_count, "status": status,
            "current_stage": current_stage, "current_pass": current_pass, "source": source,
            "blob_container": blob_container, "blob_path": blob_path, "output_path": output_path,
            "run_id": run_id, "batch_id": batch_id,
        },
    ).fetchone()
    if row is None:
        # Nothing changed, so nothing was written: return the row as it is.
        row = conn.execute(
            "SELECT * FROM chart_list WHERE chart_name = %s", (chart_name,)
        ).fetchone()
    return row


@_dispatch
def set_chart_output_path(conn: Any, chart_id: int, output_path: str) -> None:
    conn.execute(
        "UPDATE chart_list SET output_path = %s WHERE id = %s",
        (output_path, chart_id),
    )


@_dispatch
def set_chart_status(
    conn: Any,
    chart_id: int,
    status: str,
    *,
    current_stage: Optional[str] = None,
    current_pass: Optional[int] = None,
) -> None:
    conn.execute(
        """
        UPDATE chart_list
           SET status = %s, current_stage = %s, current_pass = %s
         WHERE id = %s
        """,
        (status, current_stage, current_pass, chart_id),
    )


@_dispatch
def get_chart(conn: Any, chart_id: int) -> Optional[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM chart_list WHERE id = %s", (chart_id,)
    ).fetchone()


@_dispatch
def get_chart_by_name(conn: Any, chart_name: str) -> Optional[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM chart_list WHERE chart_name = %s", (chart_name,)
    ).fetchone()


@_dispatch
def upsert_pages(
    conn: Any,
    chart_id: int,
    pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """pages: [{page_name, page_number, image_sha256?, file_size_bytes?}, ...]

    New rows default to ``use_corrected=false`` and ``image_path=pages/<name>``.
    On conflict those two columns are left alone so a resume does not wipe
    values written by the quality stage.
    """
    if not pages:
        return []
    for page in pages:
        page_name = page["page_name"]
        image_path = page.get("image_path") or f"pages/{page_name}"
        use_corrected = bool(page.get("use_corrected", False))
        conn.execute(
            """
            INSERT INTO page_list (
                chart_id, page_name, page_number, image_sha256, file_size_bytes,
                use_corrected, image_path
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chart_id, page_name) DO UPDATE SET
                page_number     = EXCLUDED.page_number,
                image_sha256    = COALESCE(EXCLUDED.image_sha256, page_list.image_sha256),
                file_size_bytes = COALESCE(EXCLUDED.file_size_bytes, page_list.file_size_bytes),
                image_path      = COALESCE(page_list.image_path, EXCLUDED.image_path)
            """,
            (
                chart_id,
                page_name,
                page.get("page_number"),
                page.get("image_sha256"),
                page.get("file_size_bytes"),
                use_corrected,
                image_path,
            ),
        )
    return list_pages(conn, chart_id)


@_dispatch
def set_page_image_source(
    conn: Any,
    page_id: int,
    *,
    use_corrected: bool,
    image_path: str,
) -> None:
    """Record which workspace image stages should read for this page."""
    conn.execute(
        """
        UPDATE page_list
           SET use_corrected = %s,
               image_path    = %s
         WHERE id = %s
        """,
        (bool(use_corrected), image_path, page_id),
    )


@_dispatch
def list_pages(conn: Any, chart_id: int) -> list[dict[str, Any]]:
    return list(
        conn.execute(
            """
            SELECT * FROM page_list
            WHERE chart_id = %s
            ORDER BY page_number NULLS LAST, page_name
            """,
            (chart_id,),
        ).fetchall()
    )


# ---------------------------------------------------------------------------
# page_stage_status — per-page, per-stage, per-pass progress
# ---------------------------------------------------------------------------


@_dispatch
def init_page_stages(
    conn: Any,
    chart_id: int,
    *,
    stages: Optional[Sequence[tuple[str, int]]] = None,
) -> int:
    """Seed 'pending' rows for every (page, stage, pass). Idempotent."""
    if stages is None:
        stages = [(s["stage_name"], s["pass_no"]) for s in list_stages(conn)]
    count = 0
    for stage_name, pass_no in stages:
        result = conn.execute(
            """
            INSERT INTO page_stage_status (chart_id, page_id, stage_name, pass_no, status)
            SELECT %s, p.id, %s, %s, 'pending'
              FROM page_list p
             WHERE p.chart_id = %s
            ON CONFLICT (page_id, stage_name, pass_no) DO NOTHING
            """,
            (chart_id, stage_name, pass_no, chart_id),
        )
        count += getattr(result, "rowcount", 0) or 0
    return count


@_dispatch
def set_page_stage(
    conn: Any,
    *,
    chart_id: int,
    page_id: int,
    stage_name: str,
    status: str,
    pass_no: int = 1,
    error_message: Optional[str] = None,
    skip_reason: Optional[str] = None,
) -> None:
    if status not in PAGE_STAGE_STATUSES:
        raise ValueError(f"Invalid page stage status: {status}")
    conn.execute(
        """
        INSERT INTO page_stage_status (
            chart_id, page_id, stage_name, pass_no, status, attempt,
            error_message, skip_reason, started_at, completed_at
        ) VALUES (
            %s, %s, %s, %s, %s,
            CASE WHEN %s = 'processing' THEN 1 ELSE 0 END,
            %s, %s,
            CASE WHEN %s = 'processing' THEN now() END,
            CASE WHEN %s IN ('completed','failed','skipped') THEN now() END
        )
        ON CONFLICT (page_id, stage_name, pass_no) DO UPDATE SET
            status        = EXCLUDED.status,
            attempt       = page_stage_status.attempt
                            + CASE WHEN EXCLUDED.status = 'processing' THEN 1 ELSE 0 END,
            error_message = EXCLUDED.error_message,
            skip_reason   = EXCLUDED.skip_reason,
            started_at    = CASE WHEN EXCLUDED.status = 'processing'
                                 THEN now() ELSE page_stage_status.started_at END,
            completed_at  = CASE WHEN EXCLUDED.status IN ('completed','failed','skipped')
                                 THEN now() ELSE page_stage_status.completed_at END
        """,
        (
            chart_id, page_id, stage_name, pass_no, status,
            status,
            error_message, skip_reason,
            status, status,
        ),
    )


@_dispatch
def set_pages_stage(
    conn: Any,
    *,
    chart_id: int,
    page_ids: Sequence[int],
    stage_name: str,
    status: str,
    pass_no: int = 1,
    skip_reason: Optional[str] = None,
) -> None:
    """Bulk form of set_page_stage — one statement for a whole page set."""
    if not page_ids:
        return
    if status not in PAGE_STAGE_STATUSES:
        raise ValueError(f"Invalid page stage status: {status}")
    conn.execute(
        """
        INSERT INTO page_stage_status (
            chart_id, page_id, stage_name, pass_no, status, skip_reason, completed_at
        )
        SELECT %s, unnest(%s::bigint[]), %s, %s, %s, %s,
               CASE WHEN %s IN ('completed','failed','skipped') THEN now() END
        ON CONFLICT (page_id, stage_name, pass_no) DO UPDATE SET
            status       = EXCLUDED.status,
            skip_reason  = EXCLUDED.skip_reason,
            completed_at = CASE WHEN EXCLUDED.status IN ('completed','failed','skipped')
                                THEN now() ELSE page_stage_status.completed_at END
        """,
        (chart_id, list(page_ids), stage_name, pass_no, status, skip_reason, status),
    )


@_dispatch
def get_stage_status_map(
    conn: Any, chart_id: int, stage_name: str, pass_no: int = 1
) -> dict[int, str]:
    rows = conn.execute(
        """
        SELECT page_id, status FROM page_stage_status
         WHERE chart_id = %s AND stage_name = %s AND pass_no = %s
        """,
        (chart_id, stage_name, pass_no),
    ).fetchall()
    return {r["page_id"]: r["status"] for r in rows}


@_dispatch
def pages_needing_stage(
    conn: Any,
    chart_id: int,
    stage_name: str,
    pass_no: int = 1,
    *,
    force: bool = False,
) -> set[int]:
    """Page ids this stage still has to do.

    This is what makes a re-run resumable: pages already `completed` or
    `skipped` are not redone, so a crash on page 400 of 500 does not re-bill
    Azure Document Intelligence for the first 399. `force=True` reprocesses
    everything (the /run?force=true resume path).
    """
    if force:
        rows = conn.execute(
            "SELECT id FROM page_list WHERE chart_id = %s", (chart_id,)
        ).fetchall()
        return {r["id"] for r in rows}
    rows = conn.execute(
        """
        SELECT p.id
          FROM page_list p
          LEFT JOIN page_stage_status s
                 ON s.page_id = p.id AND s.stage_name = %s AND s.pass_no = %s
         WHERE p.chart_id = %s
           AND (s.status IS NULL OR s.status NOT IN ('completed','skipped'))
        """,
        (stage_name, pass_no, chart_id),
    ).fetchall()
    return {r["id"] for r in rows}


@_dispatch
def reset_stage(conn: Any, chart_id: int, stage_name: str, pass_no: int = 1) -> None:
    """Put a stage back to pending for every page (targeted re-run)."""
    conn.execute(
        """
        UPDATE page_stage_status
           SET status = 'pending', error_message = NULL, skip_reason = NULL,
               completed_at = NULL
         WHERE chart_id = %s AND stage_name = %s AND pass_no = %s
        """,
        (chart_id, stage_name, pass_no),
    )


@_dispatch
def reset_pages_stage(
    conn: Any,
    chart_id: int,
    page_ids: Sequence[int],
    stage_name: str,
    pass_no: int = 1,
) -> int:
    """Put selected pages back to pending for one stage (gate-delta reopen)."""
    if not page_ids:
        return 0
    result = conn.execute(
        """
        INSERT INTO page_stage_status (
            chart_id, page_id, stage_name, pass_no, status,
            error_message, skip_reason, completed_at
        )
        SELECT %s, unnest(%s::bigint[]), %s, %s, 'pending', NULL, NULL, NULL
        ON CONFLICT (page_id, stage_name, pass_no) DO UPDATE SET
            status        = 'pending',
            error_message = NULL,
            skip_reason   = NULL,
            completed_at  = NULL
        """,
        (chart_id, list(page_ids), stage_name, pass_no),
    )
    return getattr(result, "rowcount", 0) or 0


@_dispatch
def clear_stuck_processing(conn: Any, chart_id: int) -> int:
    """Turn abandoned ``processing`` page rows back to ``pending`` (resume).

    A killed worker / timeout can leave pages stuck in ``processing``; resume
    must treat them as still todo. ``pages_needing_stage`` already includes
    them; this cleans the status so the UI does not show a forever-running page.
    """
    result = conn.execute(
        """
        UPDATE page_stage_status
           SET status = 'pending', error_message = NULL, skip_reason = NULL,
               completed_at = NULL
         WHERE chart_id = %s AND status = 'processing'
        """,
        (chart_id,),
    )
    return getattr(result, "rowcount", 0) or 0


# ---------------------------------------------------------------------------
# pipeline_jobs
# ---------------------------------------------------------------------------


@_dispatch
def create_job(
    conn: Any,
    *,
    chart_id: Optional[int],
    stage_name: str,
    status: str = "queued",
    pass_no: int = 1,
    queue_name: str = "default",
    worker_id: Optional[str] = None,
    pages_total: Optional[int] = None,
) -> int:
    return conn.execute(
        """
        INSERT INTO pipeline_jobs (
            chart_id, stage_name, pass_no, status, queue_name, worker_id, pages_total
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (chart_id, stage_name, pass_no, status, queue_name, worker_id, pages_total),
    ).fetchone()["id"]


@_dispatch
def update_job(
    conn: Any,
    job_id: int,
    *,
    status: Optional[str] = None,
    error_message: Optional[str] = None,
    started: bool = False,
    completed: bool = False,
    chart_id: Optional[int] = None,
    pages_total: Optional[int] = None,
    pages_done: Optional[int] = None,
    pages_failed: Optional[int] = None,
    pages_skipped: Optional[int] = None,
) -> None:
    sets: list[str] = []
    args: list[Any] = []
    if status:
        sets.append("status = %s")
        args.append(status)
    if error_message is not None:
        sets.append("error_message = %s")
        args.append(error_message)
    if chart_id is not None:
        sets.append("chart_id = %s")
        args.append(chart_id)
    for column, value in (
        ("pages_total", pages_total),
        ("pages_done", pages_done),
        ("pages_failed", pages_failed),
        ("pages_skipped", pages_skipped),
    ):
        if value is not None:
            sets.append(f"{column} = %s")
            args.append(value)
    if started:
        sets.append("started_at = now()")
        sets.append("heartbeat_at = now()")
    if completed:
        sets.append("completed_at = now()")
    if not sets:
        return
    args.append(job_id)
    conn.execute(
        f"UPDATE pipeline_jobs SET {', '.join(sets)} WHERE id = %s",
        args,
    )


@_dispatch
def heartbeat_job(conn: Any, job_id: int, *, lease_seconds: int = 300) -> None:
    conn.execute(
        """
        UPDATE pipeline_jobs
           SET heartbeat_at = now(),
               lease_expires_at = now() + make_interval(secs => %s)
         WHERE id = %s
        """,
        (lease_seconds, job_id),
    )


# ---------------------------------------------------------------------------
# OCR + quality
# ---------------------------------------------------------------------------


@_dispatch
def upsert_ocr_result(
    conn: Any,
    *,
    chart_id: int,
    page_id: int,
    ocr_type: str,
    raw_text: Optional[str],
) -> None:
    conn.execute(
        """
        INSERT INTO ocr_results (chart_id, page_id, ocr_type, raw_text, char_count, text_sha256)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (page_id, ocr_type) DO UPDATE SET
            raw_text    = EXCLUDED.raw_text,
            char_count  = EXCLUDED.char_count,
            text_sha256 = EXCLUDED.text_sha256,
            updated_at  = now()
        """,
        (
            chart_id, page_id, ocr_type, raw_text,
            len(raw_text or ""), sha256_text(raw_text),
        ),
    )


@_dispatch
def get_ocr_texts(conn: Any, chart_id: int, ocr_type: str) -> dict[int, str]:
    """All page text for one engine in one query — no per-page file re-parsing."""
    rows = conn.execute(
        """
        SELECT page_id, raw_text FROM ocr_results
         WHERE chart_id = %s AND ocr_type = %s
        """,
        (chart_id, ocr_type),
    ).fetchall()
    return {r["page_id"]: (r["raw_text"] or "") for r in rows}


@_dispatch
def upsert_quality(
    conn: Any,
    *,
    chart_id: int,
    page_id: int,
    printed_or_handwritten: Optional[str],
    orientation_angle: Optional[float] = None,
    tilt_angle: Optional[float] = None,
    mirrored: Optional[bool] = None,
    rotation_applied: bool = False,
    hw_method: Optional[str] = None,
    hw_confidence: Optional[float] = None,
    quality_tag: Optional[str] = None,
    quality_score: Optional[float] = None,
    quality_detail: Optional[dict[str, Any]] = None,
    input_dpi: Optional[float] = None,
    document_type: Optional[str] = None,
    handwritten_probability: Optional[float] = None,
    is_visible: Optional[bool] = None,
    handwritten_area_pct: Optional[float] = None,
    review_required: Optional[bool] = None,
) -> None:
    """Upsert one page's quality / HW / page-type / rotation row."""
    import json

    detail = quality_detail if quality_detail is not None else {}
    conn.execute(
        """
        INSERT INTO ocr_quality_results (
            chart_id, page_id, quality_tag, quality_score, quality_detail,
            input_dpi, printed_or_handwritten, hw_method, hw_confidence,
            document_type, handwritten_probability, is_visible,
            handwritten_area_pct, review_required,
            orientation_angle, tilt_angle, mirrored, rotation_applied
        ) VALUES (
            %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        )
        ON CONFLICT (page_id) DO UPDATE SET
            quality_tag             = EXCLUDED.quality_tag,
            quality_score           = EXCLUDED.quality_score,
            quality_detail          = EXCLUDED.quality_detail,
            input_dpi               = EXCLUDED.input_dpi,
            printed_or_handwritten  = EXCLUDED.printed_or_handwritten,
            hw_method               = EXCLUDED.hw_method,
            hw_confidence           = EXCLUDED.hw_confidence,
            document_type           = EXCLUDED.document_type,
            handwritten_probability = EXCLUDED.handwritten_probability,
            is_visible              = EXCLUDED.is_visible,
            handwritten_area_pct    = EXCLUDED.handwritten_area_pct,
            review_required         = EXCLUDED.review_required,
            orientation_angle       = EXCLUDED.orientation_angle,
            tilt_angle              = EXCLUDED.tilt_angle,
            mirrored                = EXCLUDED.mirrored,
            rotation_applied        = EXCLUDED.rotation_applied,
            updated_at              = now()
        """,
        (
            chart_id, page_id, quality_tag, quality_score,
            json.dumps(detail, default=str),
            input_dpi,
            printed_or_handwritten, hw_method, hw_confidence,
            document_type, handwritten_probability, is_visible,
            handwritten_area_pct, review_required,
            orientation_angle, tilt_angle, mirrored, rotation_applied,
        ),
    )


@_dispatch
def get_quality_map(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM ocr_quality_results WHERE chart_id = %s", (chart_id,)
    ).fetchall()
    return {r["page_id"]: r for r in rows}


@_dispatch
def set_quality_tag(
    conn: Any,
    *,
    page_id: int,
    quality_tag: Optional[str],
    quality_detail: dict[str, Any],
    review_required: Optional[bool],
) -> None:
    """Re-grade one page after stage 1 (the Docling / Tesseract overlap check)."""
    import json

    conn.execute(
        """
        UPDATE ocr_quality_results
           SET quality_tag = %s, quality_detail = %s::jsonb, review_required = %s,
               updated_at = now()
         WHERE page_id = %s
        """,
        (quality_tag, json.dumps(quality_detail, default=str), review_required, page_id),
    )


# Page decisions read document_type. Printed and visual pages are read as
# printed text; every other type (handwritten, form, blank, uncertain) skips
# the Tesseract blank/junk pass and is judged on final OCR.
PRINTED_TYPES = frozenset({"printed", "visual"})
# A row written before document_type existed falls back to the old label.
# HW stage methods for a page with no ink: an empty sheet, or faint marks only.
NO_INK_HW_METHODS = frozenset({"blank_page", "faint_marks_only"})

_PAGE_TYPE_SQL = """lower(coalesce(
    document_type,
    CASE lower(printed_or_handwritten) WHEN 'mixed' THEN 'form'
         ELSE printed_or_handwritten END,
    ''))"""


def page_type(quality_row: Optional[dict[str, Any]]) -> str:
    """The page's document_type, lowercase; '' when it was never classified."""
    if not quality_row:
        return ""
    doc = str(quality_row.get("document_type") or "").strip().lower()
    if doc:
        return doc
    legacy = str(quality_row.get("printed_or_handwritten") or "").strip().lower()
    return "form" if legacy == "mixed" else legacy


def is_printed_type(document_type: str) -> bool:
    return document_type in PRINTED_TYPES


@_dispatch
def handwritten_page_ids(conn: Any, chart_id: int) -> set[int]:
    rows = conn.execute(
        f"""
        SELECT page_id FROM ocr_quality_results
         WHERE chart_id = %s AND {_PAGE_TYPE_SQL} = 'handwritten'
        """,
        (chart_id,),
    ).fetchall()
    return {r["page_id"] for r in rows}


@_dispatch
def non_printed_page_ids(conn: Any, chart_id: int) -> set[int]:
    """Handwritten / form / uncertain — prelim blank/junk pass 1 is skipped, and they go to
    final OCR whatever pass 1 says.

    Not a page the HW stage found no ink on (``NO_INK_HW_METHODS``), nor one typed Blank
    (2026-10-27, user): pass 1 judges it (R1: no text -> Blank) and final OCR then skips it.
    Before, such a page was skipped by pass 1, sent to final OCR (billed), and skipped again
    by pass 2 for having no text: never Blank."""
    rows = conn.execute(
        f"""
        SELECT page_id FROM ocr_quality_results
         WHERE chart_id = %s
           AND {_PAGE_TYPE_SQL} NOT IN ('', 'printed', 'visual', 'blank')
           AND lower(coalesce(hw_method, '')) <> ALL(%s)
        """,
        (chart_id, sorted(NO_INK_HW_METHODS)),
    ).fetchall()
    return {r["page_id"] for r in rows}


@_dispatch
def low_quality_page_ids(conn: Any, chart_id: int) -> set[int]:
    """Pages with measured quality_tag = low — skip blank/junk pass 1."""
    rows = conn.execute(
        """
        SELECT page_id FROM ocr_quality_results
         WHERE chart_id = %s AND lower(coalesce(quality_tag, '')) = 'low'
        """,
        (chart_id,),
    ).fetchall()
    return {r["page_id"] for r in rows}


@_dispatch
def high_quality_printed_page_ids(conn: Any, chart_id: int) -> set[int]:
    """High-quality printed pages — Azure final2 is skipped (final1 is enough). Visual
    pages are not skipped (2026-10-21): only printed + high."""
    rows = conn.execute(
        f"""
        SELECT page_id FROM ocr_quality_results
         WHERE chart_id = %s
           AND lower(coalesce(quality_tag, '')) = 'high'
           AND {_PAGE_TYPE_SQL} = 'printed'
        """,
        (chart_id,),
    ).fetchall()
    return {r["page_id"] for r in rows}


def page_blocks_prelim(
    quality_row: Optional[dict[str, Any]],
) -> bool:
    """True when prelim text must not be used (any non-printed type, or low quality)."""
    if not quality_row:
        return False
    kind = page_type(quality_row)
    tag = str(quality_row.get("quality_tag") or "").strip().lower()
    if kind and not is_printed_type(kind):
        return True
    if tag == "low":
        return True
    return False


# ---------------------------------------------------------------------------
# blank / junk / duplicate
# ---------------------------------------------------------------------------


# Databases created before duplicate_confidence (2026-10-22) gain it on the first write.
_ADD_DUPLICATE_CONFIDENCE = (
    "ALTER TABLE blank_junk_classification "
    "ADD COLUMN IF NOT EXISTS duplicate_confidence NUMERIC(5,4)"
)
_duplicate_confidence_ready = False


@_dispatch
def upsert_blank_junk(
    conn: Any,
    *,
    chart_id: int,
    page_id: int,
    blank_junk_flag: str,
    pass_no: int,
    ocr_source: str,
    junk_subtype: Optional[str] = None,
    duplicate_of_page_id: Optional[int] = None,
    confidence: Optional[float] = None,
    reason: Optional[str] = None,
    is_final: bool = False,
    duplicate_confidence: Optional[float] = None,
) -> None:
    """Write one pass's verdict. `is_final` is set separately by mark_final()
    so that exactly one row per page carries it. ``duplicate_confidence`` is the
    page's highest duplicate similarity (None when it was never compared)."""
    global _duplicate_confidence_ready
    if not _duplicate_confidence_ready:
        conn.execute(_ADD_DUPLICATE_CONFIDENCE)
        _duplicate_confidence_ready = True
    conn.execute(
        """
        INSERT INTO blank_junk_classification (
            chart_id, page_id, pass_no, blank_junk_flag, junk_subtype,
            duplicate_of_page_id, ocr_source, confidence, reason, is_final,
            duplicate_confidence
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (page_id, pass_no) DO UPDATE SET
            blank_junk_flag      = EXCLUDED.blank_junk_flag,
            junk_subtype         = EXCLUDED.junk_subtype,
            duplicate_of_page_id = EXCLUDED.duplicate_of_page_id,
            ocr_source           = EXCLUDED.ocr_source,
            confidence           = EXCLUDED.confidence,
            reason               = EXCLUDED.reason,
            duplicate_confidence = EXCLUDED.duplicate_confidence,
            updated_at           = now()
        """,
        (
            chart_id, page_id, pass_no, blank_junk_flag, junk_subtype,
            duplicate_of_page_id, ocr_source, confidence, reason, is_final,
            duplicate_confidence,
        ),
    )


@_dispatch
def delete_blank_junk(
    conn: Any,
    chart_id: int,
    pass_no: int,
    page_ids: Sequence[int],
) -> None:
    """Drop this pass's verdicts for pages the pass is skipping.

    A leftover row from an earlier run is still the highest pass, so
    ``mark_blank_junk_final`` would keep it over the verdict that should stand.
    """
    if not page_ids:
        return
    conn.execute(
        """
        DELETE FROM blank_junk_classification
         WHERE chart_id = %s
           AND pass_no = %s
           AND page_id IN (SELECT unnest(%s::bigint[]))
        """,
        (chart_id, pass_no, list(page_ids)),
    )


@_dispatch
def mark_blank_junk_final(conn: Any, chart_id: int) -> None:
    """Stamp the highest pass per page as the final verdict.

    Runs in two statements because a partial unique index on (page_id) WHERE
    is_final rejects a moment where two rows for one page are both final.
    """
    conn.execute(
        "UPDATE blank_junk_classification SET is_final = FALSE WHERE chart_id = %s",
        (chart_id,),
    )
    conn.execute(
        """
        UPDATE blank_junk_classification SET is_final = TRUE
         WHERE id IN (
             SELECT DISTINCT ON (page_id) id
               FROM blank_junk_classification
              WHERE chart_id = %s
              ORDER BY page_id, pass_no DESC, updated_at DESC NULLS LAST, id DESC
         )
        """,
        (chart_id,),
    )


@_dispatch
def get_blank_junk_reasons(conn: Any, chart_id: int, *, pass_no: int) -> dict[int, str]:
    """page_id -> one pass's verdict reason (blank_junk_classification.reason)."""
    rows = conn.execute(
        """
        SELECT page_id, reason FROM blank_junk_classification
         WHERE chart_id = %s AND pass_no = %s
        """,
        (chart_id, pass_no),
    ).fetchall()
    return {r["page_id"]: r["reason"] or "" for r in rows}


@_dispatch
def get_blank_junk_flags(
    conn: Any,
    chart_id: int,
    *,
    pass_no: Optional[int] = None,
    final_only: bool = False,
) -> dict[int, str]:
    if final_only:
        return {
            pid: row["blank_junk_flag"]
            for pid, row in get_blank_junk_final(conn, chart_id).items()
        }
    if pass_no is not None:
        rows = conn.execute(
            """
            SELECT page_id, blank_junk_flag FROM blank_junk_classification
             WHERE chart_id = %s AND pass_no = %s
            """,
            (chart_id, pass_no),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT ON (page_id) page_id, blank_junk_flag
              FROM blank_junk_classification
             WHERE chart_id = %s
             ORDER BY page_id, pass_no DESC, updated_at DESC NULLS LAST
            """,
            (chart_id,),
        ).fetchall()
    return {r["page_id"]: r["blank_junk_flag"] for r in rows}


@_dispatch
def revise_blank_junk_final(
    conn: Any,
    page_id: int,
    *,
    blank_junk_flag: str,
    duplicate_of_page_id: Optional[int],
    reason: str,
    junk_subtype: Optional[str] = None,
    confidence: Optional[float] = None,
) -> None:
    """Change a page's final blank / junk verdict in place: continuity's duplicate check
    (modules/continuity/duplicates.py) makes a partial duplicate a duplicate, or a
    duplicate a Main page with duplicate content; its short-page check
    (modules/continuity/short_pages.py) makes a Main page Junk / Others.
    ``junk_subtype`` / ``confidence`` change only when given; a page that is no longer
    junk loses its subtype."""
    conn.execute(
        """
        UPDATE blank_junk_classification
           SET blank_junk_flag = %s, duplicate_of_page_id = %s, reason = %s,
               junk_subtype = CASE WHEN %s = 'junk' THEN COALESCE(%s, junk_subtype) END,
               confidence = COALESCE(%s, confidence),
               updated_at = now()
         WHERE page_id = %s AND is_final
        """,
        (blank_junk_flag, duplicate_of_page_id, reason, blank_junk_flag, junk_subtype, confidence, page_id),
    )


@_dispatch
def get_blank_junk_final(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    """Final blank/junk verdict per page: flag, junk_subtype, confidence, reason."""
    rows = conn.execute(
        """
        SELECT page_id, blank_junk_flag, junk_subtype, confidence, duplicate_of_page_id, reason
          FROM v_page_blank_junk_final
         WHERE chart_id = %s
        """,
        (chart_id,),
    ).fetchall()
    return {int(r["page_id"]): dict(r) for r in rows}


# ---------------------------------------------------------------------------
# DOS
# ---------------------------------------------------------------------------


@_dispatch
def upsert_dos(
    conn: Any,
    *,
    chart_id: int,
    page_id: int,
    date_of_service_from: Optional[str],
    date_of_service_to: Optional[str],
    date_of_service_from_doclevel: Optional[str],
    date_of_service_to_doclevel: Optional[str],
    confidence: Optional[float],
    all_dates: Optional[Sequence[dict[str, Any]]] = None,
    extraction_method: Optional[str] = None,
) -> None:
    """One row per page: the single page/document-level pairs plus every date.

    `all_dates` items use the DOS library's key names (dos_from / dos_to); they
    are normalised here to the column names the `dates` array documents, so the
    stored JSON and the surrounding columns share one vocabulary.
    `date_count` is generated by the database from `dates`.
    """
    dates = [
        {
            "seq": seq,
            "date_of_service_from": item.get("dos_from"),
            "date_of_service_to": item.get("dos_to"),
            "source_keyword": item.get("source_keyword"),
            "confidence": item.get("confidence"),
        }
        for seq, item in enumerate(all_dates or [], start=1)
    ]
    conn.execute(
        """
        INSERT INTO dos_extraction_results (
            chart_id, page_id,
            date_of_service_from, date_of_service_to,
            date_of_service_from_doclevel, date_of_service_to_doclevel,
            dates, extraction_method, confidence
        ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        ON CONFLICT (page_id) DO UPDATE SET
            date_of_service_from          = EXCLUDED.date_of_service_from,
            date_of_service_to            = EXCLUDED.date_of_service_to,
            date_of_service_from_doclevel = EXCLUDED.date_of_service_from_doclevel,
            date_of_service_to_doclevel   = EXCLUDED.date_of_service_to_doclevel,
            dates                         = EXCLUDED.dates,
            extraction_method             = EXCLUDED.extraction_method,
            confidence                    = EXCLUDED.confidence,
            updated_at                    = now()
        """,
        (
            chart_id, page_id,
            date_of_service_from, date_of_service_to,
            date_of_service_from_doclevel, date_of_service_to_doclevel,
            json.dumps(dates), extraction_method, confidence,
        ),
    )


# ---------------------------------------------------------------------------
# Page classification (codeable) + encounter type + page sequencing
# ---------------------------------------------------------------------------


def _confidence_level(confidence: Optional[float]) -> Optional[str]:
    """The confidence's band (modules/confidence.py): high >= 0.80, medium >= 0.50,
    low below (flagged for review)."""
    from modules import confidence as conf

    return conf.level(None if confidence is None else float(confidence))


PAGE_CLASSIFICATION_FIELDS = (
    "page_type", "page_subtype", "model_type", "classification_category",
    "duplicate_flag", "confidence", "confidence_level", "decided_by", "needs_review",
    "bert_model_type", "bert_confidence", "keyword_page_subtype", "keyword_score",
    "keyword_margin", "keyword_title_hit", "axes",
)


@_dispatch
def upsert_page_classification(
    conn: Any,
    *,
    chart_id: int,
    page_id: int,
    page_subtype: Optional[str],
    classification_category: str,
    confidence: Optional[float] = None,
    duplicate_flag: bool = False,
    **fields: Any,
) -> None:
    """One page's classification: taxonomy names, ladder step, raw model outputs."""
    row = {
        "page_type": None, "model_type": None, "decided_by": None, "needs_review": False,
        "bert_model_type": None, "bert_confidence": None, "keyword_page_subtype": None,
        "keyword_score": None, "keyword_margin": None, "keyword_title_hit": None,
        "axes": None,
        **fields,
        "page_subtype": (page_subtype or "")[:200] or None,
        "classification_category": classification_category,
        "duplicate_flag": bool(duplicate_flag),
        "confidence": confidence,
        "confidence_level": _confidence_level(confidence),
    }
    if row["axes"] is not None:
        row["axes"] = json.dumps(row["axes"], default=str)
    values = [row[name] for name in PAGE_CLASSIFICATION_FIELDS]
    conn.execute(
        f"""
        INSERT INTO page_classification (chart_id, page_id, {", ".join(PAGE_CLASSIFICATION_FIELDS)})
        VALUES (%s, %s, {", ".join(["%s"] * len(PAGE_CLASSIFICATION_FIELDS))})
        ON CONFLICT (page_id) DO UPDATE SET
            {", ".join(f"{c} = EXCLUDED.{c}" for c in PAGE_CLASSIFICATION_FIELDS)},
            updated_at = now()
        """,
        (chart_id, page_id, *values),
    )


@_dispatch
def upsert_encounter(
    conn: Any,
    *,
    chart_id: int,
    page_id: int,
    encounter_type: str,
    confidence: Optional[float] = None,
    matched_keyword: Optional[str] = None,
    encounter_seq: Optional[int] = None,
    decided_by: Optional[str] = None,
    reason: Optional[str] = None,
    conflict: bool = False,
    contenders: Sequence[str] = (),
) -> None:
    """One page's encounter type, with the encounter it belongs to and how that
    encounter's setting was decided (the same on every page of the encounter)."""
    global _encounter_columns_ready
    if not _encounter_columns_ready:
        conn.execute(_ADD_ENCOUNTER_COLUMNS)
        _encounter_columns_ready = True
    conn.execute(
        """
        INSERT INTO encounter_type_results (
            chart_id, page_id, encounter_type, confidence, matched_keyword,
            encounter_seq, decided_by, reason, conflict, contenders
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (page_id) DO UPDATE SET
            encounter_type  = EXCLUDED.encounter_type,
            confidence      = EXCLUDED.confidence,
            matched_keyword = EXCLUDED.matched_keyword,
            encounter_seq   = EXCLUDED.encounter_seq,
            decided_by      = EXCLUDED.decided_by,
            reason          = EXCLUDED.reason,
            conflict        = EXCLUDED.conflict,
            contenders      = EXCLUDED.contenders,
            updated_at      = now()
        """,
        (
            chart_id,
            page_id,
            encounter_type,
            confidence,
            (matched_keyword or "")[:200] or None,
            encounter_seq,
            (decided_by or "")[:40] or None,
            reason or None,
            bool(conflict),
            json.dumps(list(contenders or [])),
        ),
    )


# The encounter columns (schema/migrations/2026-10-25.sql), added on first write so a database
# that has not run the file still gets them.
_ADD_ENCOUNTER_COLUMNS = (
    "ALTER TABLE encounter_type_results "
    "ADD COLUMN IF NOT EXISTS encounter_seq INT, "
    "ADD COLUMN IF NOT EXISTS decided_by VARCHAR(40), "
    "ADD COLUMN IF NOT EXISTS reason TEXT, "
    "ADD COLUMN IF NOT EXISTS conflict BOOLEAN NOT NULL DEFAULT FALSE, "
    "ADD COLUMN IF NOT EXISTS contenders JSONB NOT NULL DEFAULT '[]'::jsonb"
)
_encounter_columns_ready = False


@_dispatch
def delete_encounter(conn: Any, chart_id: int, page_ids: Sequence[int]) -> None:
    """Drop encounter rows for pages that are now unresolved.

    Only resolved pages get a row, so a page that had a type on an earlier run
    and has none now would otherwise keep the old answer.
    """
    if not page_ids:
        return
    conn.execute(
        """
        DELETE FROM encounter_type_results
         WHERE chart_id = %s
           AND page_id IN (SELECT unnest(%s::bigint[]))
        """,
        (chart_id, list(page_ids)),
    )


@_dispatch
def upsert_sequencing(
    conn: Any,
    *,
    chart_id: int,
    page_id: int,
    original_page_number: Optional[int],
    seq: Optional[int],
    confidence: Optional[float] = None,
    sequence_method: Optional[str] = None,
    review_flag: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO page_sequencing_results (
            chart_id, page_id, original_page_number, seq,
            confidence, sequence_method, review_flag
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (page_id) DO UPDATE SET
            original_page_number = EXCLUDED.original_page_number,
            seq                  = EXCLUDED.seq,
            confidence           = EXCLUDED.confidence,
            sequence_method      = EXCLUDED.sequence_method,
            review_flag          = EXCLUDED.review_flag,
            updated_at           = now()
        """,
        (
            chart_id,
            page_id,
            original_page_number,
            seq,
            confidence,
            (sequence_method or "")[:64] or None,
            bool(review_flag),
        ),
    )


@_dispatch
def get_page_types(conn: Any, chart_id: int) -> dict[int, str]:
    """``page_classification.page_subtype`` keyed by page_id."""
    rows = conn.execute(
        "SELECT page_id, page_subtype FROM page_classification WHERE chart_id = %s",
        (chart_id,),
    ).fetchall()
    return {int(row["page_id"]): row["page_subtype"] or "" for row in rows}


PROVIDER_SIGNATURE_COLUMNS = (
    "signature_present", "signature_key", "region", "scale", "sentence", "ner_text",
    "provider_name", "provider_credentials", "signers", "provider_version",
    "signature_date", "confidence", "source",
)
_PROVIDER_SIGNATURE_PRESENT: Optional[tuple[str, ...]] = None


def _table_columns(conn: Any, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND table_schema = current_schema()",
        (table,),
    ).fetchall()
    return {str(r["column_name"]) for r in rows}


@_dispatch
def upsert_provider_signature(conn: Any, *, chart_id: int, page_id: int, fields: dict[str, Any]) -> None:
    """One page's provider signature (stage kv_extract; results.signature_fields), the
    same values as <chart>_provider_signature.csv. Writes the columns the table has, so a
    database without the 2026-10-18 columns keeps working."""
    global _PROVIDER_SIGNATURE_PRESENT
    if _PROVIDER_SIGNATURE_PRESENT is None:
        present = _table_columns(conn, "provider_signature_results")
        _PROVIDER_SIGNATURE_PRESENT = tuple(c for c in PROVIDER_SIGNATURE_COLUMNS if c in present)
        missing = [c for c in PROVIDER_SIGNATURE_COLUMNS if c not in present]
        if missing:
            logger.warning(
                "provider_signature_results lacks %s: apply schema/migrations/2026-10-16.sql",
                ", ".join(missing),
            )
    columns = _PROVIDER_SIGNATURE_PRESENT
    row = {**fields, "signature_present": bool(fields.get("signature_present"))}
    values = [
        json.dumps(row.get(c) or [], default=str) if c == "signers" else row.get(c)
        for c in columns
    ]
    conn.execute(
        f"""
        INSERT INTO provider_signature_results (chart_id, page_id, {", ".join(columns)})
        VALUES (%s, %s, {", ".join(["%s"] * len(columns))})
        ON CONFLICT (page_id) DO UPDATE SET
            {", ".join(f"{c} = EXCLUDED.{c}" for c in columns)},
            updated_at = now()
        """,
        (chart_id, page_id, *values),
    )


@_dispatch
def get_provider_signature_map(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    """provider_signature_results by page_id."""
    return _by_page(conn, "SELECT * FROM provider_signature_results WHERE chart_id = %s", chart_id)


@_dispatch
def get_additional_page_details_map(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    """``additional_page_details`` by page_id: the printed page number and section headers
    the key/value stage stored."""
    return _by_page(conn, "SELECT * FROM additional_page_details WHERE chart_id = %s", chart_id)


# Databases created before heading_candidates (2026-10-21) gain it on the first write.
_ADD_HEADING_CANDIDATES = (
    "ALTER TABLE additional_page_details "
    "ADD COLUMN IF NOT EXISTS heading_candidates JSONB NOT NULL DEFAULT '{}'::jsonb, "
    "ADD COLUMN IF NOT EXISTS extraction_fields JSONB NOT NULL DEFAULT '{}'::jsonb"
)
_heading_candidates_ready = False


@_dispatch
def upsert_additional_page_details(
    conn: Any, *, chart_id: int, page_id: int, fields: dict[str, Any]
) -> None:
    """Printed page number, section headers, every heading candidate and every staged
    field for one page (key/value stage)."""
    global _heading_candidates_ready
    if not _heading_candidates_ready:
        conn.execute(_ADD_HEADING_CANDIDATES)
        _heading_candidates_ready = True
    conn.execute(
        """
        INSERT INTO additional_page_details (
            chart_id, page_id, page_number_key, page_number_region,
            page_number_sentence, page_number_value, printed_page_no,
            printed_page_total, confidence, source, section_headers,
            heading_candidates, extraction_fields
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb)
        ON CONFLICT (page_id) DO UPDATE SET
            page_number_key      = EXCLUDED.page_number_key,
            page_number_region   = EXCLUDED.page_number_region,
            page_number_sentence = EXCLUDED.page_number_sentence,
            page_number_value    = EXCLUDED.page_number_value,
            printed_page_no      = EXCLUDED.printed_page_no,
            printed_page_total   = EXCLUDED.printed_page_total,
            confidence           = EXCLUDED.confidence,
            source               = EXCLUDED.source,
            section_headers      = EXCLUDED.section_headers,
            heading_candidates   = EXCLUDED.heading_candidates,
            extraction_fields    = EXCLUDED.extraction_fields,
            updated_at           = now()
        """,
        (
            chart_id,
            page_id,
            (fields.get("page_number_key") or "")[:200] or None,
            (fields.get("page_number_region") or "")[:50] or None,
            fields.get("page_number_sentence") or None,
            fields.get("page_number_value") or None,
            (str(fields.get("printed_page_no") or ""))[:20] or None,
            (str(fields.get("printed_page_total") or ""))[:20] or None,
            fields.get("confidence"),
            (fields.get("source") or "")[:20] or None,
            json.dumps(fields.get("section_headers") or [], default=str),
            json.dumps(fields.get("heading_candidates") or {}, default=str),
            json.dumps(fields.get("extraction_fields") or {}, default=str),
        ),
    )


IMAGING_FINAL_COLUMNS = (
    "member_name",
    "member_dob",
    "member_id",
    "printed_or_handwritten",
    "handwritten_area_pct",
    "is_visible",
    "quality_tag",
    "orientation_angle",
    "tilt_angle",
    "mirrored",
    "blank_junk_flag",
    "is_duplicate",
    "document_seq",
    "page_type",
    "page_subtype",
    "model_type",
    "codability",
    "classification_category",
    "page_type_source",
    "continuation_rule",
    "needs_review",
    "dos_from",
    "dos_to",
    "dos_source",
    "encounter_type",
    "provider_name",
    "signature_present",
    "seq",
    "duplicate_of_page_id",
    # Appended so the Final CSV's columns above keep their places.
    "specialty",
    "provider_credentials",
    "signers",
    "provider_version",
    "provider_confidence",
    # 2026-10-25: each Final value's confidence on the one scale (modules/confidence.py)
    # and the values below the review line.
    "confidences",
    "review_fields",
)


_IMAGING_FINAL_PRESENT: Optional[tuple[str, ...]] = None
_IMAGING_FINAL_JSON = ("signers", "confidences", "review_fields")


def _imaging_final_columns(conn: Any) -> tuple[str, ...]:
    """IMAGING_FINAL_COLUMNS the table has. A database from before a column was added
    (schema/migrations/2026-10-16.sql not run yet) keeps working without it. Read once."""
    global _IMAGING_FINAL_PRESENT
    if _IMAGING_FINAL_PRESENT is None:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'imaging_final' AND table_schema = current_schema()"
        ).fetchall()
        present = {str(r["column_name"]) for r in rows}
        missing = [c for c in IMAGING_FINAL_COLUMNS if c not in present]
        if missing:
            logger.warning(
                "imaging_final lacks %s: not written until schema/migrations/2026-10-16.sql is applied",
                ", ".join(missing),
            )
        _IMAGING_FINAL_PRESENT = tuple(c for c in IMAGING_FINAL_COLUMNS if c in present) if present else IMAGING_FINAL_COLUMNS
    return _IMAGING_FINAL_PRESENT


@_dispatch
def upsert_imaging_final(conn: Any, *, chart_id: int, page_id: int, row: dict[str, Any]) -> None:
    """One page's Final values (``modules.imaging_final``), in the columns the table has."""
    columns = _imaging_final_columns(conn)
    values = [
        json.dumps(row[column], default=str) if column in _IMAGING_FINAL_JSON and row.get(column) is not None
        else row.get(column)
        for column in columns
    ]
    conn.execute(
        f"""
        INSERT INTO imaging_final (
            chart_id, page_id, {", ".join(columns)}
        ) VALUES (%s, %s, {", ".join(["%s"] * len(columns))})
        ON CONFLICT (page_id) DO UPDATE SET
            {", ".join(f"{c} = EXCLUDED.{c}" for c in columns)},
            updated_at = now()
        """,
        (chart_id, page_id, *values),
    )


def _by_page(conn: Any, sql: str, chart_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(sql, (chart_id,)).fetchall()
    return {int(row["page_id"]): dict(row) for row in rows}


@_dispatch
def get_page_classification_map(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    return _by_page(conn, "SELECT * FROM page_classification WHERE chart_id = %s", chart_id)


@_dispatch
def get_member_extraction_map(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    return _by_page(
        conn,
        "SELECT * FROM member_extraction_results WHERE chart_id = %s AND page_id IS NOT NULL",
        chart_id,
    )


@_dispatch
def get_encounter_map(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    return _by_page(conn, "SELECT * FROM encounter_type_results WHERE chart_id = %s", chart_id)


@_dispatch
def get_sequencing_map(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    return _by_page(conn, "SELECT * FROM page_sequencing_results WHERE chart_id = %s", chart_id)


@_dispatch
def get_continuity_map(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    return _by_page(conn, "SELECT * FROM page_continuity_results WHERE chart_id = %s", chart_id)


@_dispatch
def upsert_continuity(conn: Any, *, chart_id: int, page_id: int, row: dict[str, Any]) -> None:
    """One page's document grouping (``continuity.engine.assign``), with its label as shown."""
    global _continuity_label_ready
    if not _continuity_label_ready:
        conn.execute("ALTER TABLE page_continuity_results ADD COLUMN IF NOT EXISTS label VARCHAR(60)")
        _continuity_label_ready = True
    conn.execute(
        """
        INSERT INTO page_continuity_results (
            chart_id, page_id, document_seq, seq, position, relation, decided_by,
            confidence_level, score, confidence, review_required, link_strength,
            start_confirmed, in_progress_note, evidence, label
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (page_id) DO UPDATE SET
            document_seq       = EXCLUDED.document_seq,
            seq                = EXCLUDED.seq,
            position           = EXCLUDED.position,
            relation           = EXCLUDED.relation,
            decided_by         = EXCLUDED.decided_by,
            confidence_level   = EXCLUDED.confidence_level,
            score              = EXCLUDED.score,
            confidence         = EXCLUDED.confidence,
            review_required    = EXCLUDED.review_required,
            link_strength      = EXCLUDED.link_strength,
            start_confirmed    = EXCLUDED.start_confirmed,
            in_progress_note   = EXCLUDED.in_progress_note,
            evidence           = EXCLUDED.evidence,
            label              = EXCLUDED.label,
            updated_at         = now()
        """,
        (
            chart_id,
            page_id,
            row.get("document_seq"),
            row.get("seq"),
            row.get("position"),
            row.get("relation") or None,
            row.get("decided_by"),
            row.get("confidence_level") or None,
            row.get("score"),
            row.get("confidence"),
            bool(row.get("review_required")),
            row.get("link_strength"),
            bool(row.get("start_confirmed")),
            bool(row.get("in_progress_note")),
            row.get("evidence") or None,
            (row.get("label") or "")[:60] or None,
        ),
    )


_continuity_label_ready = False

# ---------------------------------------------------------------------------
# Member extraction + verification
# ---------------------------------------------------------------------------


@_dispatch
def upsert_member_extraction(
    conn: Any,
    *,
    chart_id: int,
    page_id: int,
    extracted_name: Optional[str],
    extracted_dob: Optional[str],
    extracted_member_id: Optional[str],
    detection_source_name: Optional[str],
    detection_source_dob: Optional[str],
    detection_source_member_id: Optional[str],
    ner_key_source_name: Optional[str],
    ner_key_source_dob: Optional[str],
    ner_key_source_member_id: Optional[str],
    page_status: Optional[str],
    page_verified: Optional[bool],
    confidence: Optional[float],
    provided_name: Optional[str],
    provided_dob: Optional[str],
    provided_external_member_id: Optional[str],
    matched_member_list_id: Optional[int],
    other_names: Optional[list[str]] = None,
) -> None:
    """One page's member row. ``other_names``: the people the page names who are not the
    manifest member (2026-10-27), written when the table has the column."""
    conn.execute(
        """
        INSERT INTO member_extraction_results (
            chart_id, page_id,
            extracted_name, extracted_dob, extracted_member_id,
            detection_source_name, detection_source_dob, detection_source_member_id,
            ner_key_source_name, ner_key_source_dob, ner_key_source_member_id,
            page_status, page_verified, confidence,
            provided_name, provided_dob, provided_external_member_id,
            matched_member_list_id
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (page_id) DO UPDATE SET
            extracted_name             = EXCLUDED.extracted_name,
            extracted_dob              = EXCLUDED.extracted_dob,
            extracted_member_id        = EXCLUDED.extracted_member_id,
            detection_source_name      = EXCLUDED.detection_source_name,
            detection_source_dob       = EXCLUDED.detection_source_dob,
            detection_source_member_id = EXCLUDED.detection_source_member_id,
            ner_key_source_name        = EXCLUDED.ner_key_source_name,
            ner_key_source_dob         = EXCLUDED.ner_key_source_dob,
            ner_key_source_member_id   = EXCLUDED.ner_key_source_member_id,
            page_status                = EXCLUDED.page_status,
            page_verified              = EXCLUDED.page_verified,
            confidence                 = EXCLUDED.confidence,
            provided_name              = EXCLUDED.provided_name,
            provided_dob               = EXCLUDED.provided_dob,
            provided_external_member_id = EXCLUDED.provided_external_member_id,
            matched_member_list_id     = EXCLUDED.matched_member_list_id,
            updated_at                 = now()
        """,
        (
            chart_id, page_id,
            extracted_name, extracted_dob, extracted_member_id,
            detection_source_name, detection_source_dob, detection_source_member_id,
            ner_key_source_name, ner_key_source_dob, ner_key_source_member_id,
            page_status, page_verified, confidence,
            provided_name, provided_dob, provided_external_member_id,
            matched_member_list_id,
        ),
    )
    if _member_other_names_column(conn):
        conn.execute(
            "UPDATE member_extraction_results SET other_names = %s::jsonb WHERE page_id = %s",
            (json.dumps(list(other_names or [])), page_id),
        )


_MEMBER_OTHER_NAMES: Optional[bool] = None


def _member_other_names_column(conn: Any) -> bool:
    """member_extraction_results.other_names exists (schema/migrations/2026-10-27.sql). Read once."""
    global _MEMBER_OTHER_NAMES
    if _MEMBER_OTHER_NAMES is None:
        _MEMBER_OTHER_NAMES = "other_names" in _table_columns(conn, "member_extraction_results")
        if not _MEMBER_OTHER_NAMES:
            logger.warning("member_extraction_results lacks other_names: run schema/migrations/2026-10-27.sql")
    return _MEMBER_OTHER_NAMES


@_dispatch
def upsert_member_summary(
    conn: Any,
    *,
    chart_id: int,
    final_status: str,
    document_decision: Optional[str],
    matched_member_list_id: Optional[int],
    matched_name: Optional[str],
    name_mode: Optional[str],
    confidence: Optional[float],
    pages_checked: int,
    pages_matched: int,
    wrong_member_pages: int,
    reject_threshold: Optional[int],
    decision_reason: Optional[str],
) -> None:
    conn.execute(
        """
        INSERT INTO member_verification_summary (
            chart_id, final_status, document_decision, matched_member_list_id,
            matched_name, name_mode, confidence, pages_checked, pages_matched,
            wrong_member_pages, reject_threshold, decision_reason
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (chart_id) DO UPDATE SET
            final_status       = EXCLUDED.final_status,
            document_decision  = EXCLUDED.document_decision,
            matched_member_list_id = EXCLUDED.matched_member_list_id,
            matched_name       = EXCLUDED.matched_name,
            name_mode          = EXCLUDED.name_mode,
            confidence         = EXCLUDED.confidence,
            pages_checked      = EXCLUDED.pages_checked,
            pages_matched      = EXCLUDED.pages_matched,
            wrong_member_pages = EXCLUDED.wrong_member_pages,
            reject_threshold   = EXCLUDED.reject_threshold,
            decision_reason    = EXCLUDED.decision_reason,
            updated_at         = now()
        """,
        (
            chart_id, final_status, document_decision, matched_member_list_id,
            matched_name, name_mode, confidence, pages_checked, pages_matched,
            wrong_member_pages, reject_threshold, decision_reason,
        ),
    )


@_dispatch
def get_member_summary(conn: Any, chart_id: int) -> Optional[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM member_verification_summary WHERE chart_id = %s",
        (chart_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# manifest_member_list
# ---------------------------------------------------------------------------


@_dispatch
def list_manifest_members(
    conn: Any,
    *,
    chart_id: Optional[int] = None,
    record_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Manifest rows for a chart, by record_id or by chart_id.

    record_id IS the chart name, so the chart_id form is a join rather than a
    stored foreign key. One spelling of the relationship instead of two that can
    disagree, and a manifest loaded before its chart is still found either way.
    """
    if record_id:
        return list(
            conn.execute(
                "SELECT * FROM manifest_member_list WHERE record_id = %s ORDER BY id",
                (record_id,),
            ).fetchall()
        )
    if chart_id is not None:
        return list(
            conn.execute(
                """
                SELECT m.* FROM manifest_member_list m
                  JOIN chart_list c ON c.chart_name = m.record_id
                 WHERE c.id = %s
                 ORDER BY m.id
                """,
                (chart_id,),
            ).fetchall()
        )
    return []


# Result tables wiped by reset_chart_results(). pipeline_jobs is deliberately
# absent: it is the run log, and the point of a re-run is to be able to compare
# it against the previous attempt. manifest_member_list is absent too — it is
# the client's roster, not our output, and re-loading it is a separate action.
# page_list is also kept: re-ingest upserts pages in place so page ids stay
# stable; only orphan page_names (gone from the new source) are pruned.
CHART_RESULT_TABLES = (
    "member_verification_summary",
    "member_extraction_results",
    "dos_extraction_results",
    "page_classification",
    "encounter_type_results",
    "page_sequencing_results",
    "imaging_final",
    "page_continuity_results",
    "additional_page_details",
    "provider_signature_results",
    "blank_junk_classification",
    "ocr_quality_results",
    "ocr_results",
    "page_stage_status",
)


@_dispatch
def reset_chart_results(conn: Any, chart_id: int) -> dict[str, int]:
    """Clear stage outputs for a re-ingest, keeping chart_list and page_list.

    A re-ingest of the same chart name would otherwise merge into the previous
    attempt: completed page_stage_status rows make the chart look finished
    while serving stale OCR/member/DOS. Wiping results first makes a re-run
    mean what it says.

    The chart_list and page_list rows survive so chart_id / page_id stay
    stable across re-submits. Orphan page_names (absent from the new source)
    are pruned by the caller after upsert_pages. pipeline_jobs survives as
    the audit trail.
    """
    deleted: dict[str, int] = {}
    for table in CHART_RESULT_TABLES:
        result = conn.execute(
            f"DELETE FROM {table} WHERE chart_id = %s", (chart_id,)
        )
        count = getattr(result, "rowcount", 0) or 0
        if count:
            deleted[table] = count
    conn.execute(
        """
        UPDATE chart_list
           SET status = 'received', current_stage = NULL, current_pass = NULL,
               page_count = NULL, updated_at = now()
         WHERE id = %s
        """,
        (chart_id,),
    )
    return deleted


OCR_STAGE_NAMES = ("ocr_prelim", "ocr_final1", "ocr_final2")


@_dispatch
def clear_chart_outputs(
    conn: Any, chart_id: int, *, keep_ocr: bool, keep_quality: bool = False
) -> dict[str, int]:
    """Clear a chart's results before a full re-run.

    ``keep_ocr=True`` (skip_ocr) keeps ``ocr_results`` and the OCR stages'
    ``page_stage_status`` rows; everything else is rebuilt. ``keep_ocr=False``
    clears OCR too. ``keep_quality`` also keeps ``ocr_quality_results``: a
    batch clears its charts up front with it, so a skip_ocr run can still
    snapshot the old rotation when the chart starts. ``chart_list`` and
    ``page_list`` are never touched, so ``page_count`` and page ids survive.
    """
    deleted: dict[str, int] = {}
    for table in CHART_RESULT_TABLES:
        if keep_ocr and table == "ocr_results":
            continue
        if keep_quality and table == "ocr_quality_results":
            continue
        if keep_ocr and table == "page_stage_status":
            result = conn.execute(
                """
                DELETE FROM page_stage_status
                 WHERE chart_id = %s AND stage_name <> ALL(%s)
                """,
                (chart_id, list(OCR_STAGE_NAMES)),
            )
        else:
            result = conn.execute(
                f"DELETE FROM {table} WHERE chart_id = %s", (chart_id,)
            )
        count = getattr(result, "rowcount", 0) or 0
        if count:
            deleted[table] = count
    return deleted


@_dispatch
def prune_orphan_pages(
    conn: Any, chart_id: int, keep_page_names: list[str]
) -> int:
    """Delete page_list rows whose names are not in the new ingest set."""
    keep = [n for n in keep_page_names if n]
    if not keep:
        result = conn.execute(
            "DELETE FROM page_list WHERE chart_id = %s", (chart_id,)
        )
        return getattr(result, "rowcount", 0) or 0
    result = conn.execute(
        """
        DELETE FROM page_list
         WHERE chart_id = %s
           AND page_name <> ALL(%s)
        """,
        (chart_id, keep),
    )
    return getattr(result, "rowcount", 0) or 0


@_dispatch
def count_manifest_members(conn: Any, record_id: str) -> int:
    """How many manifest rows exist for this chart name.

    Replaces the old link step: there is no chart_id to write, so ingest only
    reports whether a roster is present for the chart it just registered.
    """
    row = conn.execute(
        "SELECT count(*) AS n FROM manifest_member_list WHERE record_id = %s",
        (record_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


# Manifests (manifest_member_list) are written by db-utilities, never here (2026-11-07).


# ---------------------------------------------------------------------------
# Jobs, usage and batch lookups (2026-11-07: moved here from the API, usage.py and
# batch_intake.py, so no SQL lives outside common.db)
# ---------------------------------------------------------------------------


@_dispatch
def list_jobs(conn: Any, *, chart_id: Optional[int] = None, limit: int = 50) -> list[dict[str, Any]]:
    """Recent pipeline_jobs rows, newest first: one chart's, or the whole service's."""
    if chart_id is not None:
        return list(conn.execute(
            "SELECT * FROM pipeline_jobs WHERE chart_id = %s ORDER BY id DESC LIMIT %s",
            (chart_id, limit),
        ).fetchall())
    return list(conn.execute("SELECT * FROM pipeline_jobs ORDER BY id DESC LIMIT %s", (limit,)).fetchall())


@_dispatch
def insert_run_usage(conn: Any, row: dict[str, Any], columns: Sequence[str]) -> None:
    """One run_usage row (apps/worker/usage.py builds it). JSON columns arrive as dicts."""
    values = [json.dumps(row[c]) if c in ("stage_seconds", "llm_usage") else row[c] for c in columns]
    conn.execute(
        f"INSERT INTO run_usage ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
        values,
    )


@_dispatch
def list_run_usage_daily(conn: Any, days: int = 14) -> list[dict[str, Any]]:
    """The last ``days`` days of v_run_usage_daily, newest first."""
    cur = conn.execute(
        "SELECT * FROM v_run_usage_daily WHERE day >= current_date - %s ORDER BY day DESC", (int(days),)
    )
    names = [d[0] for d in cur.description]
    return [r if isinstance(r, dict) else dict(zip(names, r)) for r in cur.fetchall()]


@_dispatch
def list_finished_chart_names(
    conn: Any, names: Sequence[str], statuses: Sequence[str], since: Optional[Any] = None
) -> set[str]:
    """Of ``names``, those whose chart status is in ``statuses`` (updated on or after ``since``)."""
    rows = conn.execute(
        "SELECT chart_name FROM chart_list "
        "WHERE chart_name = ANY(%s) AND status = ANY(%s) "
        "AND (%s::date IS NULL OR updated_at >= %s::date)",
        (list(names), sorted(statuses), since, since),
    ).fetchall()
    return {str(r["chart_name"]) for r in rows}



# ---------------------------------------------------------------------------
# Helpers shared by Postgres + MemoryStore (CSV rebuild / stage queries)
# ---------------------------------------------------------------------------


def list_blank_junk_for_csv(conn: Any, chart_id: int) -> list[dict[str, Any]]:
    """Rows for the junk CSV rewrite."""
    if _mem(conn):
        return conn.list_blank_junk_for_csv(chart_id)
    return list(
        conn.execute(
            """
            SELECT c.chart_name, p.page_name, p.page_number,
                   b.blank_junk_flag, b.junk_subtype, b.confidence, b.reason,
                   b.ocr_source, b.pass_no, b.is_final,
                   orig.page_name AS duplicate_of_page
              FROM blank_junk_classification b
              JOIN page_list p  ON p.id = b.page_id
              JOIN chart_list c ON c.id = b.chart_id
              LEFT JOIN page_list orig ON orig.id = b.duplicate_of_page_id
             WHERE b.chart_id = %s
             ORDER BY p.page_number NULLS LAST, p.page_name, b.pass_no
            """,
            (chart_id,),
        ).fetchall()
    )


def list_quality_for_csv(conn: Any, chart_id: int) -> list[dict[str, Any]]:
    """Joined quality + page rows for rotation/hw/quality CSV rebuild."""
    if _mem(conn):
        return conn.list_quality_for_csv(chart_id)
    return list(
        conn.execute(
            """
            SELECT p.page_name, p.page_number, q.printed_or_handwritten,
                   q.orientation_angle, q.tilt_angle, q.mirrored,
                   q.rotation_applied, q.hw_confidence, q.hw_method,
                   q.quality_tag, q.quality_score, q.input_dpi,
                   q.document_type, q.handwritten_probability, q.is_visible,
                   q.handwritten_area_pct, q.review_required,
                   q.quality_detail -> 'confidences' AS confidences
              FROM ocr_quality_results q
              JOIN page_list p ON p.id = q.page_id
             WHERE q.chart_id = %s
             ORDER BY p.page_number NULLS LAST, p.page_name
            """,
            (chart_id,),
        ).fetchall()
    )


def get_dos_map(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    """DOS extraction rows keyed by page_id."""
    if _mem(conn):
        return conn.get_dos_map(chart_id)
    rows = conn.execute(
        """
        SELECT page_id,
               date_of_service_from,
               date_of_service_to,
               date_of_service_from_doclevel,
               date_of_service_to_doclevel,
               confidence
          FROM dos_extraction_results
         WHERE chart_id = %s
        """,
        (chart_id,),
    ).fetchall()
    return {int(row["page_id"]): dict(row) for row in rows}


def has_ocr_results(conn: Any, chart_id: int) -> bool:
    """True when ocr_results has at least one non-empty row for this chart."""
    if _mem(conn):
        return conn.has_ocr_results(chart_id)
    row = conn.execute(
        """
        SELECT 1 AS ok
          FROM ocr_results
         WHERE chart_id = %s
           AND COALESCE(char_count, 0) > 0
         LIMIT 1
        """,
        (chart_id,),
    ).fetchone()
    return bool(row)
