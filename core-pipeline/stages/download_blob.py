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

from config import IMAGE_SUFFIXES, chart_dir, ensure_chart_dirs, pages_dir
from db import (
    connect,
    create_job,
    init_page_stages,
    count_manifest_members,
    reset_chart_results,
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
    # No manifest linking step: manifest_member_list.record_id IS the chart
    # name, so the relationship is a join, never a column to populate.
    manifest_rows = count_manifest_members(conn, chart_name)
    if manifest_rows:
        logger.info("chart %s: %s manifest row(s) match", chart_name, manifest_rows)
    progress = refresh_chart_status(conn, chart_id)
    return {"pages": pages, "manifest_rows": manifest_rows, "progress": progress}


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
    force: bool = False,
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
    """
    import shutil

    src = Path(source).expanduser().resolve()
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
    if existing and not force:
        raise RuntimeError(
            f"{dest_dir} already holds {len(existing)} file(s). "
            "Pass force=True to replace them."
        )

    # A re-import replaces the chart rather than merging into it. Without the
    # database reset, pages that disappeared from the source keep their old
    # page_list rows AND their completed page_stage_status rows, so the chart
    # reports finished while serving results for pages that no longer exist.
    # The chart_list row and its id survive; pipeline_jobs is left as the log.
    reset: dict[str, int] = {}
    if existing:
        with connect() as conn:
            prior = conn.execute(
                "SELECT id FROM chart_list WHERE chart_name = %s", (name,)
            ).fetchone()
            if prior:
                reset = reset_chart_results(conn, prior["id"])
                if reset:
                    logger.info(
                        "Re-import of %s: cleared %s",
                        name,
                        ", ".join(f"{v} {k}" for k, v in reset.items()),
                    )
    for stale in existing:
        stale.unlink()
    # Stale OCR text, imaging CSVs and corrected page images all describe the
    # old page set. A left-behind corrected-pages/1.jpg is the worst of the
    # three: page_image_path would prefer it, so the new scan would be OCR'd as
    # the old one, silently.
    for sub in ("ocr", "imaging", "corrected-pages"):
        folder = chart_dir(name) / sub
        if folder.is_dir():
            for old_file in folder.iterdir():
                if old_file.is_file():
                    old_file.unlink()

    copied: list[str] = []
    for index, path in enumerate(images, start=1):
        target = dest_dir / _normalize_page_filename(index, path.name)
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
    result["source"] = str(src)
    result["imported"] = len(copied)
    result["manifest"] = manifest_summary
    result["reset"] = reset
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
