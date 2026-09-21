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
import re
from pathlib import Path
from typing import Any, Optional

from config import IMAGE_SUFFIXES, ensure_chart_dirs, pages_dir
from db import (
    connect,
    create_job,
    init_page_stages,
    count_manifest_members,
    prune_orphan_pages,
    reset_chart_results,
    set_chart_output_path,
    set_chart_status,
    sha256_file,
    update_job,
    upsert_chart,
    upsert_pages,
)
from db.blob_store import (
    chart_name_from_blob_path,
    download_blob_to_path,
    ensure_blob_ready,
    list_image_blobs,
)
from db.chart_status import refresh_chart_status
from db.paths import clear_chart_workspace

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
    pruned = prune_orphan_pages(
        conn, chart_id, [p["page_name"] for p in page_rows]
    )
    if pruned:
        logger.info(
            "chart %s: pruned %d orphan page_list row(s) after re-ingest",
            chart_name, pruned,
        )
    init_page_stages(conn, chart_id)
    # No manifest linking step: manifest_member_list.record_id IS the chart
    # name, so the relationship is a join, never a column to populate.
    manifest_rows = count_manifest_members(conn, chart_name)
    if manifest_rows:
        logger.info("chart %s: %s manifest row(s) match", chart_name, manifest_rows)
    progress = refresh_chart_status(conn, chart_id)
    return {
        "pages": pages,
        "manifest_rows": manifest_rows,
        "progress": progress,
        "pruned_pages": pruned,
    }


def run_download(
    *,
    blob_container: str,
    blob_path: str,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    chart_id: Optional[int] = None,
    force: bool = True,
) -> dict[str, Any]:
    from db.path_ids import resolve_output_path, resolve_run_batch

    run_id, batch_id = resolve_run_batch(run_id, batch_id, blob_path, blob_container)
    chart_name = chart_name_from_blob_path(blob_path)
    ensure_chart_dirs(chart_name)
    out_path = resolve_output_path(chart_name, read_path=blob_path.strip("/"))

    with connect() as conn:
        chart = upsert_chart(
            conn,
            chart_name=chart_name,
            status="downloading",
            source="blob",
            blob_container=blob_container,
            blob_path=blob_path.strip("/"),
            output_path=out_path,
            run_id=run_id,
            batch_id=batch_id,
        )
        chart_id = chart["id"]
        job_id = create_job(
            conn, chart_id=chart_id, stage_name="download_blob", status="running"
        )
        update_job(conn, job_id, started=True)

    try:
        ensure_blob_ready(blob_container)
        blob_names = list_image_blobs(blob_container, blob_path)
        if not blob_names:
            # "No images" has three quite different causes and the bare message
            # distinguished none of them, so the next step was always guessing.
            # One extra listing call, only on the failure path, says which.
            from db.blob_store import get_container_client, normalize_prefix

            prefix = normalize_prefix(blob_path)
            try:
                client = get_container_client(blob_container)
                sample = [
                    b.name
                    for _, b in zip(
                        range(5), client.list_blobs(name_starts_with=prefix)
                    )
                ]
            except Exception:
                sample = []

            if not sample:
                detail = (
                    "nothing at all exists under that prefix — check the path, "
                    "its capitalisation (blob names are case-sensitive), and "
                    "that the container is right"
                )
            else:
                detail = (
                    "blobs exist there but none is a recognised image "
                    f"({', '.join(sorted(IMAGE_SUFFIXES))}). First few: "
                    + ", ".join(sample)
                )
            raise RuntimeError(
                f"No images found under {blob_container}/{prefix} — {detail}"
            )

        # Re-submit with force=True wipes local results so a new page set cannot
        # be served from stale files. force=False is resume: keep workspace +
        # stage results and only download missing page files.
        reset: dict[str, int] = {}
        cleared: dict[str, int] = {}
        dest_root = pages_dir(chart_name)
        if force:
            with connect() as conn:
                reset = reset_chart_results(conn, chart_id)
            cleared = clear_chart_workspace(chart_name)
            if reset or cleared:
                logger.info(
                    "Blob re-ingest %s: reset=%s cleared=%s",
                    chart_name, reset or "{}", cleared or "{}",
                )
        else:
            logger.info(
                "Blob resume %s: keeping existing workspace/results "
                "(force=false)",
                chart_name,
            )

        page_rows: list[dict[str, Any]] = []
        reused = 0
        total_files = len(blob_names)

        from db.paths import write_folder_progress

        for idx, blob_name in enumerate(blob_names, start=1):
            local_name = _normalize_page_filename(idx, Path(blob_name).name)
            dest = dest_root / local_name
            write_folder_progress(
                chart_name, idx, total_files, detail=f"download {local_name}"
            )
            if not force and dest.is_file() and dest.stat().st_size > 0:
                reused += 1
            else:
                logger.info(
                    "Downloading %d/%d %s -> %s", idx, total_files, blob_name, dest
                )
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
                output_path=out_path,
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


