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
from stages._support import stage_label
from stages import (
    blank_junk_classify,
    dos_extract,
    member_extract_verify,
    ocr_final1_docling,
    ocr_final2_azure,
    ocr_prelim_tesseract,
    quality_rotation_hw,
    section_headers,
)
from stages.download_blob import run_download

logger = logging.getLogger(__name__)

StageFn = Callable[..., Any]

# (stage_name, pass_no, callable). Order matches pipeline_stage.seq; that table
# is the source of truth for progress reporting, this list for execution.
STAGE_CHAIN: list[tuple[str, int, StageFn]] = [
    # Rotation runs FIRST so every OCR pass reads an upright page. It needs no
    # OCR output of its own — it works from pixels — and a 90-degree page OCRs
    # badly in all three engines, so measuring before correcting was costing
    # accuracy on every rotated scan.
    ("ocr_quality", 1, quality_rotation_hw.run),
    ("ocr_prelim", 1, ocr_prelim_tesseract.run),
    ("blank_junk", 1, blank_junk_classify.run_pass1),
    ("ocr_final1", 1, ocr_final1_docling.run),
    ("ocr_final2", 1, ocr_final2_azure.run),
    ("section_headers", 1, section_headers.run),
    ("blank_junk", 2, blank_junk_classify.run_pass2),
    ("member_verify", 1, member_extract_verify.run),
    ("dos_extract", 1, dos_extract.run),
]

STAGE_NAMES = [f"{name}:{pass_no}" for name, pass_no, _ in STAGE_CHAIN]


def _stage_key(name: str, pass_no: int) -> str:
    return f"{name}:{pass_no}"


def resolve_stage(token: str) -> int:
    """Index into STAGE_CHAIN for a stage token, or raise ValueError.

    Accepts "blank_junk:2" for an explicit pass and "dos_extract" for pass 1 —
    the same spelling `only` takes, so callers learn one vocabulary, not two.
    A bare name that exists only at pass 2 is NOT silently promoted: naming a
    stage that does not exist should be an error the caller sees immediately,
    not a run that quietly does something else.
    """
    token = (token or "").strip()
    if not token:
        raise ValueError("empty stage name")
    if ":" in token:
        name, _, raw_pass = token.partition(":")
        try:
            pass_no = int(raw_pass)
        except ValueError:
            raise ValueError(
                f"unknown stage {token!r} — pass must be a number, e.g. blank_junk:2"
            ) from None
    else:
        name, pass_no = token, 1
    for index, (chain_name, chain_pass, _) in enumerate(STAGE_CHAIN):
        if chain_name == name and chain_pass == pass_no:
            return index
    raise ValueError(
        f"unknown stage {token!r} — known: {', '.join(STAGE_NAMES)}"
    )


