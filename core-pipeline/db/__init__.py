"""Postgres helpers for core-pipeline (schema v7).

Three things changed from the v6 helper module:

* **A connection pool.** v6 opened a fresh connection per page per stage; a
  400-page chart cost ~3000 TCP+auth round trips. ``connect()`` now leases from
  a pool and the stages hold one lease for a batch of pages.
* **page_stage_status.** The 11 ``page_list.*_status`` columns are gone. Stage
  progress is rows keyed ``(page_id, stage_name, pass_no)``, so a stage that
  runs twice is representable and adding a stage is an INSERT into
  ``pipeline_stage``, not a CHECK-constraint migration.
* **Real upserts.** ``upsert_chart`` / ``upsert_manifest_member`` were
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

from config import DATABASE_URL

from db.memory_store import (  # noqa: E402
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
    return isinstance(conn, MemoryStore)


def _dispatch(fn):
    """Route to MemoryStore.<same name> when conn is the in-memory backend."""
    import functools

    @functools.wraps(fn)
    def wrapper(conn, *args, **kwargs):
        if _mem(conn):
            method = getattr(conn, fn.__name__)
            return method(*args, **kwargs)
        return fn(conn, *args, **kwargs)

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
# quality_detail JSONB. See stages/lib/imaging/quality_analyzer.py.


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
    """
    return conn.execute(
        """
        INSERT INTO chart_list (
            chart_name, page_count, status, current_stage, current_pass,
            source, blob_container, blob_path, output_path, run_id, batch_id
        ) VALUES (
            %s, %s, COALESCE(%s, 'received'), %s, %s,
            COALESCE(%s, 'blob'), %s, %s, %s, %s, %s
        )
        ON CONFLICT (chart_name) DO UPDATE SET
            page_count          = COALESCE(EXCLUDED.page_count, chart_list.page_count),
            status              = COALESCE(%s, chart_list.status),
            current_stage       = COALESCE(EXCLUDED.current_stage, chart_list.current_stage),
            current_pass        = COALESCE(EXCLUDED.current_pass, chart_list.current_pass),
            source              = CASE
                                    WHEN chart_list.source = 'manifest'
                                     AND EXCLUDED.source <> 'manifest'
                                    THEN EXCLUDED.source
                                    WHEN chart_list.source = 'manifest'
                                    THEN chart_list.source
                                    ELSE COALESCE(%s, chart_list.source)
                                  END,
            blob_container      = COALESCE(EXCLUDED.blob_container, chart_list.blob_container),
            blob_path           = COALESCE(EXCLUDED.blob_path, chart_list.blob_path),
            output_path         = COALESCE(EXCLUDED.output_path, chart_list.output_path),
            run_id              = COALESCE(EXCLUDED.run_id, chart_list.run_id),
            batch_id            = COALESCE(EXCLUDED.batch_id, chart_list.batch_id)
        RETURNING *
        """,
        (
            chart_name, page_count, status, current_stage, current_pass,
            source, blob_container, blob_path, output_path, run_id, batch_id,
            status, source,
        ),
    ).fetchone()


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
) -> None:
    """Upsert one page's quality / HW / rotation row."""
    import json

    detail = quality_detail if quality_detail is not None else {}
    conn.execute(
        """
        INSERT INTO ocr_quality_results (
            chart_id, page_id, quality_tag, quality_score, quality_detail,
            input_dpi, printed_or_handwritten, hw_method, hw_confidence,
            orientation_angle, tilt_angle, mirrored, rotation_applied
        ) VALUES (
            %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (page_id) DO UPDATE SET
            quality_tag            = EXCLUDED.quality_tag,
            quality_score          = EXCLUDED.quality_score,
            quality_detail         = EXCLUDED.quality_detail,
            input_dpi              = EXCLUDED.input_dpi,
            printed_or_handwritten = EXCLUDED.printed_or_handwritten,
            hw_method              = EXCLUDED.hw_method,
            hw_confidence          = EXCLUDED.hw_confidence,
            orientation_angle      = EXCLUDED.orientation_angle,
            tilt_angle             = EXCLUDED.tilt_angle,
            mirrored               = EXCLUDED.mirrored,
            rotation_applied       = EXCLUDED.rotation_applied,
            updated_at             = now()
        """,
        (
            chart_id, page_id, quality_tag, quality_score,
            json.dumps(detail, default=str),
            input_dpi,
            printed_or_handwritten, hw_method, hw_confidence,
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
def handwritten_page_ids(conn: Any, chart_id: int) -> set[int]:
    rows = conn.execute(
        """
        SELECT page_id FROM ocr_quality_results
         WHERE chart_id = %s AND lower(printed_or_handwritten) = 'handwritten'
        """,
        (chart_id,),
    ).fetchall()
    return {r["page_id"] for r in rows}


@_dispatch
def non_printed_page_ids(conn: Any, chart_id: int) -> set[int]:
    """Handwritten / uncertain / mixed — prelim blank/junk pass 1 is skipped."""
    rows = conn.execute(
        """
        SELECT page_id FROM ocr_quality_results
         WHERE chart_id = %s
           AND lower(coalesce(printed_or_handwritten, ''))
               IN ('handwritten', 'uncertain', 'mixed')
        """,
        (chart_id,),
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
    """High-quality printed pages — Azure final2 is skipped (final1 is enough)."""
    rows = conn.execute(
        """
        SELECT page_id FROM ocr_quality_results
         WHERE chart_id = %s
           AND lower(coalesce(quality_tag, '')) = 'high'
           AND lower(coalesce(printed_or_handwritten, '')) = 'printed'
        """,
        (chart_id,),
    ).fetchall()
    return {r["page_id"] for r in rows}


def page_blocks_prelim(
    quality_row: Optional[dict[str, Any]],
) -> bool:
    """True when prelim text must not be used (HW / uncertain / mixed / low)."""
    if not quality_row:
        return False
    hw = str(quality_row.get("printed_or_handwritten") or "").strip().lower()
    tag = str(quality_row.get("quality_tag") or "").strip().lower()
    if hw in {"handwritten", "uncertain", "mixed"}:
        return True
    if tag == "low":
        return True
    return False


# ---------------------------------------------------------------------------
# blank / junk / duplicate
# ---------------------------------------------------------------------------


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
) -> None:
    """Write one pass's verdict. `is_final` is set separately by mark_final()
    so that exactly one row per page carries it."""
    conn.execute(
        """
        INSERT INTO blank_junk_classification (
            chart_id, page_id, pass_no, blank_junk_flag, junk_subtype,
            duplicate_of_page_id, ocr_source, confidence, reason, is_final
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (page_id, pass_no) DO UPDATE SET
            blank_junk_flag      = EXCLUDED.blank_junk_flag,
            junk_subtype         = EXCLUDED.junk_subtype,
            duplicate_of_page_id = EXCLUDED.duplicate_of_page_id,
            ocr_source           = EXCLUDED.ocr_source,
            confidence           = EXCLUDED.confidence,
            reason               = EXCLUDED.reason,
            updated_at           = now()
        """,
        (
            chart_id, page_id, pass_no, blank_junk_flag, junk_subtype,
            duplicate_of_page_id, ocr_source, confidence, reason, is_final,
        ),
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
def get_blank_junk_final(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    """Final blank/junk verdict per page: flag, junk_subtype, confidence."""
    rows = conn.execute(
        """
        SELECT page_id, blank_junk_flag, junk_subtype, confidence
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
    if confidence is None:
        return None
    if confidence >= 0.75:
        return "high"
    if confidence >= 0.45:
        return "medium"
    return "low"


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
) -> None:
    """Write codeable / non_codeable / discharge_summary for one page."""
    conn.execute(
        """
        INSERT INTO page_classification (
            chart_id, page_id, page_subtype, classification_category,
            duplicate_flag, confidence, confidence_level
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (page_id) DO UPDATE SET
            page_subtype            = EXCLUDED.page_subtype,
            classification_category = EXCLUDED.classification_category,
            duplicate_flag          = EXCLUDED.duplicate_flag,
            confidence              = EXCLUDED.confidence,
            confidence_level        = EXCLUDED.confidence_level,
            updated_at              = now()
        """,
        (
            chart_id,
            page_id,
            (page_subtype or "")[:200] or None,
            classification_category,
            bool(duplicate_flag),
            confidence,
            _confidence_level(confidence),
        ),
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
) -> None:
    conn.execute(
        """
        INSERT INTO encounter_type_results (
            chart_id, page_id, encounter_type, confidence, matched_keyword
        ) VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (page_id) DO UPDATE SET
            encounter_type  = EXCLUDED.encounter_type,
            confidence      = EXCLUDED.confidence,
            matched_keyword = EXCLUDED.matched_keyword,
            updated_at      = now()
        """,
        (
            chart_id,
            page_id,
            encounter_type,
            confidence,
            (matched_keyword or "")[:200] or None,
        ),
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
) -> None:
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


@_dispatch
def upsert_manifest_member(
    conn: Any,
    *,
    record_id: str,
    member_name: str,
    first_name: Optional[str],
    middle_name: Optional[str],
    last_name: Optional[str],
    member_dob: Optional[str],
    external_member_id: Optional[str],
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    source_file: Optional[str] = None,
    source_path: Optional[str] = None,
) -> dict[str, Any]:
    """Insert or update one manifest row. Returns {id, action}.

    Prefer :func:`upsert_manifest_members` for bulk loads (batches of 1000).
    """
    stats = upsert_manifest_members(
        conn,
        [
            {
                "record_id": record_id,
                "member_name": member_name,
                "first_name": first_name,
                "middle_name": middle_name,
                "last_name": last_name,
                "member_dob": member_dob,
                "external_member_id": external_member_id,
                "run_id": run_id,
                "batch_id": batch_id,
                "source_file": source_file,
                "source_path": source_path,
            }
        ],
    )
    # Single-row callers only need action; id is not required by current uses.
    return {
        "id": None,
        "action": "inserted" if stats["inserted"] else "updated",
    }


MANIFEST_UPSERT_BATCH = 1000

_MANIFEST_VALUE = "(%s, %s, %s, %s, %s, %s::date, %s, %s, %s, %s, %s)"

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
    )


def _manifest_dedupe_key(m: dict[str, Any], *, has_id: bool) -> tuple[Any, ...]:
    if has_id:
        return (m["record_id"], (m.get("external_member_id") or "").strip())
    dob = m.get("member_dob") or "epoch"
    return (m["record_id"], (m["member_name"] or "").casefold(), dob)


@_dispatch
def upsert_manifest_members(
    conn: Any,
    members: list[dict[str, Any]],
    *,
    batch_size: int = MANIFEST_UPSERT_BATCH,
) -> dict[str, int]:
    """Bulk upsert manifesto rows in chunks of ``batch_size`` (default 1000).

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
                    source_file, source_path
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
                   b.ocr_source, b.pass_no, b.is_final
              FROM blank_junk_classification b
              JOIN page_list p  ON p.id = b.page_id
              JOIN chart_list c ON c.id = b.chart_id
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
                   q.quality_tag, q.quality_score, q.input_dpi
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
               date_of_service_to_doclevel
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
