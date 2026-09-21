"""Batch intake: scan a parent folder or blob prefix and run every chart in it.

Flow for a submitted drop:

  1. Enumerate every chart folder under the read path.
  2. Pre-register every chart into ``chart_list`` (status ``received``) so the
     batch is visible before any work starts.
  3. Ingest + run charts with a thread pool of ``workers`` (default
     ``BATCH_WORKERS``, usually 4). One bad folder is recorded; the rest continue.

Each chart already fans out across its pages (``STAGE_WORKERS``). Chart-level
concurrency saturates the box on drops of small charts; a single huge chart
still benefits mainly from the page pool. Stage 5 is globally capped by
``AZURE_DI_MAX_CONCURRENT`` so overlapping charts do not multiply DI spend
unbounded.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from config import (
    BATCH_POOL_HEADROOM,
    BATCH_WORKERS,
    IMAGE_SUFFIXES,
    LARGE_CHART_MIN_PAGES,
    STAGE_WORKERS,
)

logger = logging.getLogger(__name__)


class LargeChartLimiter:
    """At most one ≥N-page chart while any smaller chart is still pending.

    When only large charts remain, the normal worker pool runs them in parallel.
    """

    def __init__(self, small_remaining: int) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._small_remaining = max(0, int(small_remaining))
        self._large_running = 0

    def enter(self, is_large: bool) -> None:
        if not is_large:
            return
        with self._cond:
            while self._small_remaining > 0 and self._large_running >= 1:
                self._cond.wait()
            self._large_running += 1

    def leave(self, is_large: bool) -> None:
        with self._cond:
            if is_large:
                self._large_running = max(0, self._large_running - 1)
                self._cond.notify_all()
            else:
                if self._small_remaining > 0:
                    self._small_remaining -= 1
                    self._cond.notify_all()


def estimate_chart_pages(
    source: str,
    mode: str,
    *,
    blob_container: Optional[str] = None,
) -> int:
    """Best-effort page count before ingest (for large-chart scheduling)."""
    try:
        if mode == "local":
            from stages.download_blob import _collect_images

            return len(_collect_images(Path(source), recursive=True))
        if mode == "blob" and blob_container:
            from db.blob_store import list_image_blobs

            return len(list_image_blobs(blob_container, source))
    except Exception:
        logger.debug("page estimate failed for %s", source, exc_info=True)
    return 0


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


def resolve_batch_workers(requested: Optional[int] = None) -> int:
    """Return the worker count to use, or raise if it cannot fit the DB pool.

    ``workers × STAGE_WORKERS + BATCH_POOL_HEADROOM ≤ DB_POOL_MAX``.
    """
    want = BATCH_WORKERS if requested is None else int(requested)
    want = max(1, want)
    assert_batch_workers_fit(want)
    return want


def assert_batch_workers_fit(workers: int) -> None:
    """Raise ValueError naming both sides when the pool cannot fit the batch."""
    from db import DB_POOL_MAX

    need = int(workers) * STAGE_WORKERS + BATCH_POOL_HEADROOM
    if need > DB_POOL_MAX:
        raise ValueError(
            f"workers={workers} × STAGE_WORKERS={STAGE_WORKERS} + "
            f"headroom={BATCH_POOL_HEADROOM} = {need} exceeds "
            f"DB_POOL_MAX={DB_POOL_MAX}. Lower workers / STAGE_WORKERS or "
            f"raise DB_POOL_MAX."
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
    are in the workspace, and POST /api/charts/run with chart_name + a write
    path can retry (sync skips files already at the destination). In a batch of fifty it must also not stop the other
    forty-nine — one unwritable destination is exactly the kind of thing that
    should be reported and stepped over.
    """
    from db import connect, set_chart_output_path
    from db.path_ids import resolve_output_path
    from jobs.export_chart import write_chart

    write = blob_write_path or local_write_path
    if write:
        out_path = resolve_output_path(chart_name, write_path=write)
        if out_path:
            try:
                with connect() as conn:
                    row = conn.execute(
                        "SELECT id FROM chart_list WHERE chart_name = %s",
                        (chart_name,),
                    ).fetchone()
                    if row:
                        set_chart_output_path(conn, int(row["id"]), out_path)
            except Exception:
                logger.debug(
                    "could not persist output_path for %s", chart_name, exc_info=True
                )

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
    skipped = [r for r in results if r["status"] == "skipped"]
    return {
        "charts_found": len(results),
        "completed": len(ok),
        "failed": len(failed),
        "skipped": len(skipped),
        "duration_seconds": round(time.time() - started, 1),
        "charts": results,
    }


