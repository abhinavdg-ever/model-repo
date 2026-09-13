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
    move: bool = False,
    force: bool = False,
    load_manifest: bool = True,
    run_pipeline: bool = True,
    limit: Optional[int] = None,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> dict[str, Any]:
    """Scan a local folder or blob prefix and run every chart found, in order."""
    if bool(local_root) == bool(blob_container or blob_prefix):
        raise ValueError(
            "Provide either local_root, or both blob_container and blob_prefix"
        )
    if (blob_container or blob_prefix) and not (blob_container and blob_prefix):
        raise ValueError("blob_container and blob_prefix must be given together")

    started = time.time()
    results: list[dict[str, Any]] = []

    if local_root:
        from stages.download_blob import import_local_folder

        folders = find_local_chart_folders(local_root)
        if limit:
            folders = folders[:limit]
        logger.info("Batch: %d chart folder(s) under %s", len(folders), local_root)
        for index, folder in enumerate(folders, start=1):
            logger.info("[%d/%d] %s", index, len(folders), folder.name)
            entry: dict[str, Any] = {"source": str(folder), "name": folder.name}
            try:
                imported = import_local_folder(
                    folder,
                    move=move,
                    force=force,
                    load_manifest=load_manifest,
                    run_id=run_id,
                    batch_id=batch_id,
                )
                entry.update(
                    chart_id=imported["chart_id"],
                    chart_name=imported["chart_name"],
                    pages=imported["page_count"],
                    status="completed",
                )
                if run_pipeline:
                    _run_one(imported["chart_id"], entry)
            except Exception as exc:
                logger.exception("[%d/%d] FAILED %s", index, len(folders), folder.name)
                entry.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            results.append(entry)
    else:
        from db.blob_store import chart_name_from_blob_path, list_chart_prefixes
        from orchestrator.runner import ingest_and_run

        prefixes = list_chart_prefixes(blob_container, blob_prefix)
        if limit:
            prefixes = prefixes[:limit]
        logger.info(
            "Batch: %d chart folder(s) under %s/%s",
            len(prefixes), blob_container, blob_prefix,
        )
        for index, prefix in enumerate(prefixes, start=1):
            name = chart_name_from_blob_path(prefix)
            logger.info("[%d/%d] %s", index, len(prefixes), name)
            entry = {"source": prefix, "name": name}
            try:
                out = ingest_and_run(
                    blob_container=blob_container,
                    blob_path=prefix,
                    run_id=run_id,
                    batch_id=batch_id,
                    run_pipeline=run_pipeline,
                    force=force,
                )
                entry.update(
                    chart_id=(out or {}).get("chart_id"),
                    chart_name=name,
                    status="completed",
                )
            except Exception as exc:
                logger.exception("[%d/%d] FAILED %s", index, len(prefixes), name)
                entry.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            results.append(entry)

    summary = _summarise(results, started)
    logger.info(
        "Batch finished: %d/%d completed, %d failed, %.1fs",
        summary["completed"], summary["charts_found"],
        summary["failed"], summary["duration_seconds"],
    )
    return summary


def _run_one(chart_id: int, entry: dict[str, Any]) -> None:
    from orchestrator.runner import run_pipeline_for_chart

    run_pipeline_for_chart(chart_id)
    entry["pipeline"] = "ran"
