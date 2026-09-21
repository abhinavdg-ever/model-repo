"""Stage: intake — register a chart and pull its page images to local disk.

The chart folder on blob holds one image per page. This stage:

  1. upserts ``chart_list`` (atomic on UNIQUE (chart_name)),
  2. ensures ``pages/`` (+ sparse ``corrected-pages/``) under the workspace —
     prefer files already in ``review-ui/data/folders/<chart>/``, else hydrate
     from ``chart_list.output_path`` (Processed), else download missing pages
     from Raw_Input ``blob_path``,
  3. upserts ``page_list`` with a SHA-256 and size per page,
  4. seeds ``page_stage_status`` so every later stage has a pending row,
  5. links any manifest rows swept before this chart existed.

By default existing workspace pages are **not** re-downloaded. ``force`` only
resets stage result tables; pass ``redownload_pages=True`` to wipe and re-fetch
``pages/`` + ``corrected-pages/``.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from config import IMAGE_SUFFIXES, corrected_pages_dir, ensure_chart_dirs, pages_dir
from db import (
    connect,
    create_job,
    get_chart,
    get_chart_by_name,
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
from db.paths import clear_page_image_dirs, list_local_pages

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


def _page_rows_from_workspace(chart_name: str) -> list[dict[str, Any]]:
    files = list_local_pages(chart_name)
    return [
        {
            "page_name": path.name,
            "page_number": index,
            "image_sha256": sha256_file(path),
            "file_size_bytes": path.stat().st_size,
        }
        for index, path in enumerate(files, start=1)
    ]


def _local_output_subdir_candidates(
    output_path: str, chart_name: str, subdir: str
) -> list[Path]:
    """Possible on-disk directories for ``pages/`` or ``corrected-pages/`` under output."""
    raw = (output_path or "").strip().rstrip("/\\")
    if not raw:
        return []
    base = Path(raw)
    candidates = [base / subdir, base]
    if base.name != chart_name:
        candidates.insert(0, base / chart_name / subdir)
        candidates.insert(1, base / chart_name)
    seen: set[str] = set()
    out: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append(path)
    return out


def _iter_image_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    files = [
        p
        for p in folder.iterdir()
        if p.is_file()
        and p.suffix.lower() in IMAGE_SUFFIXES
        and not p.name.startswith("._")
    ]
    return sorted(files, key=lambda p: p.name.casefold())


def _copy_images_into(
    src: Path, dest: Path, *, only_missing: bool = True
) -> int:
    """Copy image files from ``src`` into ``dest``. Returns files written/kept."""
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src_file in _iter_image_files(src):
        target = dest / src_file.name
        if only_missing and target.is_file() and target.stat().st_size > 0:
            copied += 1
            continue
        if src_file.resolve() == target.resolve():
            copied += 1
            continue
        shutil.copy2(src_file, target)
        copied += 1
    return copied


def _hydrate_subdir_from_local_output(
    *,
    chart_name: str,
    output_path: str,
    subdir: str,
    dest: Path,
    only_missing: bool = True,
) -> int:
    for candidate in _local_output_subdir_candidates(output_path, chart_name, subdir):
        # Candidate may be the subdir itself or the chart root (copy from …/subdir).
        src = candidate if candidate.name == subdir else candidate / subdir
        if not src.is_dir():
            # Flat folder of images under output_path — only for pages/.
            if (
                subdir == "pages"
                and candidate.is_dir()
                and _iter_image_files(candidate)
            ):
                src = candidate
            else:
                continue
        n = _copy_images_into(src, dest, only_missing=only_missing)
        if n:
            logger.info(
                "Hydrated %d file(s) into %s/%s from local output %s",
                n,
                chart_name,
                subdir,
                src,
            )
            return n
    return 0


def _pull_subdir_from_blob_output(
    *,
    container: str,
    output_path: str,
    chart_name: str,
    subdir: str,
    dest: Path,
    only_missing: bool = True,
) -> int:
    prefix = output_path.strip().strip("/").replace("\\", "/")
    prefixes = [f"{prefix}/{subdir}", prefix]
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    for pref in prefixes:
        try:
            names = list_image_blobs(container, pref)
        except Exception as exc:
            logger.warning(
                "Could not list %s images under %s/%s (%s)",
                subdir,
                container,
                pref,
                exc,
            )
            continue
        filtered: list[str] = []
        for blob_name in names:
            parts = [p for p in blob_name.replace("\\", "/").split("/") if p]
            if not parts:
                continue
            if pref.rstrip("/").endswith(subdir) or (
                len(parts) >= 2 and parts[-2] == subdir
            ):
                filtered.append(blob_name)
            elif pref == prefix and len(parts) == 1 and subdir == "pages":
                filtered.append(blob_name)
        for blob_name in filtered:
            filename = Path(blob_name).name
            if filename.startswith("._"):
                continue
            target = dest / filename
            if only_missing and target.is_file() and target.stat().st_size > 0:
                copied += 1
                continue
            try:
                download_blob_to_path(container, blob_name, target)
                copied += 1
            except Exception as exc:
                logger.warning("Failed to download %s (%s)", blob_name, exc)
        if copied:
            logger.info(
                "Pulled %d %s file(s) from blob %s/%s → workspace",
                copied,
                subdir,
                container,
                pref,
            )
            break
    return copied


def _hydrate_corrected_pages(
    *,
    chart_name: str,
    output_path: Optional[str],
    blob_container: Optional[str],
) -> int:
    """Fill gaps in corrected-pages/ from output_path (local then blob)."""
    out = (output_path or "").strip()
    if not out:
        return 0
    dest = corrected_pages_dir(chart_name)
    n = _hydrate_subdir_from_local_output(
        chart_name=chart_name,
        output_path=out,
        subdir="corrected-pages",
        dest=dest,
        only_missing=True,
    )
    if n:
        return n
    container = (blob_container or "").strip()
    if not container:
        return 0
    try:
        ensure_blob_ready(container)
    except Exception as exc:
        logger.warning("Blob not ready for corrected-pages hydrate: %s", exc)
        return 0
    return _pull_subdir_from_blob_output(
        container=container,
        output_path=out,
        chart_name=chart_name,
        subdir="corrected-pages",
        dest=dest,
        only_missing=True,
    )


def _hydrate_pages_from_output(
    *,
    chart_name: str,
    output_path: Optional[str],
    blob_container: Optional[str],
) -> int:
    out = (output_path or "").strip()
    if not out:
        return 0
    dest = pages_dir(chart_name)
    n = _hydrate_subdir_from_local_output(
        chart_name=chart_name,
        output_path=out,
        subdir="pages",
        dest=dest,
        only_missing=True,
    )
    if list_local_pages(chart_name):
        return max(n, len(list_local_pages(chart_name)))
    container = (blob_container or "").strip()
    if not container:
        return 0
    try:
        ensure_blob_ready(container)
    except Exception as exc:
        logger.warning("Blob not ready for pages hydrate: %s", exc)
        return 0
    return _pull_subdir_from_blob_output(
        container=container,
        output_path=out,
        chart_name=chart_name,
        subdir="pages",
        dest=dest,
        only_missing=True,
    )


def _download_missing_from_raw_input(
    *,
    chart_name: str,
    blob_container: str,
    blob_path: str,
) -> tuple[int, int]:
    """Download Raw_Input images into pages/, skipping files already present.

    Returns ``(total_registered, reused_count)``.
    """
    ensure_blob_ready(blob_container)
    blob_names = list_image_blobs(blob_container, blob_path)
    if not blob_names:
        from db.blob_store import get_container_client, normalize_prefix

        prefix = normalize_prefix(blob_path)
        try:
            client = get_container_client(blob_container)
            sample = [
                b.name
                for _, b in zip(range(5), client.list_blobs(name_starts_with=prefix))
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

    dest_root = pages_dir(chart_name)
    dest_root.mkdir(parents=True, exist_ok=True)
    reused = 0
    total = len(blob_names)
    from db.paths import write_folder_progress

    for idx, blob_name in enumerate(blob_names, start=1):
        local_name = _normalize_page_filename(idx, Path(blob_name).name)
        dest = dest_root / local_name
        write_folder_progress(
            chart_name, idx, total, detail=f"download {local_name}"
        )
        if dest.is_file() and dest.stat().st_size > 0:
            reused += 1
            continue
        logger.info(
            "Downloading %d/%d %s -> %s", idx, total, blob_name, dest
        )
        download_blob_to_path(blob_container, blob_name, dest)
    return total, reused


def ensure_chart_images(
    chart_name: str,
    *,
    chart_id: Optional[int] = None,
    blob_container: Optional[str] = None,
    blob_path: Optional[str] = None,
    output_path: Optional[str] = None,
    force_redownload_pages: bool = False,
    register: bool = True,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    source: str = "blob",
) -> dict[str, Any]:
    """Ensure workspace ``pages/`` for this chart.

    Prefer ``data/folders/<chart>/pages/``. If missing, download from Raw_Input
    (``blob_path``). Corrected images are **not** hydrated here — quality/
    rotation recreates ``corrected-pages/``. ``output_path`` is unused for pages
    (Processed usually omits originals under ``skip_orig_pages``).
    """
    ensure_chart_dirs(chart_name)
    cleared: dict[str, int] = {}
    if force_redownload_pages:
        cleared = clear_page_image_dirs(chart_name)
        if cleared:
            logger.info(
                "Redownload pages for %s: cleared %s",
                chart_name,
                cleared,
            )

    tried: list[str] = []
    source_label = "workspace"
    reused = 0
    downloaded = 0

    local_pages = list_local_pages(chart_name)
    if local_pages:
        tried.append("workspace")
        source_label = "workspace"
    else:
        tried.append("workspace(empty)")
        container = (blob_container or "").strip()
        raw = (blob_path or "").strip()
        if container and raw:
            tried.append(f"raw_input:{container}/{raw}")
            total, reused = _download_missing_from_raw_input(
                chart_name=chart_name,
                blob_container=container,
                blob_path=raw,
            )
            downloaded = total - reused
            source_label = f"raw_input:{container}/{raw}"
        else:
            tried.append("raw_input(unavailable)")

    local_pages = list_local_pages(chart_name)
    if not local_pages:
        raise RuntimeError(
            f"No page images for chart '{chart_name}' — tried: "
            + ", ".join(tried)
            + ". Put files under data/folders/<chart>/pages/ or provide "
            "blob_container + blob_path (Raw_Input)."
        )

    page_rows = _page_rows_from_workspace(chart_name)
    result: dict[str, Any] = {
        "chart_name": chart_name,
        "page_count": len(page_rows),
        "pages_reused": (
            reused if source_label.startswith("raw_input") else len(page_rows)
        ),
        "pages_downloaded": (
            downloaded if source_label.startswith("raw_input") else 0
        ),
        "image_source": source_label,
        "tried": tried,
        "cleared_pages": cleared,
    }

    if not register:
        result["page_rows"] = page_rows
        return result

    blob_path_norm = (blob_path or "").strip().strip("/") or None
    with connect() as conn:
        chart = upsert_chart(
            conn,
            chart_name=chart_name,
            page_count=len(page_rows),
            status="processing",
            source=source,
            blob_container=blob_container,
            blob_path=blob_path_norm,
            output_path=output_path,
            run_id=run_id,
            batch_id=batch_id,
        )
        resolved_id = int(chart["id"] if chart_id is None else chart_id)
        tail = _finish_registration(conn, resolved_id, chart_name, page_rows)

    result["chart_id"] = resolved_id
    result.update(tail)
    logger.info(
        "chart %s: %d page(s) ready via %s",
        chart_name,
        len(page_rows),
        source_label,
    )
    return result


def run_download(
    *,
    blob_container: str,
    blob_path: str,
    run_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    chart_id: Optional[int] = None,
    force: bool = True,
    redownload_pages: bool = False,
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
        # force resets stage DB rows so the chain reprocesses; it does NOT wipe
        # pages/corrected-pages. Use redownload_pages for a full image re-fetch.
        reset: dict[str, int] = {}
        if force:
            with connect() as conn:
                reset = reset_chart_results(conn, chart_id)
            if reset:
                logger.info(
                    "Blob re-ingest %s: reset stage results %s (pages kept)",
                    chart_name,
                    reset,
                )
        else:
            logger.info(
                "Blob resume %s: keeping existing workspace/results "
                "(force=false)",
                chart_name,
            )

        ensured = ensure_chart_images(
            chart_name,
            chart_id=chart_id,
            blob_container=blob_container,
            blob_path=blob_path.strip("/"),
            output_path=out_path,
            force_redownload_pages=redownload_pages,
            register=True,
            run_id=run_id,
            batch_id=batch_id,
            source="blob",
        )

        with connect() as conn:
            update_job(
                conn,
                job_id,
                status="completed",
                completed=True,
                pages_total=ensured["page_count"],
                pages_done=ensured["page_count"],
            )

        return {
            "chart_id": chart_id,
            "chart_name": chart_name,
            "page_count": ensured["page_count"],
            "pages_reused": ensured.get("pages_reused", 0),
            "pages_downloaded": ensured.get("pages_downloaded", 0),
            "image_source": ensured.get("image_source"),
            "job_id": job_id,
            "reset": reset,
            "cleared_pages": ensured.get("cleared_pages") or {},
            "pages": ensured.get("pages"),
            "manifest_rows": ensured.get("manifest_rows"),
            "progress": ensured.get("progress"),
            "pruned_pages": ensured.get("pruned_pages"),
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
    redownload_pages: bool = False,
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

    Existing workspace ``pages/`` are kept by default (even when ``force=True``,
    which only resets stage result tables). Pass ``redownload_pages=True`` to
    wipe ``pages/`` + ``corrected-pages/`` and re-copy from ``source``.
    """
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
    existing = list_local_pages(name)

    reset: dict[str, int] = {}
    cleared: dict[str, int] = {}
    if force:
        with connect() as conn:
            prior = get_chart_by_name(conn, name)
            if prior:
                reset = reset_chart_results(conn, prior["id"])
                if reset:
                    logger.info(
                        "Re-import of %s: cleared DB results %s (pages kept)",
                        name,
                        ", ".join(f"{v} {k}" for k, v in reset.items()),
                    )

    if redownload_pages:
        cleared = clear_page_image_dirs(name)
        if cleared:
            logger.info(
                "Re-import of %s: cleared page images %s",
                name,
                ", ".join(f"{v} {k}" for k, v in cleared.items()),
            )
        existing = []

    # Prefer existing workspace pages — do not re-copy from source.
    if existing:
        logger.info(
            "Import %s: keeping %d existing page file(s) under %s",
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
        result["reset"] = reset
        result["cleared_workspace"] = cleared
        return result

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
        chart = get_chart(conn, chart_id)

    return {
        "chart_id": chart_id,
        "chart_name": chart_name,
        "page_count": len(page_rows),
        "status": chart["status"],
        **tail,
    }
