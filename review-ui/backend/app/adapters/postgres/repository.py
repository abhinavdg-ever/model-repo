"""Production Mode repository — read pipeline outputs from Postgres.

Page images come from Azure Blob using ``chart_list.blob_container`` +
``blob_path`` (Raw_Input), with Processed ``output_path`` pages/corrected as
fallbacks. OCR text, manifest, quality, blank/junk, member, verification, and
DOS come from schema tables. ``DATA_ROOT`` is optional (used when a local
workspace copy exists).
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from fastapi import HTTPException

from app.adapters.base import FolderRepository
from app.adapters.local.repository import LocalFolderRepository, _fmt_dos_display, _normalize_kind
from app.core.schemas import (
    FolderDetail,
    FolderSummary,
    ImagingDocumentResponse,
    ImagingManifestDetails,
    ImagingPageResult,
    ImagingSectionsProcessed,
    ImagingVerificationDetails,
    OcrSectionHeader,
    OcrTextResponse,
    PageSummary,
)
from app.services import db
from app.services.chart_run_batch import with_run_batch_default
from app.services.db import psycopg_url as _psycopg_url
from app.services.ground_truth import attach_ground_truth
from app.services.imaging_overlays import (
    inherit_duplicate_blank_junk,
    compose_page_type,
    continuity_label,
    empty_imaging_pages,
    page_type_fields,
    signer_fields,
    subtype_confidence,
)

logger = logging.getLogger("review_ui.postgres")


@contextmanager
def _skip_on_error(name: str, folder_id: str) -> Iterator[None]:
    """One part of a chart's overlay: a failure while applying its rows skips that part
    with a warning; the parts after it still load."""
    try:
        yield
    except Exception as exc:
        logger.warning("imaging overlay %s skipped for %s: %s", name, folder_id, exc)


# The overlay's queries run at the same time, each on its own pooled connection (no
# savepoints, a failed query skips only its own part); at most this many at once, so a
# chart open leaves the pool room for the other requests it makes.
_OVERLAY_WORKERS = 4


# The imaging overlay's queries, one per section of _page_imaging_from_db (parameter: the
# chart name). _overlay_rows runs them in parallel; the sections apply their rows in order.
_OVERLAY_SQL: dict[str, str] = {
    "quality": """
                            SELECT p.page_name, q.printed_or_handwritten, q.hw_confidence,
                                   q.quality_score, q.quality_tag,
                                   q.orientation_angle, q.tilt_angle, q.mirrored,
                                   q.document_type, q.is_visible,
                                   q.handwritten_area_pct,
                                   q.quality_detail -> 'confidences'
                            FROM ocr_quality_results q
                            JOIN page_list p ON p.id = q.page_id
                            JOIN chart_list c ON c.id = q.chart_id
                            WHERE c.chart_name = %s
                            """,
    "blank_junk": """
                            SELECT p.page_name, b.blank_junk_flag, b.junk_subtype,
                                   b.confidence, orig.page_name,
                                   -- NULL until changes_2026-10-22.sql has replaced the view.
                                   to_jsonb(b) ->> 'duplicate_confidence',
                                   b.reason
                            FROM v_page_blank_junk_final b
                            JOIN page_list p ON p.id = b.page_id
                            JOIN chart_list c ON c.id = b.chart_id
                            LEFT JOIN page_list orig ON orig.id = b.duplicate_of_page_id
                            WHERE c.chart_name = %s
                            """,
    "member": """
                            SELECT p.page_name,
                                   m.extracted_name, m.extracted_dob, m.extracted_member_id,
                                   m.confidence, m.page_status,
                                   -- NULL until changes_2026-10-27.sql adds the column.
                                   to_jsonb(m) -> 'other_names'
                            FROM member_extraction_results m
                            JOIN chart_list c ON c.id = m.chart_id
                            JOIN page_list p ON p.id = m.page_id
                            WHERE c.chart_name = %s
                            """,
    "dos": """
                            SELECT p.page_name,
                                   d.date_of_service_from, d.date_of_service_to,
                                   d.date_of_service_from_doclevel, d.date_of_service_to_doclevel,
                                   d.confidence
                            FROM dos_extraction_results d
                            JOIN page_list p ON p.id = d.page_id
                            JOIN chart_list c ON c.id = d.chart_id
                            WHERE c.chart_name = %s
                            """,
    "page_classification": """
                            SELECT p.page_name, pc.page_subtype,
                                   pc.classification_category, pc.confidence,
                                   to_jsonb(pc) ->> 'page_type',
                                   to_jsonb(pc) -> 'axes' ->> 'specialty',
                                   to_jsonb(pc) -> 'axes' ->> 'specialty_confidence',
                                   to_jsonb(pc) -> 'axes'
                            FROM page_classification pc
                            JOIN page_list p ON p.id = pc.page_id
                            JOIN chart_list c ON c.id = pc.chart_id
                            WHERE c.chart_name = %s
                            """,
    "encounter_type": """
                            SELECT p.page_name, e.encounter_type, e.confidence
                            FROM encounter_type_results e
                            JOIN page_list p ON p.id = e.page_id
                            JOIN chart_list c ON c.id = e.chart_id
                            WHERE c.chart_name = %s
                            """,
    "sequencing": """
                            SELECT p.page_name, p.page_number,
                                   s.original_page_number, s.seq, s.confidence
                            FROM page_sequencing_results s
                            JOIN page_list p ON p.id = s.page_id
                            JOIN chart_list c ON c.id = s.chart_id
                            WHERE c.chart_name = %s
                            """,
    "continuity": """
                                SELECT p.page_name, to_jsonb(k)
                                FROM page_continuity_results k
                                JOIN page_list p ON p.id = k.page_id
                                JOIN chart_list c ON c.id = k.chart_id
                                WHERE c.chart_name = %s
                                """,
    "provider_signature": """
                                SELECT p.page_name, s.provider_name, s.signature_present,
                                       to_jsonb(s) ->> 'provider_credentials',
                                       to_jsonb(s) -> 'signers',
                                       to_jsonb(s) ->> 'provider_version',
                                       s.confidence
                                FROM provider_signature_results s
                                JOIN page_list p ON p.id = s.page_id
                                JOIN chart_list c ON c.id = s.chart_id
                                WHERE c.chart_name = %s
                                """,
    "imaging_final": """
                                SELECT p.page_name, f.page_type, f.classification_category,
                                       f.dos_from, f.dos_to, to_jsonb(f) ->> 'page_subtype',
                                       to_jsonb(f) ->> 'specialty',
                                       to_jsonb(f) ->> 'provider_name',
                                       to_jsonb(f) ->> 'signature_present',
                                       to_jsonb(f) ->> 'provider_credentials',
                                       to_jsonb(f) -> 'signers',
                                       to_jsonb(f) ->> 'provider_version',
                                       to_jsonb(f) ->> 'provider_confidence',
                                       -- NULL until changes_2026-10-25.sql adds them.
                                       to_jsonb(f) -> 'confidences',
                                       to_jsonb(f) -> 'review_fields'
                                FROM imaging_final f
                                JOIN page_list p ON p.id = f.page_id
                                JOIN chart_list c ON c.id = f.chart_id
                                WHERE c.chart_name = %s
                                """,
}
# Sections whose table an older database may lack: skipped (no rows) while it is absent.
_OVERLAY_OPTIONAL_TABLE = {
    "continuity": "page_continuity_results",
    "provider_signature": "provider_signature_results",
    "imaging_final": "imaging_final",
}


# get_ocr_text's cache: (chart, kind) -> (fingerprint, response), least recently used out.
_OCR_TEXT_CACHE_SIZE = 64
_OCR_TEXT_CACHE: "OrderedDict[tuple[str, str], tuple[tuple, OcrTextResponse]]" = OrderedDict()
# What the OCR text is built from, in one row: the chart's pages, its rows of this OCR
# type, and the Final OCR skip reasons (page_stage_status). Parameters: ocr_type twice,
# chart name. A Final OCR stage name is matched by both ocr_final1 and ocr_final2.
_OCR_FINGERPRINT_SQL = """
SELECT count(p.id), max(p.updated_at),
       count(o.id), max(o.updated_at),
       (SELECT max(ps.updated_at) FROM page_stage_status ps
          JOIN page_list pp ON pp.id = ps.page_id
         WHERE pp.chart_id = c.id
           AND ps.stage_name = CASE %s WHEN 'docling' THEN 'ocr_final1'
                                       WHEN 'azuredocintel' THEN 'ocr_final2' END)
  FROM chart_list c
  JOIN page_list p ON p.chart_id = c.id
  LEFT JOIN ocr_results o ON o.page_id = p.id AND o.ocr_type = %s
 WHERE c.chart_name = %s
 GROUP BY c.id
