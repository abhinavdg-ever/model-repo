"""Stage: intake — register a chart and pull its page images to local disk.

The chart folder on blob holds one image per page. This stage:

  1. upserts ``chart_list`` (atomic on UNIQUE (chart_name)),
  2. downloads each image into ``review-ui/data/folders/<chart>/pages/{n}.ext``,
  3. upserts ``page_list`` with a SHA-256 and size per page,
  4. seeds ``page_stage_status`` so every later stage has a pending row,
  5. links any manifest rows swept before this chart existed.

The SHA-256 makes the download idempotent: a re-ingest of an unchanged chart
skips bytes already on disk, and gives image-level duplicate detection
something to key on.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from config import IMAGE_SUFFIXES, ensure_chart_dirs, pages_dir
from db import (
    connect,
    create_job,
    init_page_stages,
    link_manifest_to_chart,
    set_chart_status,
    sha256_file,
    update_job,
    upsert_chart,
    upsert_pages,
)
from db.blob_store import (
    chart_name_from_blob_path,
    download_blob_to_path,
    list_image_blobs,
)
from db.chart_status import refresh_chart_status

logger = logging.getLogger(__name__)


def _normalize_page_filename(index: int, original_name: str) -> str:
    suffix = Path(original_name).suffix.lower() or ".jpg"
    if suffix == ".jpeg":
        suffix = ".jpg"
    return f"{index}{suffix}"


def _finish_registration(
    conn: Any,
    chart_id: int,
    chart_name: str,
    page_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Shared tail of blob ingest and local registration."""
    pages = upsert_pages(conn, chart_id, page_rows)
    init_page_stages(conn, chart_id)
    linked = link_manifest_to_chart(conn, chart_id, chart_name)
    if linked:
        logger.info("chart %s: linked %s manifest row(s)", chart_name, linked)
    progress = refresh_chart_status(conn, chart_id)
    return {"pages": pages, "manifest_rows_linked": linked, "progress": progress}


def run_download(
    *,
    blob_container: str,
    blob_path: str,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    chart_id: Optional[int] = None,
) -> dict[str, Any]:
    chart_name = chart_name_from_blob_path(blob_path)
    ensure_chart_dirs(chart_name)

    with connect() as conn:
        chart = upsert_chart(
            conn,
            chart_name=chart_name,
            status="downloading",
            source="blob",
            blob_container=blob_container,
            blob_path=blob_path.strip("/"),
            run_id=run_id,
            batch_id=batch_id,
        )
        chart_id = chart["id"]
        job_id = create_job(
            conn, chart_id=chart_id, stage_name="download_blob", status="running"
        )
        update_job(conn, job_id, started=True)

    try:
        blob_names = list_image_blobs(blob_container, blob_path)
        if not blob_names:
            raise RuntimeError(f"No images found under {blob_container}/{blob_path}")

        dest_root = pages_dir(chart_name)
        page_rows: list[dict[str, Any]] = []
        reused = 0

        for idx, blob_name in enumerate(blob_names, start=1):
            local_name = _normalize_page_filename(idx, Path(blob_name).name)
            dest = dest_root / local_name
            if dest.is_file() and dest.stat().st_size > 0:
                # Already downloaded — a resumed ingest does not re-pull bytes.
                reused += 1
            else:
                logger.info("Downloading %s -> %s", blob_name, dest)
                download_blob_to_path(blob_container, blob_name, dest)
            page_rows.append(
                {
                    "page_name": local_name,
                    "page_number": idx,
                    "image_sha256": sha256_file(dest),
                    "file_size_bytes": dest.stat().st_size,
                }
            )

        with connect() as conn:
            upsert_chart(
                conn,
                chart_name=chart_name,
                page_count=len(page_rows),
                status="processing",
                source="blob",
                blob_container=blob_container,
                blob_path=blob_path.strip("/"),
                run_id=run_id,
                batch_id=batch_id,
            )
            tail = _finish_registration(conn, chart_id, chart_name, page_rows)
            update_job(
                conn,
                job_id,
                status="completed",
                completed=True,
                pages_total=len(page_rows),
                pages_done=len(page_rows),
            )

        logger.info(
            "chart %s: %s page(s) registered (%s reused from disk)",
            chart_name, len(page_rows), reused,
        )
        return {
            "chart_id": chart_id,
            "chart_name": chart_name,
            "page_count": len(page_rows),
            "pages_reused": reused,
            "job_id": job_id,
            **tail,
        }
    except Exception as exc:
        with connect() as conn:
            set_chart_status(conn, chart_id, "failed")
            update_job(
                conn, job_id, status="failed", error_message=str(exc), completed=True
            )
        raise


def register_local_pages(
    chart_name: str,
    *,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> dict[str, Any]:
    """Register an already-present ``data/folders/<chart>`` (dev / demo)."""
    from db.paths import list_local_pages

    ensure_chart_dirs(chart_name)
    files = list_local_pages(chart_name)
    if not files:
        raise RuntimeError(f"No pages under {pages_dir(chart_name)}")

    page_rows = [
        {
            "page_name": path.name,
            "page_number": index,
            "image_sha256": sha256_file(path),
            "file_size_bytes": path.stat().st_size,
        }
        for index, path in enumerate(files, start=1)
    ]

    with connect() as conn:
        chart = upsert_chart(
            conn,
            chart_name=chart_name,
            page_count=len(page_rows),
            status="processing",
            source="local",
            run_id=run_id,
            batch_id=batch_id,
        )
        chart_id = chart["id"]
        tail = _finish_registration(conn, chart_id, chart_name, page_rows)
        chart = conn.execute(
            "SELECT * FROM chart_list WHERE id = %s", (chart_id,)
        ).fetchone()

    return {
        "chart_id": chart_id,
        "chart_name": chart_name,
        "page_count": len(page_rows),
        "status": chart["status"],
        **tail,
    }
