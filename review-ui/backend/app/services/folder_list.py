"""Paginated / filtered folder listing for the landing page.

Prefer ``chart_list`` summaries when ``DATABASE_URL`` is usable so Local Mode
does not walk every chart's pages/ocr/imaging tree on each request. Disk is
only used for charts missing from Postgres (light page count) or when no DB
is configured (full local scan as before).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, Sequence

# Every stage after Final OCR 2 is an imaging stage (the postgres adapter's set).
from app.adapters.postgres.repository import IMAGING_STAGES as _IMAGING_STAGES
from app.core.schemas import FolderSummary, OcrRunStatus
from app.services.chart_run_batch import (
    db_lookup_enabled,
    database_url_usable,
    with_run_batch_default,
)
from app.services.chart_metadata import chart_metadata_map

# The metadata filters' option for a chart with no value.
BLANK_OPTION = "(Blank)"
from app.services.chart_review import reviewed_charts
from app.services.ground_truth import charts_with_ground_truth
from app.services.db import connection


logger = logging.getLogger("review_ui.folder_list")

SortKey = Literal["filename", "pages", "updated"]
SortDir = Literal["asc", "desc"]

# Every image type core-pipeline downloads (config.IMAGE_SUFFIXES).
IMAGE_RE = re.compile(
    r"\.(jpe?g|jpe|jfif|png|webp|gif|bmp|dib|tiff?|jp2|j2k|pbm|pgm|ppm|pnm)$", re.IGNORECASE
)

# chart_list.status (+ current_stage) → landing pill. Mirrors postgres adapter.
_CHART_STATUS_TO_OCR: dict[str, str] = {
    "received": "QUEUED",
    "downloading": "IN_PROGRESS",
    "processing": "IN_PROGRESS",
    "completed": "IMAGING_COMPLETED",
    "failed": "FAILED",
    "needs_review": "IMAGING_COMPLETED",
    "rejected": "IMAGING_COMPLETED",
    "ocr_prelim": "IN_PROGRESS",
    "ocr_quality": "IN_PROGRESS",
    "blank_junk": "IMAGING_IN_PROGRESS",
    "ocr_final1": "IN_PROGRESS",
    "ocr_final2": "IN_PROGRESS",
    "section_headers": "IMAGING_IN_PROGRESS",
    "kv_extract": "IMAGING_IN_PROGRESS",
    "member_verify": "IMAGING_IN_PROGRESS",
    "dos_extract": "IMAGING_IN_PROGRESS",
    "page_subtype": "IMAGING_IN_PROGRESS",
    "encounter_type": "IMAGING_IN_PROGRESS",
    "page_sequencing": "IMAGING_IN_PROGRESS",
}


def ocr_status_for(status: str | None, current_stage: str | None) -> OcrRunStatus:
    key = str(status or "").lower()
    mapped = _CHART_STATUS_TO_OCR.get(key, "QUEUED")
    if key == "processing" and str(current_stage or "").lower() in _IMAGING_STAGES:
        return "IMAGING_IN_PROGRESS"  # type: ignore[return-value]
    return mapped  # type: ignore[return-value]


@dataclass
class FolderListParams:
    q: str = ""
    status: list[str] = field(default_factory=list)
    run: list[str] = field(default_factory=list)
    batch: list[str] = field(default_factory=list)
    source: list[str] = field(default_factory=list)
    measure: list[str] = field(default_factory=list)
    retrieval: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    # Only charts last run at or after this moment (None = any time).
    run_after: Optional[datetime] = None
    sort: SortKey = "updated"
    sort_dir: SortDir = "desc"
    limit: Optional[int] = None  # None / <=0 → return all matching
    offset: int = 0


@dataclass
class FolderListResult:
    items: list[FolderSummary]
    total: int
    page_count_sum: int
    ocr_processed_sum: int
    # Of the filtered charts, the Imaging Completed ones: how many, their pages, and how
    # many of those have ground truth (the landing summary).
    imaging_completed_charts: int
    imaging_completed_pages: int
    ground_truth_charts: int
    run_options: list[str]
    batch_options: list[str]
    source_options: list[str]
    scope_options: list[str]
    measure_options: list[str]
    retrieval_options: list[str]
    limit: Optional[int]
    offset: int


def quick_page_count(folder_dir: Path) -> int:
    """Count image files under pages/ (or folder root) without OCR/imaging I/O."""
    pages = folder_dir / "pages"
    root = pages if pages.is_dir() else folder_dir
    if not root.is_dir():
        return 0
    n = 0
    try:
        for p in root.iterdir():
            if p.is_file() and IMAGE_RE.search(p.name) and not p.name.startswith("."):
                n += 1
    except OSError:
        return 0
    return n


def fetch_chart_summaries_from_db(
    database_url: str,
    *,
    db_schema: str = "public",
) -> list[FolderSummary]:
    """Landing rows from ``chart_list`` only (no disk walk).

    Includes ``source='local'`` charts. Empty manifest rows stay out.
    """
    if not db_lookup_enabled(database_url, db_schema):
        return []
    try:
        with connection(database_url, db_schema) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT chart_name, page_count, status, updated_at,
                           current_stage, run_id, batch_id
                      FROM chart_list
                     WHERE source <> 'manifest' OR page_count > 0
                     ORDER BY updated_at DESC NULLS LAST, id DESC
                    """
                )
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("chart_list summary lookup failed: %s", exc)
        return []

    out: list[FolderSummary] = []
    for chart_name, page_count, status, updated_at, current_stage, run_id, batch_id in rows:
        name = str(chart_name or "").strip()
        if not name:
            continue
        page_n = int(page_count or 0)
        status_ui = ocr_status_for(status, current_stage)
        # Approximate progress columns from chart status (no per-page disk scan).
        imaging_n = page_n if status_ui == "IMAGING_COMPLETED" else 0
        ocr_n = (
            page_n
            if status_ui
            in {"COMPLETED", "IMAGING_COMPLETED", "IMAGING_IN_PROGRESS", "IN_PROGRESS"}
            else 0
        )
        stored_run, stored_batch = with_run_batch_default(
            str(run_id) if run_id else None,
            str(batch_id) if batch_id else None,
        )
        out.append(
            FolderSummary(
                id=name,
                name=name,
                page_count=page_n,
                ocr_processed=ocr_n,
                imaging_processed=imaging_n,
                ocr_status=status_ui,
                last_updated_at=updated_at,
                run_id=stored_run,
                batch_id=stored_batch,
            )
        )
    return out


