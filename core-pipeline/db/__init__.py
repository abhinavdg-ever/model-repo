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
import logging
import os
import threading
from typing import Any, Iterator, Optional, Sequence

from config import DATABASE_URL

logger = logging.getLogger(__name__)

DB_POOL_MIN = int(os.environ.get("DB_POOL_MIN") or "1")
DB_POOL_MAX = int(os.environ.get("DB_POOL_MAX") or "8")

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
    """Lease a connection. Commits on clean exit, rolls back on exception."""
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
# There is no trained page-quality model yet. Until there is, every page gets a
# fixed grade derived from the handwriting classifier's label — printed pages
# score 0.8/high, handwritten and mixed pages 0.5/low.
#
# This is a PLACEHOLDER, not a measurement. It adds no information beyond
# printed_or_handwritten, so nothing downstream should branch on it, and it
# must not be reported to a reviewer as a real score. Replacing it means
# deleting this function and passing the model's output into upsert_quality.
QUALITY_PLACEHOLDER = {
    "printed":     ("high", 0.8000),
    "handwritten": ("low",  0.5000),
    "mixed":       ("low",  0.5000),
}
QUALITY_PLACEHOLDER_DEFAULT = ("low", 0.5000)


def quality_placeholder(
    printed_or_handwritten: Optional[str],
) -> tuple[Optional[str], Optional[float]]:
    """(quality_tag, quality_score) for a page, from its printed/handwritten label.

    Returns (None, None) when the label is missing — an unclassified page gets
    no grade rather than a default one.
    """
    if not printed_or_handwritten:
        return (None, None)
    key = str(printed_or_handwritten).strip().lower()
    return QUALITY_PLACEHOLDER.get(key, QUALITY_PLACEHOLDER_DEFAULT)


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
            source, blob_container, blob_path, run_id, batch_id
        ) VALUES (
            %s, %s, COALESCE(%s, 'received'), %s, %s,
            COALESCE(%s, 'blob'), %s, %s, %s, %s
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
            run_id              = COALESCE(EXCLUDED.run_id, chart_list.run_id),
            batch_id            = COALESCE(EXCLUDED.batch_id, chart_list.batch_id)
        RETURNING *
        """,
        (
            chart_name, page_count, status, current_stage, current_pass,
            source, blob_container, blob_path, run_id, batch_id,
            status, source,
        ),
    ).fetchone()


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


def get_chart(conn: Any, chart_id: int) -> Optional[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM chart_list WHERE id = %s", (chart_id,)
    ).fetchone()


def get_chart_by_name(conn: Any, chart_name: str) -> Optional[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM chart_list WHERE chart_name = %s", (chart_name,)
    ).fetchone()


def upsert_pages(
    conn: Any,
    chart_id: int,
    pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """pages: [{page_name, page_number, image_sha256?, file_size_bytes?}, ...]"""
    if not pages:
        return []
    for page in pages:
        conn.execute(
            """
            INSERT INTO page_list (
                chart_id, page_name, page_number, image_sha256, file_size_bytes
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (chart_id, page_name) DO UPDATE SET
                page_number     = EXCLUDED.page_number,
                image_sha256    = COALESCE(EXCLUDED.image_sha256, page_list.image_sha256),
                file_size_bytes = COALESCE(EXCLUDED.file_size_bytes, page_list.file_size_bytes)
            """,
            (
                chart_id,
                page["page_name"],
                page.get("page_number"),
                page.get("image_sha256"),
                page.get("file_size_bytes"),
            ),
        )
    return list_pages(conn, chart_id)


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
    everything (the /rerun?force=true path).
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


# ---------------------------------------------------------------------------
# pipeline_jobs
# ---------------------------------------------------------------------------


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
) -> None:
    """Upsert one page's quality row.

    quality_tag / quality_score are not passed in: no quality model exists
    yet, so they are derived here from printed_or_handwritten via
    `quality_placeholder`. When the model lands, this signature grows the two
    parameters and the placeholder call goes away.
    """
    quality_tag, quality_score = quality_placeholder(printed_or_handwritten)
    conn.execute(
        """
        INSERT INTO ocr_quality_results (
            chart_id, page_id, quality_tag, quality_score,
            printed_or_handwritten, hw_method, hw_confidence,
            orientation_angle, tilt_angle, mirrored, rotation_applied
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (page_id) DO UPDATE SET
            quality_tag            = EXCLUDED.quality_tag,
            quality_score          = EXCLUDED.quality_score,
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
            printed_or_handwritten, hw_method, hw_confidence,
            orientation_angle, tilt_angle, mirrored, rotation_applied,
        ),
    )


def get_quality_map(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM ocr_quality_results WHERE chart_id = %s", (chart_id,)
    ).fetchall()
    return {r["page_id"]: r for r in rows}


def handwritten_page_ids(conn: Any, chart_id: int) -> set[int]:
    rows = conn.execute(
        """
        SELECT page_id FROM ocr_quality_results
         WHERE chart_id = %s AND lower(printed_or_handwritten) = 'handwritten'
        """,
        (chart_id,),
    ).fetchall()
    return {r["page_id"] for r in rows}


# ---------------------------------------------------------------------------
# blank / junk / duplicate
# ---------------------------------------------------------------------------


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


def get_blank_junk_flags(
    conn: Any,
    chart_id: int,
    *,
    pass_no: Optional[int] = None,
    final_only: bool = False,
) -> dict[int, str]:
    if final_only:
        rows = conn.execute(
            "SELECT page_id, blank_junk_flag FROM v_page_blank_junk_final WHERE chart_id = %s",
            (chart_id,),
        ).fetchall()
    elif pass_no is not None:
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


# ---------------------------------------------------------------------------
# DOS
# ---------------------------------------------------------------------------


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
    """Primary DOS pair plus the full multi-date set for the page."""
    dates = list(all_dates or [])
    conn.execute(
        """
        INSERT INTO dos_extraction_results (
            chart_id, page_id,
            date_of_service_from, date_of_service_to,
            date_of_service_from_doclevel, date_of_service_to_doclevel,
            date_count, extraction_method, confidence
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (page_id) DO UPDATE SET
            date_of_service_from          = EXCLUDED.date_of_service_from,
            date_of_service_to            = EXCLUDED.date_of_service_to,
            date_of_service_from_doclevel = EXCLUDED.date_of_service_from_doclevel,
            date_of_service_to_doclevel   = EXCLUDED.date_of_service_to_doclevel,
            date_count                    = EXCLUDED.date_count,
            extraction_method             = EXCLUDED.extraction_method,
            confidence                    = EXCLUDED.confidence,
            updated_at                    = now()
        """,
        (
            chart_id, page_id,
            date_of_service_from, date_of_service_to,
            date_of_service_from_doclevel, date_of_service_to_doclevel,
            len(dates), extraction_method, confidence,
        ),
    )
    conn.execute("DELETE FROM dos_extraction_dates WHERE page_id = %s", (page_id,))
    for seq, item in enumerate(dates, start=1):
        conn.execute(
            """
            INSERT INTO dos_extraction_dates (
                chart_id, page_id, seq,
                date_of_service_from, date_of_service_to,
                source_keyword, confidence
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (page_id, seq) DO UPDATE SET
                date_of_service_from = EXCLUDED.date_of_service_from,
                date_of_service_to   = EXCLUDED.date_of_service_to,
                source_keyword       = EXCLUDED.source_keyword,
                confidence           = EXCLUDED.confidence,
                updated_at           = now()
            """,
            (
                chart_id, page_id, seq,
                item.get("dos_from"), item.get("dos_to"),
                item.get("source_keyword"), item.get("confidence"),
            ),
        )


# ---------------------------------------------------------------------------
# Member extraction + verification
# ---------------------------------------------------------------------------


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


def get_member_summary(conn: Any, chart_id: int) -> Optional[dict[str, Any]]:
    return conn.execute(
        "SELECT * FROM member_verification_summary WHERE chart_id = %s",
        (chart_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# manifest_member_list
# ---------------------------------------------------------------------------


def list_manifest_members(
    conn: Any,
    *,
    chart_id: Optional[int] = None,
    record_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Manifest rows for a chart.

    record_id is the real key — a manifest sweep can land before the chart is
    ingested, so looking up by chart_id alone would miss those rows.
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
                 WHERE m.chart_id = %s
                    OR m.record_id = (SELECT chart_name FROM chart_list WHERE id = %s)
                 ORDER BY m.id
                """,
                (chart_id, chart_id),
            ).fetchall()
        )
    return []


def upsert_manifest_member(
    conn: Any,
    *,
    record_id: str,
    chart_id: Optional[int],
    member_name: str,
    first_name: Optional[str],
    middle_name: Optional[str],
    last_name: Optional[str],
    member_dob: Optional[str],
    external_member_id: Optional[str],
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    source_file: Optional[str] = None,
    source_blob_path: Optional[str] = None,
) -> dict[str, Any]:
    """Insert or update one manifest row. Returns {id, action}.

    Two partial unique indexes back this: (record_id, external_member_id) when a
    MemberID is present, (record_id, lower(member_name), dob) when it is not.
    Which index applies is decided by the same predicate, so exactly one
    ON CONFLICT target is valid per row.
    """
    has_id = bool((external_member_id or "").strip())
    params = (
        record_id, chart_id, member_name, first_name, middle_name, last_name,
        member_dob, (external_member_id or None) if has_id else None,
        run_id, batch_id, source_file, source_blob_path,
    )
    conflict = (
        "(record_id, external_member_id) WHERE external_member_id IS NOT NULL AND external_member_id <> ''"
        if has_id
        else "(record_id, lower(member_name), COALESCE(member_dob, 'epoch'::date)) "
             "WHERE external_member_id IS NULL OR external_member_id = ''"
    )
    row = conn.execute(
        f"""
        INSERT INTO manifest_member_list (
            record_id, chart_id, member_name, first_name, middle_name, last_name,
            member_dob, external_member_id, run_id, batch_id,
            source_file, source_blob_path
        ) VALUES (%s, %s, %s, %s, %s, %s, %s::date, %s, %s, %s, %s, %s)
        ON CONFLICT {conflict} DO UPDATE SET
            chart_id         = COALESCE(EXCLUDED.chart_id, manifest_member_list.chart_id),
            member_name      = EXCLUDED.member_name,
            first_name       = COALESCE(EXCLUDED.first_name, manifest_member_list.first_name),
            middle_name      = COALESCE(EXCLUDED.middle_name, manifest_member_list.middle_name),
            last_name        = COALESCE(EXCLUDED.last_name, manifest_member_list.last_name),
            member_dob       = COALESCE(EXCLUDED.member_dob, manifest_member_list.member_dob),
            run_id           = COALESCE(EXCLUDED.run_id, manifest_member_list.run_id),
            batch_id         = COALESCE(EXCLUDED.batch_id, manifest_member_list.batch_id),
            source_file      = COALESCE(EXCLUDED.source_file, manifest_member_list.source_file),
            source_blob_path = COALESCE(EXCLUDED.source_blob_path, manifest_member_list.source_blob_path),
            updated_at       = now()
        RETURNING id, (xmax = 0) AS inserted
        """,
        params,
    ).fetchone()
    return {
        "id": row["id"],
        "action": "inserted" if row["inserted"] else "updated",
    }


def link_manifest_to_chart(conn: Any, chart_id: int, record_id: str) -> int:
    """Attach manifest rows swept before the chart existed."""
    result = conn.execute(
        """
        UPDATE manifest_member_list
           SET chart_id = %s
         WHERE record_id = %s AND chart_id IS DISTINCT FROM %s
        """,
        (chart_id, record_id, chart_id),
    )
    return getattr(result, "rowcount", 0) or 0
