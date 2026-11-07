"""Production Mode repository — read pipeline outputs from Postgres.

Page images come from Azure Blob using ``chart_list.blob_container`` +
``blob_path`` (Raw_Input), with Processed ``output_path`` pages/corrected as
fallbacks (store/blob_pages.py). OCR text, manifest, quality, blank/junk, member,
verification, and DOS come from schema tables; the rows become responses in
store/rows.py, which the Cosmos repository shares.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import HTTPException

from app.store.base import FolderRepository
from app.store.blob_pages import BLOB_LIST_TTL_SEC as _BLOB_LIST_TTL_SEC, BlobPageImages
from app.store.shared import _normalize_kind
from app.store import rows as _rows
# Re-exported: services and tests import these from here.
from app.store.rows import (  # noqa: F401
    CHART_STATUS_TO_OCR,
    DUPLICATE_CONTENT_MARK,
    IMAGING_DONE_STATUSES as _IMAGING_DONE_STATUSES,
    IMAGING_STAGES,
    KIND_TO_OCR_TYPE,
    _blank_junk_ui,
    _fmt_date,
    _skip_on_error,
)
from app.core.schemas import (
    FolderDetail,
    FolderSummary,
    ImagingDocumentResponse,
    ImagingManifestDetails,
    ImagingVerificationDetails,
    OcrTextResponse,
)
from app.services import db
from app.services.db import psycopg_url as _psycopg_url
from app.services.ground_truth import attach_ground_truth

logger = logging.getLogger("review_ui.postgres")


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
                                   -- NULL until migrations/2026-10-22.sql has replaced the view.
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
                                   -- NULL until migrations/2026-10-27.sql adds the column.
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
                                       -- NULL until migrations/2026-10-25.sql adds them.
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


class PostgresFolderRepository(BlobPageImages, FolderRepository):
    def __init__(
        self,
        database_url: str,
        data_root: Path | None = None,
        metadata_root: Path | None = None,
        db_schema: str = "public",
        disk_data: bool = True,
    ):
        """Charts, page lists and results from Postgres; page images from Azure Blob. Nothing
        is read from disk (2026-11-07). ``data_root``, ``metadata_root`` and ``disk_data`` are
        accepted for old callers and ignored."""
        self.database_url = _psycopg_url(database_url)
        self._disk_data = disk_data
        self.db_schema = (db_schema or "public").strip() or "public"
        self._init_blob_caches()

    def _optional_local(self) -> None:
        """No disk fallback (2026-11-07): kept so old callers still get an answer."""
        return None

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
            data_root=None,
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
        return _rows.folder_detail(
            folder_id,
            found["chart"][0] if found["chart"] else None,
            found["pages"],
            found["flags"],
            {str(r[0]) for r in found["imaging_done"]},
            found["manifest"][0] if found["manifest"] else None,
        )

    def get_folder(self, folder_id: str) -> FolderDetail:
        """Pages and OCR flags from Postgres; 404 when the chart has no pages there."""
        db_detail = self._folder_from_db(folder_id)
        if db_detail is not None and db_detail.pages:
            return db_detail

        raise HTTPException(status_code=404, detail=f"Chart not found: {folder_id}")

    def get_page_image_path(
        self, folder_id: str, page_number: int, *, corrected: bool = False
    ) -> Path:
        """Never a disk path: page images come from Azure Blob only (2026-11-07)."""
        raise HTTPException(
            status_code=404,
            detail="No page image: review-ui serves page images from Azure Blob only",
        )

    def _page_location_rows(self, folder_id: str) -> list[tuple]:
        """(blob_container, blob_path, output_path, page_name, page_number, use_corrected,
        image_path) per page, in page order."""
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
                return cur.fetchall()

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
        return _rows.ocr_text(folder_id, kind, rows)

    def _manifest_from_db(self, folder_id: str) -> ImagingManifestDetails | None:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT m.member_name, m.member_dob, m.external_member_id,
                               to_jsonb(m) ->> 'measure', to_jsonb(m) ->> 'service_note'
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

        return _rows.manifest_details(row)

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
        return _rows.verification_details(row)

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
        return _rows.page_imaging_fields(folder_id, self._overlay_rows(folder_id))

    def get_imaging(self, folder_id: str) -> ImagingDocumentResponse:
        """Build imaging panel entirely from Postgres; page skeleton from page_list."""
        # The loaders below are independent: run them together (each on its own pooled
        # connection) instead of one after another.
        with ThreadPoolExecutor(max_workers=_OVERLAY_WORKERS) as pool:
            pages_job = pool.submit(self._pages_from_db, folder_id)
            fields_job = pool.submit(self._page_imaging_from_db, folder_id)
            manifest_job = pool.submit(self._manifest_from_db, folder_id)
            verification_job = pool.submit(self._verification_from_db, folder_id)
            flags_job = pool.submit(self._sections_from_db, folder_id)
        return _rows.imaging_document(
            folder_id,
            pages_job.result(),
            fields_job.result(),
            manifest_job.result(),
            verification_job.result(),
            flags_job.result(),
            lambda pages: attach_ground_truth(pages, folder_id, self.database_url, self.db_schema),
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