def quick_disk_status(folder_dir: Path) -> str:
    """A workspace's status from what is on disk, without reading it: the final CSV means
    imaging finished; other imaging CSVs, imaging in progress; OCR files, OCR done.
    (A chart with no chart_list row -- a test-mode run -- has no other status, and
    QUEUED charts are hidden from the list.)"""
    imaging = folder_dir / "imaging"
    try:
        if (imaging / f"{folder_dir.name}_final.csv").is_file():
            return "IMAGING_COMPLETED"
        if imaging.is_dir() and any(p.suffix == ".csv" for p in imaging.iterdir()):
            return "IMAGING_IN_PROGRESS"
        ocr = folder_dir / "ocr"
        if ocr.is_dir() and any(not p.name.startswith(".") for p in ocr.iterdir()):
            return "COMPLETED"
    except OSError:
        pass
    return "QUEUED"


def list_disk_folder_names_light(data_root: Path) -> list[FolderSummary]:
    """Chart dirs on disk with a cheap page count — no OCR/imaging scan."""
    if not data_root.is_dir():
        return []
    out: list[FolderSummary] = []
    try:
        entries = sorted(data_root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        mtime = None
        try:
            mtime = datetime.fromtimestamp(entry.stat().st_mtime).astimezone()
        except OSError:
            pass
        disk_run, disk_batch = with_run_batch_default(None, None)
        out.append(
            FolderSummary(
                id=entry.name,
                name=entry.name,
                page_count=quick_page_count(entry),
                ocr_processed=0,
                imaging_processed=0,
                ocr_status=quick_disk_status(entry),  # type: ignore[arg-type]
                last_updated_at=mtime,
                run_id=disk_run,
                batch_id=disk_batch,
                on_disk=True,
            )
        )
    return out


def merge_db_and_disk(
    db_rows: Sequence[FolderSummary],
    disk_rows: Sequence[FolderSummary],
) -> list[FolderSummary]:
    """DB wins for known charts; disk-only charts appended."""
    by_id: dict[str, FolderSummary] = {f.id: f for f in db_rows}
    for d in disk_rows:
        if d.id in by_id:
            existing = by_id[d.id]
            if existing.page_count <= 0 and d.page_count > 0:
                existing.page_count = d.page_count
            if existing.last_updated_at is None and d.last_updated_at is not None:
                existing.last_updated_at = d.last_updated_at
            continue
        by_id[d.id] = d
    return list(by_id.values())


def _sort_key_value(f: FolderSummary, sort: SortKey):
    if sort == "filename":
        return (f.name or "").casefold()
    if sort == "pages":
        return f.page_count or 0
    # updated. All aware, so a chart with no timestamp (oldest) sorts beside one with an offset.
    ts = f.last_updated_at
    if ts is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return _aware(ts)


def _aware(ts: datetime) -> datetime:
    """A timestamp comparable with any other: a naive one (file mtime) is local time."""
    return ts if ts.tzinfo is not None else ts.astimezone()


def filter_sort_page(
    rows: Sequence[FolderSummary],
    params: FolderListParams,
) -> FolderListResult:
    """Apply q/status/run/batch/run-after filters, sort, then paginate.

    Run facets come from the full set. Batch facets are empty until a run is
    selected, then only batches that appear on charts in those run(s).
    """
    # Review UI never lists Queued charts — drop them before facets/filters.
    active = [f for f in rows if f.ocr_status != "QUEUED"]

    run_options = sorted(
        {f.run_id for f in active if f.run_id},
        key=lambda s: s.casefold(),
    )

    q = (params.q or "").strip().casefold()
    status_set = {s for s in params.status if s}
    run_set = {s for s in params.run if s}
    batch_set = {s for s in params.batch if s}
    # Scope / Source / Measure / Retrieval: options from every listed chart, not locked to
    # another filter.
    metadata_filters = [
        ("project_scope", {s for s in params.scope if s}),
        ("source_name", {s for s in params.source if s}),
        ("measure", {s for s in params.measure if s}),
        ("rr_method", {s for s in params.retrieval if s}),
    ]
    # A chart with no value (none in the database or the CSVs) is offered as "(Blank)".
    metadata_options = {
        attr: sorted({getattr(f, attr) for f in active if getattr(f, attr)}, key=lambda s: s.casefold())
        + ([BLANK_OPTION] if any(not getattr(f, attr) for f in active) else [])
        for attr, _ in metadata_filters
    }
    run_after = _aware(params.run_after) if params.run_after else None

    # Batch options are locked to selected run(s).
    if run_set:
        batch_options = sorted(
            {
                f.batch_id
                for f in active
                if f.batch_id and f.run_id in run_set
            },
            key=lambda s: s.casefold(),
        )
    else:
        batch_options = []

    filtered: list[FolderSummary] = []
    for f in active:
        if q and q not in (f.name or "").casefold():
            continue
        if status_set and f.ocr_status not in status_set:
            continue
        if run_set and (f.run_id is None or f.run_id not in run_set):
            continue
        if batch_set and (f.batch_id is None or f.batch_id not in batch_set):
            continue
        if run_after and (f.last_updated_at is None or _aware(f.last_updated_at) < run_after):
            continue
        if any(wanted and (getattr(f, attr) or BLANK_OPTION) not in wanted for attr, wanted in metadata_filters):
            continue
        filtered.append(f)

    reverse = params.sort_dir == "desc"
    filtered.sort(
        key=lambda f: _sort_key_value(f, params.sort),
        reverse=reverse,
    )

    total = len(filtered)
    page_count_sum = sum(f.page_count or 0 for f in filtered)
    ocr_processed_sum = sum(f.ocr_processed or 0 for f in filtered)
    done = [f for f in filtered if f.ocr_status == "IMAGING_COMPLETED"]

    offset = max(0, int(params.offset or 0))
    limit = params.limit
    if limit is None or int(limit) <= 0:
        items = filtered[offset:] if offset else filtered
        eff_limit: Optional[int] = None
    else:
        lim = int(limit)
        items = filtered[offset : offset + lim]
        eff_limit = lim

    return FolderListResult(
        items=items,
        total=total,
        page_count_sum=page_count_sum,
        ocr_processed_sum=ocr_processed_sum,
        imaging_completed_charts=len(done),
        imaging_completed_pages=sum(f.page_count or 0 for f in done),
        ground_truth_charts=sum(1 for f in done if f.ground_truth_available),
        run_options=run_options,
        batch_options=batch_options,
        scope_options=metadata_options["project_scope"],
        source_options=metadata_options["source_name"],
        measure_options=metadata_options["measure"],
        retrieval_options=metadata_options["rr_method"],
        limit=eff_limit,
        offset=offset,
    )


def build_folder_list(
    *,
    database_url: str | None,
    db_schema: str = "public",
    data_root: Path | None,
    full_local_rows: Sequence[FolderSummary] | None,
    params: FolderListParams,
    metadata_dir: Path | None = None,
) -> FolderListResult:
    """Compose landing list: DB summaries preferred; light disk or full local fallback."""
    usable = bool(database_url and database_url_usable(database_url))
    metadata = chart_metadata_map(
        database_url if usable else None, db_schema=db_schema, metadata_dir=metadata_dir
    )

    def with_metadata(rows: Sequence[FolderSummary]) -> list[FolderSummary]:
        return [
            row.model_copy(update=metadata[row.name]) if row.name in metadata else row
            for row in rows
        ]

    if usable:
        db_rows = fetch_chart_summaries_from_db(database_url, db_schema=db_schema)
        disk_rows: list[FolderSummary] = []
        if data_root is not None:
            disk_rows = list_disk_folder_names_light(data_root)
        merged = merge_db_and_disk(db_rows, disk_rows)
        have = charts_with_ground_truth(database_url, db_schema)
        try:
            reviewed = reviewed_charts(database_url, db_schema)
        except Exception as exc:
            logger.warning("chart_review lookup failed: %s", exc)
            reviewed = {}
        on_disk = {row.id for row in disk_rows}
        merged = [
            row.model_copy(update={
                "ground_truth_available": row.name in have,
                "on_disk": row.id in on_disk,
                **_review_fields(reviewed.get(row.name)),
            })
            for row in merged
        ]
        return filter_sort_page(with_metadata(merged), params)

    # No DB — use caller-provided full local scan (existing behaviour).
    rows = list(full_local_rows or [])
    if not rows and data_root is not None:
        rows = list_disk_folder_names_light(data_root)
    return filter_sort_page(with_metadata(rows), params)


def _review_fields(review) -> dict:
    """FolderSummary's reviewed / reviewed_by / reviewed_at from a ChartReview."""
    if review is None:
        return {}
    return {
        "reviewed": True,
        "reviewed_by": review.reviewedBy,
        "reviewed_by_id": review.reviewedById,
        "reviewed_at": review.reviewedAt,
    }
