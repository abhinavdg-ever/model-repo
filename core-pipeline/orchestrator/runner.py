"""Pipeline orchestrator.

Runs the phase-1 stages in order and recomputes chart status after each one.

Two behaviours worth knowing:

* **Resumable.** Each stage asks ``pages_needing_stage`` which pages are still
  outstanding and does only those. A chart that died at DOS on page 400 of 500
  re-runs DOS for the pages that never finished — it does not re-OCR the chart
  or re-bill Azure Document Intelligence. ``force=True`` overrides that.

* **Continue-on-stage-failure is deliberate off.** A stage that raises aborts the
  chain, because every later stage reads what the failed one produced. Per-page
  failures are different: those are recorded on the page and the stage carries
  on, so one unreadable page does not stop a 500-page chart.
"""
from __future__ import annotations

import logging
import traceback
from typing import Any, Callable, Optional

from db import connect, create_job, get_chart, set_chart_status, update_job
from db.chart_status import refresh_chart_status
from stages import (
    blank_junk_classify,
    dos_extract,
    member_extract_verify,
    ocr_final1_docling,
    ocr_final2_azure,
    ocr_prelim_tesseract,
    quality_rotation_hw,
)
from stages.download_blob import run_download

logger = logging.getLogger(__name__)

StageFn = Callable[..., Any]

# (stage_name, pass_no, callable). Order matches pipeline_stage.seq; that table
# is the source of truth for progress reporting, this list for execution.
STAGE_CHAIN: list[tuple[str, int, StageFn]] = [
    ("ocr_prelim", 1, ocr_prelim_tesseract.run),
    ("ocr_quality", 1, quality_rotation_hw.run),
    ("blank_junk", 1, blank_junk_classify.run_pass1),
    ("ocr_final1", 1, ocr_final1_docling.run),
    ("ocr_final2", 1, ocr_final2_azure.run),
    ("blank_junk", 2, blank_junk_classify.run_pass2),
    ("member_verify", 1, member_extract_verify.run),
    ("dos_extract", 1, dos_extract.run),
]

STAGE_NAMES = [f"{name}:{pass_no}" for name, pass_no, _ in STAGE_CHAIN]


def run_pipeline_for_chart(
    chart_id: int,
    *,
    force: bool = False,
    only: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Run the stage chain for one chart.

    `only` restricts execution to named stages ("blank_junk:2", or "dos_extract"
    for pass 1) without disturbing the others' recorded progress.
    """
    with connect() as conn:
        chart = get_chart(conn, chart_id)
        if not chart:
            raise RuntimeError(f"chart_id={chart_id} not found")
        job_id = create_job(
            conn, chart_id=chart_id, stage_name="pipeline_full", status="running"
        )
        update_job(conn, job_id, started=True)
        progress = refresh_chart_status(conn, chart_id)

    wanted = set(only or [])
    results: dict[str, Any] = {
        "chart_id": chart_id,
        "chart_name": chart["chart_name"],
        "stages": {},
        "skipped_stages": [],
        "progress": progress,
    }

    try:
        for name, pass_no, fn in STAGE_CHAIN:
            key = f"{name}:{pass_no}"
            if wanted and key not in wanted and name not in wanted:
                results["skipped_stages"].append(key)
                continue

            logger.info("chart %s — stage %s", chart_id, key)
            results["stages"][key] = fn(chart_id, force=force)

            with connect() as conn:
                progress = refresh_chart_status(conn, chart_id)
            results["progress"] = progress
            logger.info(
                "chart %s — %s done → status=%s current_stage=%s",
                chart_id, key, progress.get("status"), progress.get("current_stage"),
            )

        with connect() as conn:
            progress = refresh_chart_status(conn, chart_id)
            update_job(conn, job_id, status="completed", completed=True)

        results["progress"] = progress
        results["status"] = progress.get("status")
        return results

    except Exception as exc:
        logger.error("Pipeline failed for chart %s: %s", chart_id, exc)
        logger.debug(traceback.format_exc())
        with connect() as conn:
            progress = refresh_chart_status(conn, chart_id)
            # refresh_chart_status only sees page-level state; a stage that blew
            # up before touching any page would otherwise leave the chart
            # looking merely "processing".
            set_chart_status(
                conn,
                chart_id,
                "failed",
                current_stage=progress.get("current_stage"),
                current_pass=progress.get("current_pass"),
            )
            update_job(
                conn, job_id, status="failed", error_message=str(exc), completed=True
            )
        results["status"] = "failed"
        results["progress"] = progress
        results["error"] = str(exc)
        raise


def ingest_and_run(
    *,
    blob_container: str,
    blob_path: str,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    run_pipeline: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    download = run_download(
        blob_container=blob_container,
        blob_path=blob_path,
        run_id=run_id,
        batch_id=batch_id,
    )
    chart_id = download["chart_id"]
    out: dict[str, Any] = {
        "chart_id": chart_id,
        "chart_name": download["chart_name"],
        "page_count": download["page_count"],
    }
    if run_pipeline:
        out["pipeline"] = run_pipeline_for_chart(chart_id, force=force)
    return out