_SAFE_CHART_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _natural_key(path: Path) -> tuple:
    """Sort page2.jpg before page10.jpg, which a plain string sort would not."""
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", path.name)
    )


def _collect_images(source: Path, *, recursive: bool) -> list[Path]:
    walker = source.rglob("*") if recursive else source.iterdir()
    files = [
        p for p in walker
        if p.is_file()
        and p.suffix.lower() in IMAGE_SUFFIXES
        and not p.name.startswith("._")          # macOS AppleDouble stubs
    ]
    return sorted(files, key=_natural_key)


def _collect_manifests(source: Path, *, recursive: bool) -> list[Path]:
    """CSV/XLSX sitting alongside the images — the member roster for this drop."""
    from jobs.manifest_sweeper import MANIFEST_SUFFIXES

    walker = source.rglob("*") if recursive else source.iterdir()
    return sorted(
        p for p in walker
        if p.is_file()
        and p.suffix.casefold() in MANIFEST_SUFFIXES
        and not p.name.startswith("._")
    )


def import_local_folder(
    source: str | Path,
    *,
    chart_name: Optional[str] = None,
    force: bool = True,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> dict[str, Any]:
    """Copy a folder of images into the chart workspace and register it.

    ``source`` is any directory containing page images — it does NOT have to be
    under DATA_ROOT, which is what ``register_local_pages`` requires. Images are
    renamed to ``1.jpg``, ``2.jpg`` … in natural-sort order, matching what the
    blob intake produces, so every later stage sees the same shape either way.

    The source is always **copied**, never moved: a failed import is then a
    no-op rather than data loss. Subfolders are always searched, because a
    chart folder that keeps its scans in ``pages/`` is the common shape, not a
    special case. Any manifest alongside the images is always loaded — the
    member stage cannot run without one.

    Re-submit **overwrites by default** (``force=True``): the local chart
    workspace is cleared and stage result tables are wiped, while ``chart_list``
    and ``page_list`` rows are kept (page ids stay stable via upsert).

    ``force=False`` is **resume**: if ``pages/`` already has files, they are
    kept (no wipe, no re-copy) and the chart is only re-registered so the
    pipeline can continue incomplete pages.
    """
    import shutil

    from db.path_ids import resolve_output_path, resolve_run_batch

    src = Path(source).expanduser().resolve()
    run_id, batch_id = resolve_run_batch(run_id, batch_id, str(src))
    if not src.is_dir():
        raise RuntimeError(f"Not a directory: {src}")

    images = _collect_images(src, recursive=True)
    if not images:
        raise RuntimeError(
            f"No images in {src} or its subfolders "
            f"(looked for {', '.join(sorted(IMAGE_SUFFIXES))})"
        )

    name = (chart_name or src.name).strip()
    name = _SAFE_CHART_NAME.sub("_", name).strip("._-")
    if not name:
        raise RuntimeError(f"Cannot derive a chart name from {src}")

    dest_dir = pages_dir(name)
    if dest_dir.resolve() == src:
        raise RuntimeError(
            f"{src} is already the chart workspace for '{name}' — "
            f"use register_local_pages('{name}') instead"
        )

    ensure_chart_dirs(name)
    existing = [p for p in dest_dir.iterdir() if p.is_file()] if dest_dir.is_dir() else []

    # force=False + existing workspace = resume: do not wipe OCR/results or
    # re-copy pages. force=True (default) clears and re-imports.
    if existing and not force:
        logger.info(
            "Resume import %s: keeping %d existing page file(s) under %s",
            name,
            len(existing),
            dest_dir,
        )
        result = register_local_pages(name, run_id=run_id, batch_id=batch_id)
        out_path = resolve_output_path(name, read_path=str(src))
        if out_path and result.get("chart_id") is not None:
            with connect() as conn:
                set_chart_output_path(conn, int(result["chart_id"]), out_path)
            result["output_path"] = out_path
        result["source"] = str(src)
        result["imported"] = 0
        result["resumed"] = True
        result["manifest"] = {"files": 0, "inserted": 0, "updated": 0}
        result["reset"] = {}
        result["cleared_workspace"] = {}
        return result

    # Re-import replaces disk outputs and stage results; chart_list + page_list
    # survive so ids stay stable. pipeline_jobs is left as the audit log.
    reset: dict[str, int] = {}
    cleared: dict[str, int] = {}
    if force:
        with connect() as conn:
            prior = conn.execute(
                "SELECT id FROM chart_list WHERE chart_name = %s", (name,)
            ).fetchone()
            if prior:
                reset = reset_chart_results(conn, prior["id"])
                if reset:
                    logger.info(
                        "Re-import of %s: cleared DB results %s",
                        name,
                        ", ".join(f"{v} {k}" for k, v in reset.items()),
                    )
        cleared = clear_chart_workspace(name)
        if cleared:
            logger.info(
                "Re-import of %s: cleared local workspace %s",
                name,
                ", ".join(f"{v} {k}" for k, v in cleared.items()),
            )

    from db.paths import write_folder_progress

    copied: list[str] = []
    total_files = len(images)
    for index, path in enumerate(images, start=1):
        target = dest_dir / _normalize_page_filename(index, path.name)
        write_folder_progress(
            name, index, total_files, detail=f"import {target.name}"
        )
        shutil.copy2(path, target)
        copied.append(target.name)

    logger.info("Copied %d image(s) from %s -> %s", len(copied), src, dest_dir)

    # Any manifest dropped in alongside the images is loaded here. Order no
    # longer matters — manifest rows are keyed on record_id, which IS the chart
    # name, so there is no link step that could run too early or too late.
    manifest_summary: dict[str, Any] = {"files": 0, "inserted": 0, "updated": 0}
    manifests = _collect_manifests(src, recursive=True)
    if manifests:
        from config import METADATA_ROOT
        from jobs.manifest_sweeper import run_load

        METADATA_ROOT.mkdir(parents=True, exist_ok=True)
        for man in manifests:
            target = METADATA_ROOT / man.name
            shutil.copy2(man, target)
            loaded = run_load(local_path=target, run_id=run_id, batch_id=batch_id)
            # run_load flattens its counters at the top level, not under
            # a "totals" key.
            manifest_summary["files"] += loaded["files"]
            manifest_summary["inserted"] += loaded["inserted"]
            manifest_summary["updated"] += loaded["updated"]
        logger.info(
            "Loaded %d manifest file(s): +%d inserted, ~%d updated",
            manifest_summary["files"],
            manifest_summary["inserted"],
            manifest_summary["updated"],
        )

    result = register_local_pages(name, run_id=run_id, batch_id=batch_id)
    out_path = resolve_output_path(name, read_path=str(src))
    if out_path and result.get("chart_id") is not None:
        with connect() as conn:
            set_chart_output_path(conn, int(result["chart_id"]), out_path)
        result["output_path"] = out_path
    result["source"] = str(src)
    result["imported"] = len(copied)
    result["manifest"] = manifest_summary
    result["reset"] = reset
    result["cleared_workspace"] = cleared
    return result


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