def run_pipeline_for_chart(
    chart_id: int,
    *,
    force: bool = True,
    only: Optional[list[str]] = None,
    through: Optional[str] = None,
    skip_ocr: Optional[bool] = None,
) -> dict[str, Any]:
    """Run the stage chain for one chart.

    Two independent ways to run less than the whole chain:

    * ``through="ocr_final2"`` runs the chain from the top and stops after that
      stage — "everything up to here".
    * ``only=["dos_extract"]`` runs just those stages, whatever came before.
      Useful when an earlier stage's output is already on disk and only the
      last step changed.

    They compose: ``through`` bounds the chain, ``only`` filters within it.
    Neither disturbs the recorded progress of the stages it does not run.

    ``skip_ocr`` overrides the ``SKIP_OCR`` env for this run (``None`` = env).
    With ``skip_ocr`` active and ``force=false``, quality is refreshed and
    gate-delta reopens only the blank/junk / OCR pages whose path changed
    (or that are missing artifacts the new gate requires).
    """
    stop_at = resolve_stage(through) if through else None
    with connect() as conn:
        chart = get_chart(conn, chart_id)
        if not chart:
            raise RuntimeError(f"chart_id={chart_id} not found")
        if not force:
            from db import clear_stuck_processing

            stuck = clear_stuck_processing(conn, chart_id)
            if stuck:
                logger.info(
                    "Resume chart %s: cleared %d stuck processing page-stage row(s)",
                    chart["chart_name"],
                    stuck,
                )
        job_id = create_job(
            conn, chart_id=chart_id, stage_name="pipeline_full", status="running"
        )
        update_job(conn, job_id, started=True)
        progress = refresh_chart_status(conn, chart_id)

    wanted = set(only or [])
    chain = STAGE_CHAIN if stop_at is None else STAGE_CHAIN[: stop_at + 1]
    results: dict[str, Any] = {
        "chart_id": chart_id,
        "chart_name": chart["chart_name"],
        "stages": {},
        "skipped_stages": [],
        "progress": progress,
    }
    if stop_at is not None:
        results["through"] = STAGE_NAMES[stop_at]
        # The stages past the stop are not failures and not "skipped by filter"
        # either — they were never in scope. Naming them keeps a partial run
        # distinguishable from a chain that died early.
        results["not_run"] = STAGE_NAMES[stop_at + 1 :]
        logger.info(
            "Chart %s: running through [%s] — %d of %d stage(s)",
            chart["chart_name"], stage_label(*STAGE_CHAIN[stop_at][:2]), len(chain),
            len(STAGE_CHAIN),
        )

    from logging_setup import reset_current_chart, set_current_chart

    chart_token = set_current_chart(chart["chart_name"])
    try:
        total_stages = len(chain)
        skip_ocr_active = False
        ocr_hydrated = False
        adaptive_gates = False
        old_gates: dict[int, Any] = {}
        old_presence: dict[int, Any] = {}
        gate_delta_applied = False

        quality_in_scope = (not wanted) or (
            "ocr_quality" in wanted or "ocr_quality:1" in wanted
        )
        from stages.ocr_reuse import apply_skip_ocr, should_skip_ocr_stages

        will_skip_ocr = should_skip_ocr_stages(
            chart_name=chart["chart_name"],
            chart_id=chart_id,
            force=force,
            skip_ocr=skip_ocr,
        )
        # Adaptive: skip_ocr + force:false + quality in this run → refresh
        # quality, then reopen only gate-impacted pages (OCR engines stay
        # skipped for unchanged pages).
        if will_skip_ocr and quality_in_scope:
            from stages.gate_delta import snapshot_gates, snapshot_ocr_presence

            adaptive_gates = True
            skip_ocr_active = True
            with connect() as conn:
                old_gates = snapshot_gates(conn, chart_id)
                old_presence = snapshot_ocr_presence(conn, chart_id)
            results["ocr_reuse"] = apply_skip_ocr(chart_id, chart["chart_name"])
            ocr_hydrated = True
            logger.info(
                "Adaptive skip_ocr for chart %s — quality refresh + gate-delta",
                chart["chart_name"],
            )

        for index, (name, pass_no, fn) in enumerate(chain, start=1):
            key = f"{name}:{pass_no}"
            if wanted and key not in wanted and name not in wanted:
                results["skipped_stages"].append(key)
                continue

            # Adaptive path: always re-measure quality, then invalidate by delta.
            if adaptive_gates and name == "ocr_quality" and not gate_delta_applied:
                label = stage_label(name, pass_no)
                logger.info(
                    "=== [%s]  stage %d of %d  —  chart %s (force quality) ===",
                    label, index, total_stages, chart["chart_name"],
                )
                results["stages"][key] = fn(chart_id, force=True)
                from stages.gate_delta import apply_adaptive_gate_delta

                with connect() as conn:
                    plan = apply_adaptive_gate_delta(
                        conn,
                        chart_id,
                        old_gates=old_gates,
                        old_presence=old_presence,
                    )
                results["gate_delta"] = {
                    "pages_reopened": len(plan.reasons),
                    "reasons": {str(k): v for k, v in plan.reasons.items()},
                    "force_prelim": sorted(plan.force_prelim),
                    "force_final1": sorted(plan.force_final1),
                    "force_final2": sorted(plan.force_final2),
                    "invalidate": {
                        f"{s}:{p}": sorted(pids)
                        for (s, p), pids in plan.invalidate.items()
                    },
                }
                gate_delta_applied = True
                with connect() as conn:
                    progress = refresh_chart_status(conn, chart_id)
                results["progress"] = progress
                logger.info(
                    "[%s] done — gate-delta reopened %d page(s)",
                    label,
                    len(plan.reasons),
                )
                continue

            if name in {"ocr_prelim", "ocr_final1", "ocr_final2"}:
                if not ocr_hydrated:
                    skip_ocr_active = should_skip_ocr_stages(
                        chart_name=chart["chart_name"],
                        chart_id=chart_id,
                        force=force,
                        skip_ocr=skip_ocr,
                    )
                    if skip_ocr_active:
                        results["ocr_reuse"] = apply_skip_ocr(
                            chart_id, chart["chart_name"]
                        )
                    ocr_hydrated = True
                if skip_ocr_active and adaptive_gates:
                    # Per-page: only pages reset to pending by gate-delta run.
                    label = stage_label(name, pass_no)
                    logger.info(
                        "=== [%s]  stage %d of %d  —  chart %s "
                        "(skip_ocr + gate-delta pending only) ===",
                        label, index, total_stages, chart["chart_name"],
                    )
                    results["stages"][key] = fn(chart_id, force=False)
                    with connect() as conn:
                        progress = refresh_chart_status(conn, chart_id)
                    results["progress"] = progress
                    logger.info(
                        "[%s] done — chart status=%s, next=%s",
                        label, progress.get("status"),
                        progress.get("current_stage") or "finished",
                    )
                    continue
                if skip_ocr_active:
                    reason = (results.get("ocr_reuse") or {}).get("source") or "reuse"
                    results["skipped_stages"].append(key)
                    results["stages"][key] = {
                        "skipped": True,
                        "reason": f"skip_ocr_{reason}",
                    }
                    logger.info(
                        "=== [%s]  skipped (skip_ocr, %s)  —  chart %s ===",
                        stage_label(name, pass_no), reason, chart["chart_name"],
                    )
                    continue

            label = stage_label(name, pass_no)
            logger.info(
                "=== [%s]  stage %d of %d  —  chart %s ===",
                label, index, total_stages, chart["chart_name"],
            )
            # Under adaptive skip_ocr, downstream stages also honour resume
            # (force=False) so only gate-delta-invalidated pages re-run.
            stage_force = False if adaptive_gates else force
            results["stages"][key] = fn(chart_id, force=stage_force)

            with connect() as conn:
                progress = refresh_chart_status(conn, chart_id)
            results["progress"] = progress
            logger.info(
                "[%s] done — chart status=%s, next=%s",
                label, progress.get("status"),
                progress.get("current_stage") or "finished",
            )

        with connect() as conn:
            progress = refresh_chart_status(conn, chart_id)
            update_job(conn, job_id, status="completed", completed=True)

        results["progress"] = progress
        results["status"] = progress.get("status")
        return results

    except Exception as exc:
        logger.error("Pipeline failed for chart %s: %s", chart["chart_name"], exc)
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
    finally:
        reset_current_chart(chart_token)


