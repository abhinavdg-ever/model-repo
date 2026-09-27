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
    """At most one large (> LARGE_CHART_MIN_PAGES) chart runs at any time.

    Small charts pass straight through. A large chart that arrives while
    another is running waits for it; its worker slot waits with it.
    """

    def __init__(self) -> None:
        self._slot = threading.Semaphore(1)

    def enter(self, is_large: bool) -> None:
        if is_large:
            self._slot.acquire()

    def leave(self, is_large: bool) -> None:
        if is_large:
            self._slot.release()


def estimate_chart_pages(
    source: str,
    mode: str,
    *,
    blob_container: Optional[str] = None,
) -> int:
    """Best-effort page count before ingest (for large-chart scheduling)."""
    try:
        if mode == "local":
            from stages.utilities.download_blob import _collect_images

            return len(_collect_images(Path(source), recursive=True))
        if mode == "blob" and blob_container:
            from db.blob_store import list_image_blobs

            return len(list_image_blobs(blob_container, source))
    except Exception:
        logger.debug("page estimate failed for %s", source, exc_info=True)
    return 0


def is_large_chart(pages: int) -> bool:
    return pages > LARGE_CHART_MIN_PAGES


def submission_order(
    sources: list[tuple[str, str, str]],
    page_estimates: list[int],
) -> list[tuple[int, str, str, str, bool, int]]:
    """Alphabetical by chart name: ``(position, source, name, mode, is_large, pages)``.

    ``position`` is the 1-based alphabetical position — the chart N/X label and
    the summary order. ``is_large`` marks charts that must run one at a time.
    """
    paired = sorted(
        zip(sources, page_estimates), key=lambda sp: (sp[0][1].casefold(), sp[0][1])
    )
    return [
        (position, source, name, mode, is_large_chart(pages), pages)
        for position, ((source, name, mode), pages) in enumerate(paired, start=1)
    ]


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


def _chart_names_pipeline_complete(names: list[str]) -> set[str]:
    """Return chart names that already finished every phase-1 stage.

    Best-effort: an unreachable DB or unknown name means "not complete", so
    sample selection falls through to including them rather than blocking.
    """
    if not names:
        return set()
    try:
        from db import connect, get_chart_by_name, is_skip_db_write
        from db.chart_status import chart_is_pipeline_complete
    except Exception:
        return set()
    if is_skip_db_write():
        return set()
    done: set[str] = set()
    try:
        with connect() as conn:
            for name in names:
                try:
                    row = get_chart_by_name(conn, name)
                    if row and chart_is_pipeline_complete(conn, int(row["id"])):
                        done.add(name)
                except Exception:
                    logger.debug(
                        "complete-check failed for %s", name, exc_info=True
                    )
    except Exception:
        logger.debug("complete-check connect failed", exc_info=True)
        return set()
    return done


def select_batch_sources(
    sources: list[tuple[str, str, str]],
    sample: Optional[int],
    *,
    prefer_incomplete: bool = True,
) -> list[tuple[str, str, str]]:
    """Apply ``sample``: prefer charts that are not yet complete.

    When more folders exist than ``sample``:
    - take incomplete charts first (original sort order);
    - include completed ones only to fill the remaining slots.

    When the drop fits in ``sample`` (or there is no cap), every folder is kept
    — completed samples may come again.
    """
    if not sources:
        return []
    if sample is None or int(sample) <= 0:
        return list(sources)
    cap = int(sample)
    if len(sources) <= cap:
        return list(sources)
    if not prefer_incomplete:
        return list(sources[:cap])

    names = [name for _src, name, _mode in sources]
    complete = _chart_names_pipeline_complete(names)
    incomplete = [s for s in sources if s[1] not in complete]
    completed = [s for s in sources if s[1] in complete]
    if len(incomplete) >= cap:
        return incomplete[:cap]
    need = cap - len(incomplete)
    return incomplete + completed[:need]


