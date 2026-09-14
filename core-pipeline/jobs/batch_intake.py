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
    local_root: Optional[str | Path] = None,
    blob_container: Optional[str] = None,
    blob_prefix: Optional[str] = None,
    force: bool = False,
    run_pipeline: bool = True,
    only: Optional[list[str]] = None,
    through: Optional[str] = None,
    limit: Optional[int] = None,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> dict[str, Any]:
    """Scan a local folder or blob prefix and run every chart found, in order.

    Each chart goes through exactly the same call ``/api/charts/run`` makes —
    ``ingest_and_run`` — so a batch of one is indistinguishable from a single
    run, and an option added to run works here without being plumbed twice.

    Charts run one at a time on purpose: each already parallelises across its
    pages, and stage 5 is billed per page, so overlapping charts multiplies
    memory and spend without finishing sooner. One bad folder is recorded and
    the batch carries on.
    """
    from orchestrator.runner import ingest_and_run

    if bool(local_root) == bool(blob_container or blob_prefix):
        raise ValueError(
            "Provide either local_root, or both blob_container and blob_prefix"
        )
    if (blob_container or blob_prefix) and not (blob_container and blob_prefix):
        raise ValueError("blob_container and blob_prefix must be given together")

    started = time.time()
    results: list[dict[str, Any]] = []

    # Enumerate first, in whichever vocabulary the source speaks, then run one
    # loop over the result. `source` is what ingest_and_run is given; `name` is
    # only for the log and the summary.
    if local_root:
        folders = find_local_chart_folders(local_root)
        sources = [(str(f), f.name, "local") for f in folders]
        where = str(local_root)
    else:
        from db.blob_store import chart_name_from_blob_path, list_chart_prefixes

        prefixes = list_chart_prefixes(blob_container, blob_prefix)
        sources = [(p, chart_name_from_blob_path(p), "blob") for p in prefixes]
        where = f"{blob_container}/{blob_prefix}"

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