def ingest_and_run(
    *,
    blob_container: Optional[str] = None,
    blob_path: Optional[str] = None,
    local_path: Optional[str] = None,
    chart_name: Optional[str] = None,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    run_pipeline: bool = True,
    force: bool = True,
    only: Optional[list[str]] = None,
    through: Optional[str] = None,
    skip_ocr: Optional[bool] = None,
) -> dict[str, Any]:
    """Fetch one chart's pages into the workspace, then run the chain on it.

    The source is either a blob prefix or a local directory; both end with the
    pages under data/folders/<chart>/pages as 1.jpg, 2.jpg …, so everything
    downstream is identical either way. This is the whole of what /api/charts/run
    does, and what batch calls once per folder.
    """
    if bool(blob_path) == bool(local_path):
        raise ValueError("Provide exactly one of blob_path (+ blob_container) or local_path")

    from db.path_ids import resolve_run_batch
    from logging_setup import reset_current_chart, set_current_chart
    from pathlib import Path as _Path

    # Bind the folder name as early as possible so intake/download lines show it.
    hint = (chart_name or "").strip()
    if not hint and local_path:
        hint = _Path(local_path).name
    elif not hint and blob_path:
        hint = _Path(str(blob_path).rstrip("/")).name
    chart_token = set_current_chart(hint or None)

    run_id, batch_id = resolve_run_batch(
        run_id, batch_id, blob_path, local_path, blob_container
    )

    try:
        if local_path:
            from stages.download_blob import import_local_folder

            intake = import_local_folder(
                local_path,
                chart_name=chart_name,
                force=force,
                run_id=run_id,
                batch_id=batch_id,
            )
            out: dict[str, Any] = {
                "chart_id": intake["chart_id"],
                "chart_name": intake["chart_name"],
                "page_count": intake["page_count"],
                "source": intake["source"],
                "imported": intake["imported"],
                "manifest": intake["manifest"],
            }
        else:
            if not blob_container:
                raise ValueError("blob_container is required with blob_path")
            download = run_download(
                blob_container=blob_container,
                blob_path=blob_path,
                run_id=run_id,
                batch_id=batch_id,
                force=force,
            )
            out = {
                "chart_id": download["chart_id"],
                "chart_name": download["chart_name"],
                "page_count": download["page_count"],
                "source": f"{blob_container}/{blob_path}",
            }

        # Prefer the registered chart name once intake finishes.
        set_current_chart(out.get("chart_name") or hint or None)

        if run_pipeline:
            out["pipeline"] = run_pipeline_for_chart(
                out["chart_id"],
                force=force,
                only=only,
                through=through,
                skip_ocr=skip_ocr,
            )
        return out
    finally:
        reset_current_chart(chart_token)