def filter_sources_by_chart_names(
    sources: list[tuple[str, str, str]],
    chart_names: Optional[list[str]],
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Keep only charts whose folder name is in ``chart_names``.

    Returns ``(matched_sources, missing_names)``. Order of ``chart_names`` is
    preserved for matches. When ``chart_names`` is empty/None, every source is
    kept and missing is empty.
    """
    if not chart_names:
        return list(sources), []

    from db.paths import normalize_folder_name

    wanted: list[str] = []
    seen: set[str] = set()
    for raw in chart_names:
        name = normalize_folder_name(raw)
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        wanted.append(name)

    if not wanted:
        return list(sources), []

    by_key = {name.casefold(): (src, name, mode) for src, name, mode in sources}
    matched: list[tuple[str, str, str]] = []
    missing: list[str] = []
    for name in wanted:
        hit = by_key.get(name.casefold())
        if hit is None:
            missing.append(name)
        else:
            matched.append(hit)
    return matched, missing

# chart_list.status values that mean "this chart already finished the pipeline".
# All three read as Imaging Completed in review-ui; re-running any of them
# repeats the billed final2 stage for nothing.
FINISHED_CHART_STATUSES = frozenset({"completed", "needs_review", "rejected"})


def finished_chart_names(names: list[str]) -> set[str]:
    """Names whose ``chart_list.status`` is finished (see FINISHED_CHART_STATUSES).

    Best-effort like the sample check: an unreachable DB means nothing is
    treated as finished, so the charts run rather than being silently dropped.
    """
    if not names:
        return set()
    try:
        from db import connect, is_skip_db_write
    except Exception:
        return set()
    if is_skip_db_write():
        return set()
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT chart_name FROM chart_list "
                "WHERE chart_name = ANY(%s) AND status = ANY(%s)",
                (list(names), sorted(FINISHED_CHART_STATUSES)),
            ).fetchall()
    except Exception:
        logger.warning(
            "skip_completed: status lookup failed — running every chart", exc_info=True
        )
        return set()
    return {str(r["chart_name"]) for r in rows}


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
    from db import connect, get_chart_by_name, set_chart_output_path
    from db.path_ids import resolve_output_path
    from jobs.export_chart import write_chart

    write = blob_write_path or local_write_path
    if write:
        out_path = resolve_output_path(chart_name, write_path=write)
        if out_path:
            try:
                with connect() as conn:
                    row = get_chart_by_name(conn, chart_name)
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
    skip_db_write: bool = False,
    skip_completed: bool = False,
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
            f", large>{LARGE_CHART_MIN_PAGES}, one at a time" if is_large else "",
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
            # skip_completed: a chart that finished after the batch was listed
            # (the listing filter already dropped the rest). Deliberately NOT
            # tied to force — skip_ocr turns force off and must still re-run
            # finished charts. skip_db_write has no durable status: always run.
            if skip_completed and not skip_db_write:
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
                            "[skip %d/%d] %s already complete (skip_completed)",
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
                skip_db_write=skip_db_write,
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
    skip_db_write: bool = False,
    sample: Optional[int] = None,
    chart_names: Optional[list[str]] = None,
    skip_completed: bool = False,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    workers: Optional[int] = None,
) -> dict[str, Any]:
    """Scan a read path, register every chart, then run with a worker pool.

    Same vocabulary as /api/charts/run, minus the folder name: here every
    sub-folder IS a chart, so each supplies its own. A chart read from
    `<read_path>/52754737_48221214/` is written to
    `<write_path>/52754737_48221214/`.

    ``chart_names``, when set, restricts the batch to those folder names that
    actually exist under the read path (missing names are reported, not run).
    ``skip_completed`` drops charts whose chart_list.status is already finished
    (reported as ``charts_skipped_completed``).

    Each chart goes through exactly the same call ``/api/charts/run`` makes —
    ``ingest_and_run`` — so a batch of one is indistinguishable from a single
    run, and an option added to run works here without being plumbed twice.

    ``skip_db_write`` / test mode keeps charts in one in-memory store (local
    only; no Postgres) and writes each workspace under ``<chart>-test``.
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
    if skip_db_write and not local_read_path:
        raise ValueError("test_mode / skip_db_write requires local_read_path (no Postgres / blob)")

    from db import disable_skip_db_write, enable_skip_db_write
    from db.path_ids import resolve_run_batch

    if skip_db_write:
        enable_skip_db_write(reset=True)

    run_id, batch_id = resolve_run_batch(
        run_id, batch_id, blob_read_path, local_read_path
    )

    worker_count = resolve_batch_workers(workers)

    started = time.time()

    try:
        return _run_batch_inner(
            local_read_path=local_read_path,
            blob_container=blob_container,
            blob_read_path=blob_read_path,
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
            skip_db_write=skip_db_write,
            sample=sample,
            chart_names=chart_names,
            skip_completed=skip_completed,
            run_id=run_id,
            batch_id=batch_id,
            worker_count=worker_count,
            started=started,
        )
    finally:
        if skip_db_write:
            disable_skip_db_write()


def _run_batch_inner(
    *,
    local_read_path: Optional[str | Path],
    blob_container: Optional[str],
    blob_read_path: Optional[str],
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
    skip_db_write: bool,
    sample: Optional[int],
    chart_names: Optional[list[str]],
    skip_completed: bool,
    run_id: Optional[str],
    batch_id: Optional[str],
    worker_count: int,
    started: float,
) -> dict[str, Any]:
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

    missing_names: list[str] = []
    if chart_names:
        before = len(sources)
        sources, missing_names = filter_sources_by_chart_names(sources, chart_names)
        logger.info(
            "chart_names: kept %d of %d under %s (missing=%d)",
            len(sources),
            before,
            where,
            len(missing_names),
        )

    skipped_completed: list[str] = []
    if skip_completed and sources:
        finished = finished_chart_names([name for _src, name, _mode in sources])
        if finished:
            skipped_completed = [n for _s, n, _m in sources if n in finished]
            sources = [s for s in sources if s[1] not in finished]
            logger.info(
                "skip_completed: skipping %d already-finished chart(s) under %s",
                len(skipped_completed),
                where,
            )

    if skip_db_write:
        from db import test_chart_name

        sources = [(src, test_chart_name(name), mode) for src, name, mode in sources]

    if sample:
        before = len(sources)
        sources = select_batch_sources(sources, sample)
        if before > len(sources):
            logger.info(
                "sample=%d: queued %d of %d chart folder(s) "
                "(incomplete preferred when enough remain)",
                int(sample),
                len(sources),
                before,
            )
    total = len(sources)

    # Estimate pages so charts over LARGE_CHART_MIN_PAGES run one at a time.
    page_estimates: list[int] = [
        estimate_chart_pages(source, mode, blob_container=blob_container)
        for source, _name, mode in sources
    ]
    large_flags = [is_large_chart(n) for n in page_estimates]
    small_count = sum(1 for large in large_flags if not large)
    large_count = total - small_count
    ordered_jobs = submission_order(sources, page_estimates)

    logger.info(
        "Batch: %d chart folder(s) under %s (workers=%d, STAGE_WORKERS=%d, "
        "large>%d (one at a time): %d, small: %d)",
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
    large_limiter = LargeChartLimiter()

    if total == 0:
        summary = _summarise(results, started)
        summary["workers"] = worker_count
        summary["registered"] = registered
        summary["charts_missing"] = missing_names
        summary["charts_skipped_completed"] = skipped_completed
        return summary

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="batch") as pool:
        futures = []
        index_by_future: dict[Any, int] = {}
        for _orig_index, source, name, mode, is_large, _pages in ordered_jobs:
            # Submitted (and started) in alphabetical order; N/X is that position.
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
                skip_db_write=skip_db_write,
                skip_completed=skip_completed,
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
    summary["charts_missing"] = missing_names
    summary["charts_skipped_completed"] = skipped_completed
    logger.info(
        "Batch finished: %d/%d completed, %d failed, workers=%d, %.1fs",
        summary["completed"], summary["charts_found"],
        summary["failed"], worker_count, summary["duration_seconds"],
    )
    return summary
