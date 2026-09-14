"""Batch intake: scan a parent folder or blob prefix and run every chart in it.

One chart per subfolder. Charts run strictly one at a time — each already fans
out across pages internally (STAGE_WORKERS), and stage 5 is billed per page, so
running charts concurrently on top of that multiplies both memory and spend
without finishing the batch sooner.

A chart that fails does not stop the batch: the error is recorded against that
chart and the run continues, because the common failure is one bad folder in a
drop of fifty.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from config import IMAGE_SUFFIXES

logger = logging.getLogger(__name__)


def find_local_chart_folders(root: str | Path) -> list[Path]:
    """Immediate subfolders of `root` that contain at least one image.

    Also treats `root` itself as a single chart when it holds images directly,
    so pointing at either a drop of many charts or one chart folder works.
    """
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise RuntimeError(f"Not a directory: {base}")

    def has_images(d: Path) -> bool:
        return any(
            p.is_file()
            and p.suffix.lower() in IMAGE_SUFFIXES
            and not p.name.startswith("._")
            for p in d.iterdir()
        )

    if has_images(base):
        return [base]
    return sorted(
        (d for d in base.iterdir() if d.is_dir() and not d.name.startswith(".") and has_images(d)),
        key=lambda d: d.name.casefold(),
    )


def _write_one(
    chart_name: str,
    *,
    local_write_path: Optional[str],
    blob_container: Optional[str],
    blob_write_path: Optional[str],
    write_mode: str,
    overwrite: bool,
) -> dict[str, Any]:
    """Write one finished chart, recording the outcome rather than raising.

    A write failure must not mark a chart failed: the pipeline ran, the results
    are in the workspace, and POST /api/charts/write can retry without
    reprocessing. In a batch of fifty it must also not stop the other
    forty-nine — one unwritable destination is exactly the kind of thing that
    should be reported and stepped over.
    """
    from jobs.export_chart import write_chart

    try:
        result = write_chart(
            chart_name,
            local_path=local_write_path,
            blob_container=blob_container,
            blob_path=blob_write_path,
            overwrite=overwrite,
            write_mode=write_mode,
        )
        return {
            "status": "written",
            "destination": result["destination"],
            "files_written": result["files_written"],
        }
    except Exception as exc:
        logger.warning("  chart %s ran but was not written: %s", chart_name, exc)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _summarise(results: list[dict[str, Any]], started: float) -> dict[str, Any]:
    ok = [r for r in results if r["status"] == "completed"]
    failed = [r for r in results if r["status"] == "failed"]
    return {
        "charts_found": len(results),
        "completed": len(ok),
        "failed": len(failed),
        "duration_seconds": round(time.time() - started, 1),
        "charts": results,
    }


def run_batch(
    *,
    local_read_path: Optional[str | Path] = None,
    blob_container: Optional[str] = None,
    blob_read_path: Optional[str] = None,
    local_write_path: Optional[str] = None,
    blob_write_path: Optional[str] = None,
    write_mode: str = "skip_orig_pages",
    overwrite: bool = False,
    force: bool = False,
    run_pipeline: bool = True,
    only: Optional[list[str]] = None,
    through: Optional[str] = None,
    limit: Optional[int] = None,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> dict[str, Any]:
    """Scan a read path, run every chart folder under it, and optionally write.

    Same vocabulary as /api/charts/run, minus the folder name: here every
    sub-folder IS a chart, so each supplies its own. A chart read from
    `<read_path>/52754737_48221214/` is written to
    `<write_path>/52754737_48221214/`.


    Each chart goes through exactly the same call ``/api/charts/run`` makes —
    ``ingest_and_run`` — so a batch of one is indistinguishable from a single
    run, and an option added to run works here without being plumbed twice.

    Charts run one at a time on purpose: each already parallelises across its
    pages, and stage 5 is billed per page, so overlapping charts multiplies
    memory and spend without finishing sooner. One bad folder is recorded and
    the batch carries on.
    """
    from orchestrator.runner import ingest_and_run

    if bool(local_read_path) == bool(blob_container or blob_read_path):
        raise ValueError(
            "Provide either local_read_path, or both blob_container and blob_read_path"
        )
    if (blob_container or blob_read_path) and not (blob_container and blob_read_path):
        raise ValueError("blob_container and blob_read_path must be given together")
    if local_read_path and blob_write_path:
        raise ValueError("a local source writes to local_write_path")
    if blob_read_path and local_write_path:
        raise ValueError("a blob source writes to blob_write_path")

    started = time.time()
    results: list[dict[str, Any]] = []

    # Enumerate first, in whichever vocabulary the source speaks, then run one
    # loop over the result. `source` is what ingest_and_run is given; `name` is
    # only for the log and the summary.
    if local_read_path:
        folders = find_local_chart_folders(local_read_path)
        sources = [(str(f), f.name, "local") for f in folders]
        where = str(local_read_path)
    else:
        from db.blob_store import chart_name_from_blob_path, list_chart_prefixes

        prefixes = list_chart_prefixes(blob_container, blob_read_path)
        sources = [(p, chart_name_from_blob_path(p), "blob") for p in prefixes]
        where = f"{blob_container}/{blob_read_path}"

    if limit:
        sources = sources[:limit]
    logger.info("Batch: %d chart folder(s) under %s", len(sources), where)

    for index, (source, name, mode) in enumerate(sources, start=1):
        logger.info("[%d/%d] %s", index, len(sources), name)
        entry: dict[str, Any] = {"source": source, "name": name}
        try:
            out = ingest_and_run(
                local_path=source if mode == "local" else None,
                blob_container=blob_container if mode == "blob" else None,
                blob_path=source if mode == "blob" else None,
                run_id=run_id,
                batch_id=batch_id,
                run_pipeline=run_pipeline,
                force=force,
                only=only,
                through=through,
            )
            entry.update(
                chart_id=out.get("chart_id"),
                chart_name=out.get("chart_name", name),
                pages=out.get("page_count"),
                status="completed",
            )
            if local_write_path or blob_write_path:
                entry["write"] = _write_one(
                    out.get("chart_name") or name,
                    local_write_path=local_write_path,
                    blob_container=blob_container,
                    blob_write_path=blob_write_path,
                    write_mode=write_mode,
                    overwrite=overwrite,
                )
        except Exception as exc:
            logger.exception("[%d/%d] FAILED %s", index, len(sources), name)
            entry.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        results.append(entry)

    summary = _summarise(results, started)
    logger.info(
        "Batch finished: %d/%d completed, %d failed, %.1fs",
        summary["completed"], summary["charts_found"],
        summary["failed"], summary["duration_seconds"],
    )
    return summary