"""

# API kind → ocr_results.ocr_type
KIND_TO_OCR_TYPE: dict[str, str] = {
    "preliminary": "tesseract",
    "final1": "docling",
    "final2": "azuredocintel",
}

# schema v7 lifecycle values.
CHART_STATUS_TO_OCR: dict[str, str] = {
    "received": "QUEUED",
    "downloading": "IN_PROGRESS",
    "processing": "IN_PROGRESS",
    "completed": "IMAGING_COMPLETED",
    "failed": "FAILED",
    "needs_review": "IMAGING_COMPLETED",
    "rejected": "IMAGING_COMPLETED",
    # v6 packed the stage name into status. Kept so a database that has not run
    # migration 002 yet still renders sensibly.
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

# v7: once status is just "processing", which stage it is in comes from
# current_stage. These stages mean the imaging modules are running.
IMAGING_STAGES = frozenset(
    {
        "blank_junk",
        "section_headers",
        "kv_extract",
        "member_verify",
        "dos_extract",
        "page_subtype",
        "continuity",
        "encounter_type",
        "page_sequencing",
        "imaging_final",
    }
)

# A chart is Imaging Completed only when every stage in pipeline_stage is done
# for every page — chart_list.status 'completed' / 'needs_review' / 'rejected'.
_IMAGING_DONE_STATUSES = frozenset({"completed", "needs_review", "rejected"})

# How long page → blob locations are cached (Raw_Input listing order = page_number).
_BLOB_LIST_TTL_SEC = 300.0


def _ocr_status_for(status: str | None, current_stage: str | None) -> str:
    """UI status pill from (chart_list.status, chart_list.current_stage)."""
    key = str(status or "").lower()
    mapped = CHART_STATUS_TO_OCR.get(key, "QUEUED")
    if key == "processing" and str(current_stage or "").lower() in IMAGING_STAGES:
        return "IMAGING_IN_PROGRESS"
    return mapped


def _fmt_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return _fmt_dos_display(value)


# The reason a copied page carries when its encounter is not a copy (core-pipeline's
# stages/lib/continuity/duplicates.py writes it).
DUPLICATE_CONTENT_MARK = "duplicate_content_of_page_id:"


def _blank_junk_ui(flag: str | None, subtype: str | None) -> tuple[str | None, bool | None, str | None]:
    """Map DB blank_junk_flag → UI blankOrJunk / isDuplicate / pageType."""
    if not flag:
        return None, None, None
    f = flag.strip().lower()
    if f == "blank":
        return "Yes (Blank)", False, "Not Available"
    if f == "duplicate":
        # A duplicate is not junk (2026-10-29, user): Blank / Junk No, Duplicate Yes.
        return "No", True, "Not Available"
    if f == "junk":
        return "Yes (Junk)", False, subtype or "Junk"
    if f == "not_blank_junk":
        return "No", False, "Not Available"
    return None, None, None


class PostgresFolderRepository(FolderRepository):
    def __init__(
        self,
        database_url: str,
        data_root: Path | None = None,
        metadata_root: Path | None = None,
        db_schema: str = "public",
        disk_data: bool = True,
    ):
        """``data_root`` is the chart workspace. With ``disk_data`` false (Production Mode)
        it serves page images only: charts, page lists and results come from Postgres."""
        self.database_url = _psycopg_url(database_url)
        self._disk_data = disk_data
        self.db_schema = (db_schema or "public").strip() or "public"
        self._local = (
            LocalFolderRepository(data_root, metadata_root=metadata_root)
            if data_root is not None
            else None
        )
        # Per-chart caches for the page image route (TTL _BLOB_LIST_TTL_SEC):
        # folder_id → sorted (key, etag) under the Raw_Input prefix
        self._raw_blob_lists: dict[str, tuple[float, list[tuple[str, str]]]] = {}
        # folder_id → page_number → page_list/chart_list location row
        self._page_rows_cache: dict[str, tuple[float, dict[int, tuple]]] = {}
        # (folder_id, page_number) → resolved blob location
        self._resolved_blobs: dict[tuple[str, int, bool], tuple[float, dict[str, str]]] = {}
        # Processed <output_path>/pages/ per chart: page file name -> (key, etag).
        self._processed_pages: dict[str, tuple[float, dict[str, tuple[str, str]]]] = {}
        # Charts whose missing Blob image was already logged; the last listing error per chart.
        self._no_blob_logged: set[str] = set()
        # Why the last lookup found no blob for a page, for the image route's error.
        self._no_blob_reasons: dict[tuple[str, int], str] = {}
        self._blob_list_errors: dict[str, str] = {}

    def _optional_local(self) -> LocalFolderRepository | None:
        """The workspace as a data fallback (a chart, its page list, the folder list)."""
        return self._local if self._disk_data else None

    def _connect(self):
        return db.connection(self.database_url, self.db_schema)

    def list_folders(self) -> list[FolderSummary]:
        """DB chart_list summaries; light disk merge for local-only folders.

        Avoids a full LocalFolderRepository scan (pages/ocr/imaging per chart).
        """
        from app.services.folder_list import (
            FolderListParams,
            build_folder_list,
        )

        result = build_folder_list(
            database_url=self.database_url,
            db_schema=self.db_schema,
            data_root=self._local.data_root if self._optional_local() else None,
            full_local_rows=None,
            params=FolderListParams(limit=None, offset=0, sort="updated", sort_dir="desc"),
        )
        return result.items

    def _pages_from_db(self, folder_id: str) -> list[tuple[int, str]]:
        """(page_number, page_name) from page_list, ordered for the viewer."""
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT p.page_number, p.page_name
                          FROM page_list p
                          JOIN chart_list c ON c.id = p.chart_id
                         WHERE c.chart_name = %s
                         ORDER BY p.page_number NULLS LAST, p.id
                        """,
                        (folder_id,),
                    )
                    rows = cur.fetchall()
        except Exception as exc:
            logger.warning("page_list lookup failed for %s: %s", folder_id, exc)
            return []
        out: list[tuple[int, str]] = []
        for idx, (num, name) in enumerate(rows, start=1):
            out.append((int(num or idx), str(name)))
        return out

    def _fetch_parallel(self, queries: dict[str, tuple[str, tuple]]) -> dict[str, list[tuple]]:
        """Run independent queries at the same time, each on its own pooled connection:
        name -> rows. Any failure raises (after the others finish)."""

        def fetch(item: tuple[str, tuple[str, tuple]]) -> tuple[str, list[tuple]]:
            name, (sql, params) = item
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(sql, params)
                return name, cur.fetchall()

        with ThreadPoolExecutor(max_workers=min(len(queries), _OVERLAY_WORKERS)) as pool:
            return dict(pool.map(fetch, queries.items()))

    def _folder_from_db(self, folder_id: str) -> FolderDetail | None:
        """Build FolderDetail (pages + OCR flags + chart meta): five independent queries,
        keyed by chart name, run together."""
        try:
            found = self._fetch_parallel({
                "chart": (
                    """
                    SELECT id, status, current_stage, run_id, batch_id
                      FROM chart_list
                     WHERE chart_name = %s
                     LIMIT 1
                    """,
                    (folder_id,),
                ),
                "pages": (
                    """
                    SELECT p.page_number, p.page_name
                      FROM page_list p
                      JOIN chart_list c ON c.id = p.chart_id
                     WHERE c.chart_name = %s
                     ORDER BY p.page_number NULLS LAST, p.id
                    """,
                    (folder_id,),
                ),
                "flags": (
                    """
                    SELECT p.page_name, o.ocr_type
                      FROM ocr_results o
                      JOIN page_list p ON p.id = o.page_id
                      JOIN chart_list c ON c.id = o.chart_id
                     WHERE c.chart_name = %s
                       AND o.raw_text IS NOT NULL
                       AND length(trim(o.raw_text)) > 0
                    """,
                    (folder_id,),
                ),
                # Pages done in every stage of the chain (completed or skipped) — the
                # "Imaging N" count and the page dots.
                "imaging_done": (
                    """
                    SELECT p.page_name
                      FROM page_list p
                      JOIN chart_list c ON c.id = p.chart_id
                     WHERE c.chart_name = %s
                       AND NOT EXISTS (
                           SELECT 1 FROM pipeline_stage s
                            WHERE s.is_phase1
                              AND NOT EXISTS (
                                  SELECT 1 FROM page_stage_status ps
                                   WHERE ps.page_id = p.id
                                     AND ps.stage_name = s.stage_name
                                     AND ps.pass_no = s.pass_no
                                     AND ps.status IN ('completed', 'skipped')
                              )
                       )
                    """,
                    (folder_id,),
                ),
                "manifest": (
                    """
                    SELECT member_name, member_dob, external_member_id
                      FROM manifest_member_list
                     WHERE record_id = %s
                     ORDER BY id
                     LIMIT 1
                    """,
                    (folder_id,),
                ),
            })
        except Exception as exc:
            logger.warning("folder_from_db failed for %s: %s", folder_id, exc)
            return None
        if not found["chart"] or not found["pages"]:
            return None
        _chart_id, status_raw, stage_raw, run_id, batch_id = found["chart"][0]
        page_rows = found["pages"]
        flag_rows = found["flags"]
        imaging_done = {str(r[0]) for r in found["imaging_done"]}
        man = found["manifest"][0] if found["manifest"] else None

        pages: list[tuple[int, str]] = []
        for idx, (num, name) in enumerate(page_rows, start=1):
            pages.append((int(num or idx), str(name)))

        flags: dict[str, dict[str, bool]] = {}
        for page_name, ocr_type in flag_rows:
            flags.setdefault(str(page_name), {})[str(ocr_type)] = True

        page_summaries = [
            PageSummary(
                page_number=num,
                filename=name,
                image_url=f"/api/folders/{folder_id}/pages/{num}/image",
                has_preliminary_ocr=bool(flags.get(name, {}).get("tesseract")),
                has_final1_ocr=bool(flags.get(name, {}).get("docling")),
                has_final2_ocr=bool(flags.get(name, {}).get("azuredocintel")),
                has_imaging=name in imaging_done
                or str(status_raw or "").lower() in _IMAGING_DONE_STATUSES,
            )
            for num, name in pages
        ]
        kinds_present = {
            k
            for page_flags in flags.values()
            for k, ok in page_flags.items()
            if ok
        }
        ocr_processed = sum(
            1
            for k in ("tesseract", "docling", "azuredocintel")
            if k in kinds_present
        )

        status = str(status_raw or "").lower()
        stage = str(stage_raw or "").lower()
        if status == "processing" and stage:
            chart_status = stage
        else:
            chart_status = status or None
        ocr_status = CHART_STATUS_TO_OCR.get(
            str(chart_status or "").lower(), "QUEUED"
        )
        if ocr_status == "IN_PROGRESS" and ocr_processed == 3:
            ocr_status = "IMAGING_IN_PROGRESS"
        elif ocr_status == "QUEUED" and ocr_processed > 0:
            ocr_status = "IN_PROGRESS"

        manifest = None
        if man:
            name, dob, member_id = man
            manifest = ImagingManifestDetails(
                member=name,
                dob=_fmt_date(dob),
                memberId=member_id,
            )

        shown_run, shown_batch = with_run_batch_default(
            str(run_id) if run_id else None,
            str(batch_id) if batch_id else None,
        )
        return FolderDetail(
            id=folder_id,
            name=folder_id,
            page_count=len(pages),
            ocr_processed=ocr_processed,
            imaging_processed=sum(1 for p in page_summaries if p.has_imaging),
            ocr_status=ocr_status,  # type: ignore[arg-type]
            last_updated_at=None,
            run_id=shown_run,
            batch_id=shown_batch,
            manifest=manifest,
            pages=page_summaries,
        )

    def get_folder(self, folder_id: str) -> FolderDetail:
        """Pages + OCR flags from Postgres first — avoid a local disk walk on open.

        Local workspace is only a fallback when the chart is absent from page_list
        (pure Local Mode folders with no DB row).
        """
        db_detail = self._folder_from_db(folder_id)
        if db_detail is not None and db_detail.pages:
            return db_detail

        local = self._optional_local()
        detail: FolderDetail | None = None
        if local is not None:
            try:
                detail = local.get_folder(folder_id)
            except HTTPException:
                detail = None
            except Exception:
                detail = None
        if detail is None or not detail.pages:
            raise HTTPException(
                status_code=404,
                detail=f"Chart not found: {folder_id}",
            )
        return detail

    def get_page_image_path(
        self, folder_id: str, page_number: int, *, corrected: bool = False
    ) -> Path:
        """Local path when a workspace copy exists (fallback for the image route). Page
        images are the source document, not pipeline output: served from the workspace in
        every mode when Blob has none."""
        local = self._local
        if local is None:
            raise HTTPException(
                status_code=404,
                detail="No local page image; Production Mode serves images from blob",
            )
        return local.get_page_image_path(folder_id, page_number, corrected=corrected)

    def _page_rows(self, folder_id: str) -> dict[int, tuple] | None:
        """page_number → (container, blob_path, output_path, page_name,
        use_corrected, image_path, ingest_index), cached per chart.

        Every page image request needs this; querying the whole chart's
        page_list per image made a filmstrip of N thumbnails cost N queries.
        """
        now = time.monotonic()
        cached = self._page_rows_cache.get(folder_id)
        if cached and (now - cached[0]) < _BLOB_LIST_TTL_SEC:
            return cached[1]
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT c.blob_container, c.blob_path, c.output_path,
                               p.page_name, p.page_number, p.use_corrected, p.image_path
                          FROM page_list p
                          JOIN chart_list c ON c.id = p.chart_id
                         WHERE c.chart_name = %s
                         ORDER BY p.page_number NULLS LAST, p.id
                        """,
                        (folder_id,),
                    )
                    rows = cur.fetchall()
        except Exception as exc:
            logger.warning("resolve_page_blob DB failed for %s: %s", folder_id, exc)
            return None

        by_num: dict[int, tuple] = {}
        for idx, (container, blob_path, output_path, page_name, db_num, use_corrected, image_path) in enumerate(
            rows, start=1
        ):
            num = int(db_num or idx)
            by_num.setdefault(
                num,
                (
                    (container or "").strip(),
                    blob_path,
                    output_path,
                    str(page_name),
                    bool(use_corrected),
                    str(image_path or ""),
                    idx,
                ),
            )
        self._page_rows_cache[folder_id] = (now, by_num)
        return by_num

    def resolve_page_blob(
        self, folder_id: str, page_number: int, *, corrected: bool = False
    ) -> dict[str, str] | None:
        """Locate the Azure blob for a page using chart_list + page_list.

        Processed (``chart_list.output_path``) first: ``corrected-pages/`` when
        ``corrected``, then ``pages/``; then the scan as it arrived in
        Raw_Input. The pipeline uploads ``pages/`` to Processed only with
        ``write_mode=all_files``, so most charts take their originals from
        Raw_Input.

        Returns ``container``, ``key``, ``filename`` and ``etag`` (empty when
        unknown). Results are cached per page for ``_BLOB_LIST_TTL_SEC``.
        """
        now = time.monotonic()
        cache_key = (folder_id, page_number, corrected)
        cached = self._resolved_blobs.get(cache_key)
        if cached and (now - cached[0]) < _BLOB_LIST_TTL_SEC:
            return cached[1]

        rows = self._page_rows(folder_id)
        hit = rows.get(page_number) if rows else None
        if hit is None:
            self._no_blob(folder_id, page_number, "no page_list row for this page")
            return None
        container, blob_path, output_path, page_name, _use_corrected, image_path, idx = hit
        if not container:
            # chart_list.blob_container wins; BLOB_CONTAINER is the default.
            from app.core.config import get_settings

            container = get_settings().blob_container.strip().strip("/")
        if not container:
            self._no_blob(folder_id, page_number, "no chart_list.blob_container and no BLOB_CONTAINER")
            return None

        out = (output_path or "").strip().strip("/")
        stem = Path(page_name).stem
        candidates: list[str] = []
        if corrected and out:
            candidates.append(f"{out}/corrected-pages/{stem}.jpg")
            candidates.append(f"{out}/corrected-pages/{page_name}")
            rel = image_path.replace("\\", "/").lstrip("/")
            if rel.startswith("corrected-pages/"):
                candidates.append(f"{out}/{rel}")

        ordered = list(dict.fromkeys(k.lstrip("/") for k in candidates if k.lstrip("/")))
        found = self._first_existing_blob(container, ordered) if ordered else None
        if found is None and out:
            # Processed pages/: one listing per chart, not a probe per image.
            found = self._processed_page_blob(folder_id, container, out, page_name)

        # Raw_Input comes from a live listing, so the key is known to exist.
        raw = None if found else self._raw_input_blob(folder_id, container, blob_path, idx)
        if found is None and raw:
            key, etag = raw
            out_loc = {"container": container, "key": key, "filename": Path(key).name or page_name, "etag": etag}
            self._resolved_blobs[cache_key] = (now, out_loc)
            return out_loc
        if found is None:
            listing_error = self._blob_list_errors.pop(folder_id, None)
            self._no_blob(
                folder_id, page_number,
                f"could not list Blob ({listing_error}); the paths are "
                f"{container}/{out or '(no output_path)'} and {container}/{blob_path or '(no blob_path)'}"
                if listing_error else
                f"not in {container}/{out or '(no output_path)'}/pages/{page_name} "
                f"nor in the Raw_Input listing of {container}/{blob_path or '(no blob_path)'}",
            )
            return None
        key, etag = found
        out_loc = {"container": container, "key": key, "filename": Path(key).name or page_name, "etag": etag}
        self._resolved_blobs[cache_key] = (now, out_loc)
        return out_loc

    def _no_blob(self, folder_id: str, page_number: int, why: str) -> None:
        """Say once per chart why its page images are not in Blob (the route then tries the
        workspace copy), and keep the reason for the page's error."""
        self._no_blob_reasons[(folder_id, page_number)] = why
        if folder_id in self._no_blob_logged:
            return
        self._no_blob_logged.add(folder_id)
        logger.warning("page image not in Blob: chart=%s page=%s — %s", folder_id, page_number, why)

    def no_blob_reason(self, folder_id: str, page_number: int) -> str | None:
        """Why the last lookup found no blob for this page, or None."""
        return self._no_blob_reasons.get((folder_id, page_number))

    def _processed_page_blob(
        self, folder_id: str, container: str, output_path: str, page_name: str
    ) -> tuple[str, str] | None:
        """(key, etag) of ``<output_path>/pages/<page_name>`` when Processed has it."""
        now = time.monotonic()
        cached = self._processed_pages.get(folder_id)
        if cached and (now - cached[0]) < _BLOB_LIST_TTL_SEC:
            by_name = cached[1]
        else:
            try:
                from app.services.blob_store import list_image_blobs

                by_name = {
                    Path(key).name: (key, etag)
                    for key, etag in list_image_blobs(container, f"{output_path}/pages")
                }
            except Exception as exc:
                # Not cached: a sign-in failure must not hide the pages once Blob is back.
                self._blob_list_errors[folder_id] = str(getattr(exc, "detail", None) or exc)
                if getattr(exc, "status_code", None) != 503:
                    logger.warning("Processed pages list failed for %s: %s", folder_id, exc)
                return None
            self._processed_pages[folder_id] = (now, by_name)
        return by_name.get(page_name)

    def _first_existing_blob(self, container: str, keys: list[str]) -> tuple[str, str] | None:
        """First key that exists, with its ETag (fallback path only)."""
        try:
            from app.services.blob_store import _blob_service_client

            client = _blob_service_client()
            for key in keys:
                blob = client.get_blob_client(container=container, blob=key)
                try:
                    props = blob.get_blob_properties()
                    return key, str(props.etag or "").strip('"')
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("blob existence probe skipped: %s", exc)
            return (keys[0], "") if keys else None
        return None

    def _raw_input_blob(
        self,
        folder_id: str,
        container: str,
        blob_path: str | None,
        page_index_1based: int,
    ) -> tuple[str, str] | None:
        """Map page index → (Raw_Input blob key, etag), same sort order as ingest."""
        prefix = (blob_path or "").strip()
        if not container or not prefix:
            return None
        now = time.monotonic()
        cached = self._raw_blob_lists.get(folder_id)
        if cached and (now - cached[0]) < _BLOB_LIST_TTL_SEC:
            blobs = cached[1]
        else:
            try:
                from app.services.blob_store import list_image_blobs

                blobs = list_image_blobs(container, prefix)
                self._raw_blob_lists[folder_id] = (now, blobs)
            except Exception as exc:
                self._blob_list_errors[folder_id] = str(getattr(exc, "detail", None) or exc)
                if getattr(exc, "status_code", None) != 503:  # 503: Blob sign-in failed, reported once
                    logger.warning(
                        "Raw_Input blob list failed for %s (%s/%s): %s",
                        folder_id,
                        container,
                        prefix,
                        exc,
                    )
                return None
        if page_index_1based < 1 or page_index_1based > len(blobs):
            return None
        return blobs[page_index_1based - 1]

    def get_ocr_text(self, folder_id: str, kind: str) -> OcrTextResponse:
        """The chart's OCR text for ``kind``, rebuilt only when its rows changed: a small
        fingerprint query (row counts and latest updated_at of the OCR rows, pages and
        skip reasons) decides, so reopening a chart skips moving every page's OCR JSON."""
        normalized = _normalize_kind(kind)
        ocr_type = KIND_TO_OCR_TYPE[normalized]
        try:
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(_OCR_FINGERPRINT_SQL, (ocr_type, ocr_type, folder_id))
                fingerprint = tuple(cur.fetchone() or ())
        except Exception:
            fingerprint = ()
        key = (folder_id, normalized)
        hit = _OCR_TEXT_CACHE.get(key)
        if fingerprint and hit and hit[0] == fingerprint:
            _OCR_TEXT_CACHE.move_to_end(key)
            return hit[1]
        built = self._build_ocr_text(folder_id, kind)
        if fingerprint:
            _OCR_TEXT_CACHE[key] = (fingerprint, built)
            while len(_OCR_TEXT_CACHE) > _OCR_TEXT_CACHE_SIZE:
                _OCR_TEXT_CACHE.popitem(last=False)
        return built

    def _build_ocr_text(self, folder_id: str, kind: str) -> OcrTextResponse:
        """Assemble OCR from ocr_results into ===== page ===== marker text for the UI."""
        from app.adapters.local.repository import FINAL2_QUALITY_SKIP_MESSAGE

        normalized = _normalize_kind(kind)
        ocr_type = KIND_TO_OCR_TYPE[normalized]

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    if ocr_type in {"azuredocintel", "docling"}:
                        stage_name = (
                            "ocr_final2" if ocr_type == "azuredocintel" else "ocr_final1"
                        )
                        cur.execute(
                            """
                            SELECT p.page_name, o.raw_text, pss.skip_reason
                            FROM page_list p
                            JOIN chart_list c ON c.id = p.chart_id
                            LEFT JOIN ocr_results o
                              ON o.page_id = p.id AND o.ocr_type = %s
                            LEFT JOIN page_stage_status pss
                              ON pss.page_id = p.id
                             AND pss.stage_name = %s
                             AND pss.pass_no = 1
                            WHERE c.chart_name = %s
                            ORDER BY p.page_number NULLS LAST, p.id
                            """,
                            (ocr_type, stage_name, folder_id),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT p.page_name, o.raw_text, NULL::text AS skip_reason
                            FROM ocr_results o
                            JOIN chart_list c ON c.id = o.chart_id
                            JOIN page_list p ON p.id = o.page_id
                            WHERE c.chart_name = %s
                              AND o.ocr_type = %s
                            ORDER BY p.page_number NULLS LAST, p.id
                            """,
                            (folder_id, ocr_type),
                        )
                    rows = cur.fetchall()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Postgres OCR lookup failed: {exc}",
            ) from exc

        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"No ocr_results for chart={folder_id!r} ocr_type={ocr_type!r}",
            )

        chunks: list[str] = []
        headers_by_file: dict[str, list[OcrSectionHeader]] = {}
        for page_name, raw_text, skip_reason in rows:
            body = (raw_text or "").strip()
            parsed: dict[str, Any] | None = None
            if ocr_type in {"azuredocintel", "docling"} and body.startswith("{"):
                try:
                    import json

                    maybe = json.loads(body)
                    if isinstance(maybe, dict) and (
                        "content" in maybe or "markdown" in maybe
                    ):
                        parsed = maybe
                        body = str(
                            maybe.get("content")
                            or maybe.get("markdown")
                            or ""
                        ).strip()
                except Exception:
                    pass
            if (
                ocr_type in {"azuredocintel", "docling"}
                and not body
                and parsed is not None
            ):
                reason = str(
                    parsed.get("skippedReason") or parsed.get("skipped_reason") or ""
                ).strip().lower()
                if reason == "high_quality_printed":
                    body = FINAL2_QUALITY_SKIP_MESSAGE
                elif reason in {
                    "blank_junk_pass1",
                    "blank_junk",
                    "blank_junk_pass2",
                }:
                    from app.adapters.local.repository import (
                        FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE,
                    )

                    body = FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE
            if (
                ocr_type == "azuredocintel"
                and not body
                and str(skip_reason or "").strip().lower() == "high_quality_printed"
            ):
                body = FINAL2_QUALITY_SKIP_MESSAGE
            if (
                ocr_type in {"azuredocintel", "docling"}
                and not body
                and str(skip_reason or "").strip().lower()
                in {"blank_junk_pass1", "blank_junk", "blank_junk_pass2"}
            ):
                from app.adapters.local.repository import (
                    FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE,
                )

                body = FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE
            if body and ocr_type in {"azuredocintel", "docling"}:
                from app.adapters.local.repository import clean_ocr_display_text

                if not body.startswith("Skipped for "):
                    body = clean_ocr_display_text(body)
            if ocr_type in {"docling", "azuredocintel"} and parsed is not None:
                from app.adapters.local.repository import (
                    _filter_headers_against_canon,
                    _section_headers_from_page,
                )

                headers = _filter_headers_against_canon(
                    _section_headers_from_page(parsed)
                )
                if headers:
                    headers_by_file[str(page_name)] = headers
                    headers_by_file[str(page_name).lower()] = headers
            chunks.append(f"===== {page_name} =====\n{body}".rstrip())

        if ocr_type == "azuredocintel" and not any(
            (c.split("\n", 1)[-1] if "\n" in c else "").strip() for c in chunks
        ):
            raise HTTPException(
                status_code=404,
                detail=f"No ocr_results for chart={folder_id!r} ocr_type={ocr_type!r}",
            )

        return OcrTextResponse(
            folder_id=folder_id,
            kind=normalized,
            text="\n\n".join(chunks),
            section_headers_by_file=headers_by_file,
        )

    def _manifest_from_db(self, folder_id: str) -> ImagingManifestDetails | None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT m.member_name, m.member_dob, m.external_member_id
                        FROM manifest_member_list m
                        WHERE m.record_id = %s
                        ORDER BY m.id
                        LIMIT 1
                        """,
                        (folder_id,),
                    )
                    row = cur.fetchone()
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Postgres manifest lookup failed: {exc}",
            ) from exc

        if not row:
            return None
        name, dob, member_id = row
        return ImagingManifestDetails(
            member=name,
            dob=_fmt_date(dob),
            memberId=member_id,
        )

    def _verification_from_db(self, folder_id: str) -> ImagingVerificationDetails | None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT mv.final_status, mv.matched_name, m.external_member_id,
                               mv.confidence, mv.pages_matched, mv.pages_checked,
                               mv.decision_reason
                        FROM member_verification_summary mv
                        JOIN chart_list c ON c.id = mv.chart_id
                        LEFT JOIN manifest_member_list m ON m.id = mv.matched_member_list_id
                        WHERE c.chart_name = %s
                        """,
                        (folder_id,),
                    )
                    row = cur.fetchone()
        except Exception:
            return None
        if not row:
            return None
        return ImagingVerificationDetails(
            finalStatus=row[0],
            matchedName=row[1],
            matchedMemberId=row[2],
            matchedConfidence=float(row[3]) if row[3] is not None else None,
            pagesMatched=row[4],
            pagesChecked=row[5],
            decisionReason=row[6],
        )

    def _table_exists(self, table: str) -> bool:
        """Whether ``table`` exists (db.table_exists, cached)."""
        return db.table_exists(self.database_url, self.db_schema, table)

    def _overlay_rows(self, folder_id: str) -> dict[str, list[tuple] | None]:
        """Every overlay section's rows for the chart, fetched in parallel: section ->
        rows, [] when its optional table is absent, None when its query failed."""

        def fetch(name: str) -> tuple[str, list[tuple] | None]:
            try:
                table = _OVERLAY_OPTIONAL_TABLE.get(name)
                if table and not self._table_exists(table):
                    return name, []
                with self._connect() as conn, conn.cursor() as cur:
                    cur.execute(_OVERLAY_SQL[name], (folder_id,))
                    return name, cur.fetchall()
            except Exception as exc:
                logger.warning("imaging overlay %s skipped for %s: %s", name, folder_id, exc)
                return name, None

        with ThreadPoolExecutor(max_workers=_OVERLAY_WORKERS) as pool:
            return dict(pool.map(fetch, _OVERLAY_SQL))

    def _page_imaging_from_db(self, folder_id: str) -> dict[str, dict[str, Any]]:
        """page_name → field dict from quality / BJ / member / DOS tables."""
        by_page: dict[str, dict[str, Any]] = {}

        def _ensure(name: str) -> dict[str, Any]:
            return by_page.setdefault(name, {})

        rows = self._overlay_rows(folder_id)
        try:
            # codable display, shared by page_classification and imaging_final
            codeable_display = {
                "codeable": "Codable",
                "non_codeable": "Non-Codable",
                "discharge_summary": "Discharge",
                "discharge_frequency": "Discharge",
                "not_sure": "Not Sure",
            }

            with _skip_on_error("quality", folder_id):
                # Quality
                for (
                    page_name, hw, conf, qscore, qtag, orient, tilt, mirrored,
                    doc_type, visible, hw_area, measured,
                ) in rows["quality"] or []:
                    fields = _ensure(str(page_name))
                    if hw:
                        label = str(hw).strip().lower()
                        if "hand" in label:
                            fields["handwrittenOrPrinted"] = "Handwritten"
                        elif "uncertain" in label:
                            fields["handwrittenOrPrinted"] = "Uncertain"
                        elif "mix" in label:
                            fields["handwrittenOrPrinted"] = "Mixed"
                        else:
                            fields["handwrittenOrPrinted"] = "Printed"
                    if conf is not None:
                        fields["handwrittenOrPrintedConfidence"] = float(conf)
                    if qscore is not None:
                        fields["pageQualityConfidence"] = float(qscore)
                    if qtag:
                        fields["pageQualityTag"] = str(qtag)
                    if orient is not None:
                        fields["orientationAngle"] = float(orient)
                    if tilt is not None:
                        fields["tiltAngle"] = float(tilt)
                    if mirrored is not None:
                        fields["mirrored"] = bool(mirrored)
                    fields.update(
                        page_type_fields(doc_type, visible, hw_area)
                    )
                    if isinstance(measured, dict) and measured:
                        fields["measuredConfidences"] = {
                            k: float(v) for k, v in measured.items() if isinstance(v, (int, float))
                        }

            with _skip_on_error("blank_junk", folder_id):
                # Blank/junk — v7 stamps exactly one final row per page,
                # so precedence is not re-derived here any more.
                for page_name, flag, subtype, conf, duplicate_of, best, reason in rows["blank_junk"] or []:
                    fields = _ensure(str(page_name))
                    blank, dup, page_type = _blank_junk_ui(flag, subtype)
                    fields["blankOrJunk"] = blank
                    fields["isDuplicate"] = dup
                    pointing = (flag or "").strip().lower() in ("not_blank_junk", "junk") and bool(duplicate_of)
                    # A copied page whose encounter is not a copy (continuity, 2026-10-29).
                    content = pointing and DUPLICATE_CONTENT_MARK in (reason or "")
                    fields["isDuplicateContent"] = content
                    # Any other Main or junk page pointing at another is a partial (95-98%) duplicate.
                    fields["isPartialDuplicate"] = pointing and not content
                    fields["pageType"] = page_type
                    if duplicate_of:
                        fields["duplicateOf"] = str(duplicate_of)
                    if conf is not None:
                        fields["pageTypeConfidence"] = float(conf)
                        fields["blankJunkConfidence"] = float(conf)
                    if best is not None:
                        fields["duplicateConfidence"] = float(best)

            with _skip_on_error("member", folder_id):
                # Member extraction
                for page_name, name, dob, mid, conf, member_status, other_names in rows["member"] or []:
                    if not page_name:
                        continue
                    fields = _ensure(str(page_name))
                    fields["memberName"] = name
                    fields["memberDob"] = _fmt_date(dob)
                    fields["memberId"] = mid
                    if conf is not None:
                        fields["memberConfidence"] = float(conf)
                    fields["memberPageStatus"] = member_status
                    if isinstance(other_names, list):
                        fields["memberOtherNames"] = [str(n) for n in other_names if str(n).strip()]

            with _skip_on_error("dos", folder_id):
                # DOS (Extracted): the DOS stage's own row per page, the same values it
                # writes to <chart>_dos.csv. schema/v1.sql is the only schema.
                for page_name, d_from, d_to, doc_from, doc_to, conf in rows["dos"] or []:
                    fields = _ensure(str(page_name))
                    fields["dosFrom"] = _fmt_date(d_from)
                    fields["dosTo"] = _fmt_date(d_to)
                    fields["docDosFrom"] = _fmt_date(doc_from)
                    fields["docDosTo"] = _fmt_date(doc_to)
                    if conf is not None:
                        fields["dosConfidence"] = float(conf)

            with _skip_on_error("page_classification", folder_id):
                # Codable / non-codable / discharge (page_classification)
                for page_name, subtype, cat, conf, page_type, specialty, specialty_conf, axes_json in rows["page_classification"] or []:
                    fields = _ensure(str(page_name))
                    if isinstance(axes_json, dict):
                        sub = subtype_confidence(axes_json.get("l2"), axes_json.get("l2_confidence"),
                                                 axes_json.get("l3"), axes_json.get("l3_confidence"))
                        if sub is not None:
                            fields["subtypeConfidence"] = sub
                    fields["specialty"] = specialty or "Unknown"
                    if specialty_conf:
                        fields["specialtyConfidence"] = float(specialty_conf)
                    key = str(cat or "").strip()
                    shown = compose_page_type(page_type, subtype)
                    if shown:
                        fields["pageType"] = shown
                    fields["isCodeable"] = codeable_display.get(key, key or "Not Sure")
                    if conf is not None:
                        fields["pageTypeConfidence"] = float(conf)

            # Main pages with no page_classification row keep junk's
            # pageType=\"Not Available\" — stamp Not Sure for codability.
            for fields in by_page.values():
                pt = str(fields.get("pageType") or "").strip()
                if (
                    pt == "Not Available"
                    and not fields.get("isCodeable")
                ):
                    fields["isCodeable"] = "Not Sure"

            with _skip_on_error("encounter_type", folder_id):
                # Encounter type (DOS-wide TF)
                enc_display = {
                    "outpatient_f2f": "Outpatient (F2F)",
                    "outpatient_tele": "Outpatient (Tele)",
                    "inpatient": "Inpatient",
                    "home": "Home",
                    "not_applicable": "Not Available",
                }
                for page_name, et, conf in rows["encounter_type"] or []:
                    fields = _ensure(str(page_name))
                    key = str(et or "").strip()
                    fields["encounterType"] = enc_display.get(key, key)
                    if conf is not None:
                        fields["encounterConfidence"] = float(conf)

            with _skip_on_error("sequencing", folder_id):
                # Page sequencing
                for page_name, page_num, orig, seq, conf in rows["sequencing"] or []:
                    fields = _ensure(str(page_name))
                    current = orig if orig is not None else page_num
                    if current is not None:
                        fields["currentSequence"] = int(current)
                    if seq is not None:
                        fields["actualSequence"] = int(seq)
                    if conf is not None:
                        fields["sequenceConfidence"] = float(conf)

            with _skip_on_error("continuity", folder_id):
                # Document continuity, then Final values. A database without
                # these tables yet skips them, not the page.
                if rows["continuity"]:
                    # The gap scorer's columns (2026-10-20) may not exist yet: read the
                    # row as JSON so an older database still shows its documents.
                    members: dict[int, list[dict[str, Any]]] = {}
                    for page_name, k in rows["continuity"] or []:
                        k = k or {}
                        document = k.get("document_seq")
                        position = k.get("position")
                        fields = _ensure(str(page_name))
                        fields["documentSeq"] = int(document) if document is not None else None
                        fields["documentPosition"] = position
                        # The engine's own label (2026-10-25), else rebuilt from position.
                        fields["documentLabel"] = k.get("label") or continuity_label(
                            position, int(document) if document is not None else None,
                            k.get("confidence_level"),
                        )
                        fields["continuityRelation"] = k.get("relation")
                        fields["continuityDecidedBy"] = k.get("decided_by")
                        fields["continuityEvidence"] = k.get("evidence")
                        fields["continuityConfidence"] = (
                            float(k["confidence"]) if k.get("confidence") is not None else None
                        )
                        if document is not None:
                            members.setdefault(int(document), []).append(fields)
                    # Documents and junk groups share the numbering: a group whose every
                    # page is junk reads 'New (Junk 4)'.
                    for document, group in members.items():
                        if all(str(f.get("blankOrJunk") or "").lower().startswith("yes (junk)") for f in group):
                            for f in group:
                                f["documentLabel"] = (f.get("documentLabel") or "").replace(
                                    f"(Doc {document})", f"(Junk {document})"
                                )

            with _skip_on_error("provider_signature", folder_id):
                # The extraction stage's signature rows (Local reads provider_signature.csv);
                # imaging_final, below, replaces them with the document's values once it ran.
                for (page_name, provider, signed, credentials, signers, version,
                     provider_confidence) in rows["provider_signature"] or []:
                    fields = _ensure(str(page_name))
                    people = signer_fields(signers, provider, credentials, version)
                    fields.update({key: value for key, value in people.items() if value})
                    if provider_confidence is not None:
                        fields["providerSignatureConfidence"] = float(provider_confidence)
                    if signed is not None:
                        fields["providerSignature"] = "Yes" if signed else "No"

            with _skip_on_error("imaging_final", folder_id):
                if rows["imaging_final"]:
                    for (page_name, final_type, category, final_from, final_to, final_sub,
                         final_specialty, provider, signed, credentials, signers, version,
                         provider_confidence, confidences, review) in rows["imaging_final"] or []:
                        fields = _ensure(str(page_name))
                        if isinstance(confidences, dict):
                            fields["finalConfidences"] = {
                                k: float(v) for k, v in confidences.items() if isinstance(v, (int, float))
                            }
                        if isinstance(review, list):
                            fields["reviewFields"] = [str(k) for k in review]
                        # Provider Extraction: the signature rows the extraction stage
                        # writes (the same as provider_signature.csv); the name cell
                        # carries the credentials ('Ramsey Luce, R.N.').
                        people = signer_fields(signers, provider, credentials, version)
                        fields.update({key: value for key, value in people.items() if value})
                        if provider_confidence not in (None, ""):
                            fields["providerSignatureConfidence"] = float(provider_confidence)
                        if signed is not None:
                            fields["providerSignature"] = "Yes" if signed == "true" else "No"
                        fields["finalSpecialty"] = final_specialty or None
                        fields["finalPageType"] = compose_page_type(final_type, final_sub) or None
                        fields["finalCodeable"] = codeable_display.get(category or "")
                        fields["finalDosFrom"] = _fmt_date(final_from)
                        fields["finalDosTo"] = _fmt_date(final_to)
        except Exception as exc:
            logger.warning("imaging overlay unavailable for %s: %s", folder_id, exc)
            return by_page

        return by_page
    def get_imaging(self, folder_id: str) -> ImagingDocumentResponse:
        """Build imaging panel entirely from Postgres; page skeleton from page_list.

        Prefer DB page names over a local disk walk — scanning DATA_ROOT for every
        chart open was a major cost on large workspaces / external drives.
        """
        # The loaders below are independent: run them together (each on its own pooled
        # connection) instead of one after another.
        with ThreadPoolExecutor(max_workers=_OVERLAY_WORKERS) as pool:
            pages_job = pool.submit(self._pages_from_db, folder_id)
            fields_job = pool.submit(self._page_imaging_from_db, folder_id)
            manifest_job = pool.submit(self._manifest_from_db, folder_id)
            verification_job = pool.submit(self._verification_from_db, folder_id)
            flags_job = pool.submit(self._sections_from_db, folder_id)
        page_files = [(num, Path(name)) for num, name in pages_job.result()]
        if not page_files:
            local = self._optional_local()
            if local is not None:
                try:
                    folder_dir = local._folder_dir(folder_id)  # noqa: SLF001
                    page_files = local._page_files(folder_dir)  # noqa: SLF001
                except Exception:
                    page_files = []
        pages = empty_imaging_pages(page_files)

        db_fields = fields_job.result()
        merged: list[ImagingPageResult] = []
        for page in pages:
            hit = db_fields.get(page.fileName) or db_fields.get(page.fileName.lower())
            if not hit:
                # try stem match
                stem = Path(page.fileName).stem
                for key, val in db_fields.items():
                    if Path(key).stem == stem:
                        hit = val
                        break
            if hit:
                merged.append(page.model_copy(update=hit))
            else:
                merged.append(page)
        merged = inherit_duplicate_blank_junk(merged)
        merged = attach_ground_truth(
            merged, folder_id, self.database_url, self.db_schema
        )

        manifest = manifest_job.result() or ImagingManifestDetails()
        verification = verification_job.result()
        # "Processed" means the stage wrote rows — not that it found a value.
        # Using non-empty memberName/dosFrom here made empty-but-finished charts
        # show "Yet to Process" instead of "Not Found".
        stage_flags = flags_job.result()
        sections = ImagingSectionsProcessed(
            member=stage_flags["member"] or verification is not None,
            dos=stage_flags["dos"],
            hw=stage_flags["hw"]
            or any(p.handwrittenOrPrinted for p in merged),
            quality=stage_flags["quality"]
            or any(
                p.pageQualityTag or p.pageQualityConfidence is not None for p in merged
            ),
            rotation=stage_flags["rotation"]
            or any(
                p.orientationAngle is not None or p.tiltAngle is not None for p in merged
            ),
            junk=stage_flags["junk"]
            or any(p.blankOrJunk is not None or p.isDuplicate is not None for p in merged),
            codeable=stage_flags["codeable"]
            or any(p.isCodeable is not None for p in merged),
            encounter=stage_flags["encounter"]
            or any(p.encounterType is not None for p in merged),
            sequencing=stage_flags["sequencing"]
            or any(p.actualSequence is not None for p in merged),
            verification=verification is not None,
        )

        return ImagingDocumentResponse(
            folder_id=folder_id,
            manifest=manifest,
            verification=verification,
            verifications=[verification] if verification else [],
            pages=merged,
            sectionsProcessed=sections,
        )

    def _sections_from_db(self, folder_id: str) -> dict[str, bool]:
        """Which imaging sections have any result rows for this chart.

        One round-trip with EXISTS (chart_id resolved once) instead of up to ten
        sequential queries — the previous loop dominated get_imaging latency.
        """
        flags = {
            "member": False,
            "dos": False,
            "hw": False,
            "quality": False,
            "rotation": False,
            "junk": False,
            "codeable": False,
            "encounter": False,
            "sequencing": False,
        }
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM chart_list WHERE chart_name = %s LIMIT 1",
                        (folder_id,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return flags
                    chart_id = row[0]
                    cur.execute(
                        """
                        SELECT
                          EXISTS(
                            SELECT 1 FROM member_extraction_results
                             WHERE chart_id = %s LIMIT 1
                          )
                          OR EXISTS(
                            SELECT 1 FROM member_verification_summary
                             WHERE chart_id = %s LIMIT 1
                          ),
                          EXISTS(
                            SELECT 1 FROM dos_extraction_results
                             WHERE chart_id = %s LIMIT 1
                          ),
                          EXISTS(
                            SELECT 1 FROM ocr_quality_results
                             WHERE chart_id = %s LIMIT 1
                          ),
                          EXISTS(
                            SELECT 1 FROM blank_junk_classification
                             WHERE chart_id = %s LIMIT 1
                          ),
                          EXISTS(
                            SELECT 1 FROM page_classification
                             WHERE chart_id = %s LIMIT 1
                          ),
                          EXISTS(
                            SELECT 1 FROM encounter_type_results
                             WHERE chart_id = %s LIMIT 1
                          )
                          -- Unresolved visits write no row, so a finished
                          -- stage counts even when every page is unresolved.
                          OR EXISTS(
                            SELECT 1 FROM page_stage_status
                             WHERE chart_id = %s
                               AND stage_name = 'encounter_type'
                               AND status IN ('completed', 'skipped')
                             LIMIT 1
                          ),
                          EXISTS(
                            SELECT 1 FROM page_sequencing_results
                             WHERE chart_id = %s LIMIT 1
                          )
                        """,
                        (chart_id,) * 9,
                    )
                    hit = cur.fetchone()
                    if not hit:
                        return flags
                    (
                        member,
                        dos,
                        quality_hw_rot,
                        junk,
                        codeable,
                        encounter,
                        sequencing,
                    ) = hit
                    flags["member"] = bool(member)
                    flags["dos"] = bool(dos)
                    # hw / quality / rotation all come from ocr_quality_results
                    q = bool(quality_hw_rot)
                    flags["hw"] = q
                    flags["quality"] = q
                    flags["rotation"] = q
                    flags["junk"] = bool(junk)
                    flags["codeable"] = bool(codeable)
                    flags["encounter"] = bool(encounter)
                    flags["sequencing"] = bool(sequencing)
        except Exception:
            return flags
        return flags