def _pre_register(
    sources: list[tuple[str, str, str]],
    *,
    blob_container: Optional[str],
    run_id: Optional[str],
    batch_id: Optional[str],
    blob_write_path: Optional[str] = None,
    local_write_path: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Insert every chart into chart_list before any ingest/run starts.

    Status stays ``received`` until ingest flips it. Reviewers and
    ``GET /api/charts/by-name/...`` can see the full drop immediately.
    """
    from db import connect, upsert_chart
    from db.path_ids import resolve_output_path

    registered: list[dict[str, Any]] = []
    with connect() as conn:
        for source, name, mode in sources:
            write = blob_write_path if mode == "blob" else local_write_path
            out_path = resolve_output_path(
                name,
                write_path=write,
                read_path=source if mode == "blob" else (str(source) if source else None),
            )
            chart = upsert_chart(
                conn,
                chart_name=name,
                status="received",
                source=mode,
                blob_container=blob_container if mode == "blob" else None,
                blob_path=source if mode == "blob" else None,
                output_path=out_path,
                run_id=run_id,
                batch_id=batch_id,
            )
            registered.append(
                {
                    "chart_id": chart["id"],
                    "chart_name": name,
                    "source": source,
                    "mode": mode,
                    "status": chart.get("status") or "received",
                    "output_path": out_path,
                }
            )
    logger.info("Pre-registered %d chart(s) into chart_list", len(registered))
    return registered


def _note_progress(
    chart_name: str,
    current: int,
    total: int,
    *,
    batch_dir: Optional[str] = None,
    detail: str = "",
    unit: str = "charts",
    action: str = "processing",
    batch_n: Optional[int] = None,
) -> None:
    """Best-effort progress.txt under the chart and (for local batches) the parent."""
    try:
        from config import ensure_chart_dirs
        from db.paths import write_batch_folder_progress, write_folder_progress

        ensure_chart_dirs(chart_name)
        write_folder_progress(
            chart_name, current, total, action=action, detail=detail, unit=unit
        )
        if batch_dir:
            write_batch_folder_progress(
                batch_dir,
                batch_n if batch_n is not None else current,
                total,
                action=action,
                detail=detail or chart_name,
            )
    except OSError:
        logger.debug("progress write failed for %s", chart_name, exc_info=True)


def _run_one_chart(
    index: int,
    total: int,
    source: str,
    name: str,
    mode: str,
    *,
    blob_container: Optional[str],
    local_write_path: Optional[str],
    blob_write_path: Optional[str],
    write_mode: str,
    overwrite: bool,
    force: bool,
    run_pipeline: bool,
    only: Optional[list[str]],
    through: Optional[str],
    skip_ocr: Optional[bool],
    redownload_pages: bool,
    run_id: Optional[str],
    batch_id: Optional[str],
    counters: dict[str, int],
    counter_lock: threading.Lock,
    batch_progress_dir: Optional[str] = None,
    is_large: bool = False,
    large_limiter: Optional[LargeChartLimiter] = None,
) -> dict[str, Any]:
    from logging_setup import reset_current_chart, set_worker_name, set_current_chart
    from orchestrator.runner import ingest_and_run

    set_worker_name(f"batch-{index}")
    chart_token = set_current_chart(name)
    if large_limiter is not None:
        large_limiter.enter(is_large)
    try:
        with counter_lock:
            counters["started"] += 1
            started_n = counters["started"]
        # index = listing order; started_n = how many workers have begun (≠ when workers>1)
        logger.info(
            "[start %d/%d] %s (in flight %d%s)",
            index,
            total,
            name,
            started_n,
            f", large≥{LARGE_CHART_MIN_PAGES}" if is_large else "",
        )
        _note_progress(
            name,
            index,
            total,
            batch_dir=batch_progress_dir,
            detail=f"starting {name}",
            batch_n=started_n,
        )

        entry: dict[str, Any] = {
            "source": source,
            "name": name,
            "index": index,
            "of": total,
            "large_chart": is_large,
        }
        try:
            # Resume: skip charts that already finished every phase-1 stage.
            if not force:
                from db import connect, get_chart_by_name
                from db.chart_status import chart_is_pipeline_complete

                with connect() as conn:
                    existing = get_chart_by_name(conn, name)
                    if existing and chart_is_pipeline_complete(conn, int(existing["id"])):
                        entry.update(
                            chart_id=existing["id"],
                            chart_name=existing.get("chart_name") or name,
                            pages=existing.get("page_count"),
                            status="skipped",
                            skip_reason="already_complete",
                        )
                        logger.info(
                            "[skip %d/%d] %s already complete (resume)",
                            index,
                            total,
                            name,
                        )
                        with counter_lock:
                            counters["finished"] += 1
                            finished_n = counters["finished"]
                        _note_progress(
                            name,
                            finished_n,
                            total,
                            batch_dir=batch_progress_dir,
                            action="skipped",
                            detail=f"{name} already complete",
                            batch_n=finished_n,
                        )
                        return entry

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
                skip_ocr=skip_ocr,
                redownload_pages=redownload_pages,
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
            logger.exception("[fail %s] %s", name, exc)
            entry.update(status="failed", error=f"{type(exc).__name__}: {exc}")

        with counter_lock:
            counters["finished"] += 1
            finished_n = counters["finished"]
        logger.info("[done %d/%d] %s -> %s", finished_n, total, name, entry["status"])
        _note_progress(
            name,
            finished_n,
            total,
            batch_dir=batch_progress_dir,
            action="completed" if entry["status"] == "completed" else entry["status"],
            detail=name,
            batch_n=finished_n,
        )
        return entry
    finally:
        reset_current_chart(chart_token)
        if large_limiter is not None:
            large_limiter.leave(is_large)


def run_batch(
    *,
    local_read_path: Optional[str | Path] = None,
    blob_container: Optional[str] = None,
    blob_read_path: Optional[str] = None,
    local_write_path: Optional[str] = None,
    blob_write_path: Optional[str] = None,
    write_mode: str = "skip_orig_pages",
    overwrite: bool = False,
    force: bool = True,
    run_pipeline: bool = True,
    only: Optional[list[str]] = None,
    through: Optional[str] = None,
    skip_ocr: Optional[bool] = None,
    redownload_pages: bool = False,
    limit: Optional[int] = None,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    workers: Optional[int] = None,
) -> dict[str, Any]:
    """Scan a read path, register every chart, then run with a worker pool.

    Same vocabulary as /api/charts/run, minus the folder name: here every
    sub-folder IS a chart, so each supplies its own. A chart read from
    `<read_path>/52754737_48221214/` is written to
    `<write_path>/52754737_48221214/`.

    Each chart goes through exactly the same call ``/api/charts/run`` makes —
    ``ingest_and_run`` — so a batch of one is indistinguishable from a single
    run, and an option added to run works here without being plumbed twice.
    """
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

    from db.path_ids import resolve_run_batch

    run_id, batch_id = resolve_run_batch(
        run_id, batch_id, blob_read_path, local_read_path
    )

    worker_count = resolve_batch_workers(workers)

    started = time.time()

    if local_read_path:
        folders = find_local_chart_folders(local_read_path)
        sources = [(str(f), f.name, "local") for f in folders]
        where = str(local_read_path)
        batch_progress_dir = str(Path(local_read_path).expanduser().resolve())
    else:
        from db.blob_store import (
            chart_name_from_blob_path,
            ensure_blob_ready,
            list_chart_prefixes,
        )

        # One token + container touch before workers fan out (avoids N parallel
        # browser prompts on entra_interactive, and warms IMDS for MI).
        ensure_blob_ready(blob_container)
        prefixes = list_chart_prefixes(blob_container, blob_read_path)
        sources = [(p, chart_name_from_blob_path(p), "blob") for p in prefixes]
        where = f"{blob_container}/{blob_read_path}"
        batch_progress_dir = None

    if limit:
        sources = sources[:limit]
    total = len(sources)

    # Estimate pages so ≥LARGE_CHART_MIN_PAGES charts do not run together while
    # smaller ones remain. Prefer small charts first in the submission order.
    page_estimates: list[int] = [
        estimate_chart_pages(source, mode, blob_container=blob_container)
        for source, _name, mode in sources
    ]
    large_flags = [n >= LARGE_CHART_MIN_PAGES for n in page_estimates]
    small_count = sum(1 for large in large_flags if not large)
    large_count = total - small_count
    # Stable partition: small first, then large (keeps relative order inside each).
    ordered_jobs: list[tuple[int, str, str, str, bool, int]] = []
    for index, ((source, name, mode), pages, is_large) in enumerate(
        zip(sources, page_estimates, large_flags), start=1
    ):
        ordered_jobs.append((index, source, name, mode, is_large, pages))
    ordered_jobs.sort(key=lambda row: (1 if row[4] else 0, row[0]))

    logger.info(
        "Batch: %d chart folder(s) under %s (workers=%d, STAGE_WORKERS=%d, "
        "large≥%d: %d, small: %d)",
        total,
        where,
        worker_count,
        STAGE_WORKERS,
        LARGE_CHART_MIN_PAGES,
        large_count,
        small_count,
    )
    if batch_progress_dir and total:
        try:
            from db.paths import write_batch_folder_progress

            write_batch_folder_progress(batch_progress_dir, 0, total, detail="starting")
        except OSError:
            logger.debug("batch progress seed failed", exc_info=True)

    registered = _pre_register(
        sources,
        blob_container=blob_container,
        run_id=run_id,
        batch_id=batch_id,
        blob_write_path=blob_write_path,
        local_write_path=local_write_path,
    )

    counters = {"started": 0, "finished": 0}
    counter_lock = threading.Lock()
    results: list[dict[str, Any]] = []
    large_limiter = LargeChartLimiter(small_count)

    if total == 0:
        summary = _summarise(results, started)
        summary["workers"] = worker_count
        summary["registered"] = registered
        return summary

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="batch") as pool:
        futures = []
        index_by_future: dict[Any, int] = {}
        for _orig_index, source, name, mode, is_large, _pages in ordered_jobs:
            # Summary order follows original listing (1..N), not small-first.
            fut = pool.submit(
                _run_one_chart,
                _orig_index,
                total,
                source,
                name,
                mode,
                blob_container=blob_container,
                local_write_path=local_write_path,
                blob_write_path=blob_write_path,
                write_mode=write_mode,
                overwrite=overwrite,
                force=force,
                run_pipeline=run_pipeline,
                only=only,
                through=through,
                skip_ocr=skip_ocr,
                redownload_pages=redownload_pages,
                run_id=run_id,
                batch_id=batch_id,
                counters=counters,
                counter_lock=counter_lock,
                batch_progress_dir=batch_progress_dir,
                is_large=is_large,
                large_limiter=large_limiter,
            )
            futures.append(fut)
            index_by_future[fut] = _orig_index - 1
        ordered: list[Optional[dict[str, Any]]] = [None] * total
        for fut in as_completed(futures):
            ordered[index_by_future[fut]] = fut.result()
        results = [r for r in ordered if r is not None]

    summary = _summarise(results, started)
    summary["workers"] = worker_count
    summary["registered"] = registered
    logger.info(
        "Batch finished: %d/%d completed, %d failed, workers=%d, %.1fs",
        summary["completed"], summary["charts_found"],
        summary["failed"], worker_count, summary["duration_seconds"],
    )
    return summary
