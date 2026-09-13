"""Production Mode repository — read pipeline outputs from Postgres.

Page images still come from DATA_ROOT (local folders). OCR text, manifest,
quality, blank/junk, member, verification, and DOS come from schema v6 tables.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

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
    OcrTextResponse,
    PageSummary,
)
from app.services.imaging_overlays import empty_imaging_pages

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
    "member_verify": "IMAGING_IN_PROGRESS",
    "dos_extract": "IMAGING_IN_PROGRESS",
}

# v7: once status is just "processing", which stage it is in comes from
# current_stage. These stages mean the imaging modules are running.
IMAGING_STAGES = frozenset(
    {"blank_junk", "member_verify", "dos_extract"}
)


def _ocr_status_for(status: str | None, current_stage: str | None) -> str:
    """UI status pill from (chart_list.status, chart_list.current_stage)."""
    key = str(status or "").lower()
    mapped = CHART_STATUS_TO_OCR.get(key, "QUEUED")
    if key == "processing" and str(current_stage or "").lower() in IMAGING_STAGES:
        return "IMAGING_IN_PROGRESS"
    return mapped


def _psycopg_url(database_url: str) -> str:
    """Accept sqlalchemy-style postgresql+psycopg:// and plain postgresql://."""
    url = database_url.strip()
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgres+psycopg://"):
        return "postgresql://" + url[len("postgres+psycopg://") :]
    return url


def _db_schema() -> str:
    import os

    return (os.environ.get("DB_SCHEMA") or os.environ.get("PG_SCHEMA") or "public").strip() or "public"


def _fmt_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%m/%d/%Y")
    if isinstance(value, date):
        return value.strftime("%m/%d/%Y")
    return _fmt_dos_display(value)


def _blank_junk_ui(flag: str | None, subtype: str | None) -> tuple[str | None, bool | None, str | None]:
    """Map DB blank_junk_flag → UI blankOrJunk / isDuplicate / pageType."""
    if not flag:
        return None, None, None
    f = flag.strip().lower()
    if f == "blank":
        return "Yes (Blank)", False, "Not Available"
    if f == "duplicate":
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
        db_schema: str = "public",
    ):
        self.database_url = _psycopg_url(database_url)
        self.db_schema = (db_schema or "public").strip() or "public"
        self._local = LocalFolderRepository(data_root) if data_root is not None else None

    def _require_local(self) -> LocalFolderRepository:
        if self._local is None:
            raise HTTPException(
                status_code=501,
                detail="Production Mode needs DATA_ROOT for page images.",
            )
        return self._local

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail="Install psycopg: pip install 'psycopg[binary]'",
            ) from exc
        conn = psycopg.connect(self.database_url)
        schema = self.db_schema or _db_schema()
        if not schema.replace("_", "").isalnum():
            conn.close()
            raise HTTPException(status_code=500, detail=f"Invalid DB_SCHEMA: {schema!r}")
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
        return conn

    def list_folders(self) -> list[FolderSummary]:
        """Prefer charts known to Postgres; fall back to local DATA_ROOT listing."""
        local = self._require_local()
        local_by_id = {f.id: f for f in local.list_folders()}
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT chart_name, page_count, status, updated_at,
                               current_stage, source
                        FROM chart_list
                        WHERE source <> 'manifest' OR page_count > 0
                        ORDER BY updated_at DESC NULLS LAST, id DESC
                        """
                    )
                    rows = cur.fetchall()
        except Exception:
            return list(local_by_id.values())

        if not rows:
            return list(local_by_id.values())

        out: list[FolderSummary] = []
        seen: set[str] = set()
        for chart_name, page_count, status, updated_at, current_stage, _source in rows:
            name = str(chart_name)
            seen.add(name)
            local_f = local_by_id.get(name)
            ocr_status = _ocr_status_for(status, current_stage)
            if local_f and ocr_status in {"QUEUED", "IN_PROGRESS"}:
                # Prefer richer local disk-derived status when pipeline still early
                ocr_status = local_f.ocr_status
            out.append(
                FolderSummary(
                    id=name,
                    name=name,
                    page_count=int(page_count or (local_f.page_count if local_f else 0) or 0),
                    ocr_processed=local_f.ocr_processed if local_f else 0,
                    imaging_processed=local_f.imaging_processed if local_f else 0,
                    ocr_status=ocr_status,  # type: ignore[arg-type]
                    last_updated_at=updated_at or (local_f.last_updated_at if local_f else None),
                )
            )
        # Include local-only folders not yet in chart_list
        for fid, folder in local_by_id.items():
            if fid not in seen:
                out.append(folder)
        return out

    def get_folder(self, folder_id: str) -> FolderDetail:
        """Page images from local; OCR presence flags from ocr_results when available."""
        detail = self._require_local().get_folder(folder_id)
        flags = self._ocr_flags_from_db(folder_id)
        if flags is None:
            return detail

        pages: list[PageSummary] = []
        for p in detail.pages:
            page_flags = flags.get(p.filename, {})
            pages.append(
                PageSummary(
                    page_number=p.page_number,
                    filename=p.filename,
                    image_url=p.image_url,
                    has_preliminary_ocr=bool(page_flags.get("tesseract")),
                    has_final1_ocr=bool(page_flags.get("docling")),
                    has_final2_ocr=bool(page_flags.get("azuredocintel")),
                    has_imaging=p.has_imaging,
                )
            )

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
        chart_status = self._chart_status(folder_id)
        if chart_status and chart_status in CHART_STATUS_TO_OCR:
            ocr_status = CHART_STATUS_TO_OCR[chart_status]
            if ocr_status == "IN_PROGRESS" and ocr_processed == 3:
                ocr_status = "IMAGING_IN_PROGRESS"
            elif ocr_status == "QUEUED" and ocr_processed > 0:
                ocr_status = "IN_PROGRESS"
        elif ocr_processed == 3:
            ocr_status = detail.ocr_status
            if ocr_status not in (
                "IMAGING_COMPLETED",
                "IMAGING_IN_PROGRESS",
                "COMPLETED",
            ):
                ocr_status = "COMPLETED"
        elif ocr_processed > 0:
            ocr_status = "IN_PROGRESS"
        else:
            ocr_status = "QUEUED"

        return FolderDetail(
            id=detail.id,
            name=detail.name,
            page_count=detail.page_count,
            ocr_processed=ocr_processed,
            imaging_processed=detail.imaging_processed,
            ocr_status=ocr_status,  # type: ignore[arg-type]
            last_updated_at=detail.last_updated_at,
            pages=pages,
        )

    def get_page_image_path(self, folder_id: str, page_number: int) -> Path:
        return self._require_local().get_page_image_path(folder_id, page_number)

    def _chart_status(self, folder_id: str) -> str | None:
        """Effective status for the UI pill, folding in current_stage (v7)."""
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT status, current_stage FROM chart_list
                        WHERE chart_name = %s
                        """,
                        (folder_id,),
                    )
                    row = cur.fetchone()
            if not row or not row[0]:
                return None
            status = str(row[0]).lower()
            stage = str(row[1] or "").lower()
            # Surface the stage where v6 callers expected to find it, so the
            # existing CHART_STATUS_TO_OCR lookups below keep working.
            if status == "processing" and stage:
                return stage
            return status
        except Exception:
            return None

    def _ocr_flags_from_db(self, folder_id: str) -> dict[str, dict[str, bool]] | None:
        """page_name → {ocr_type: True}."""
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT p.page_name, o.ocr_type
                        FROM ocr_results o
                        JOIN chart_list c ON c.id = o.chart_id
                        JOIN page_list p ON p.id = o.page_id
                        WHERE c.chart_name = %s
                          AND o.raw_text IS NOT NULL
                          AND length(trim(o.raw_text)) > 0
                        """,
                        (folder_id,),
                    )
                    rows = cur.fetchall()
        except Exception:
            return None

        out: dict[str, dict[str, bool]] = {}
        for page_name, ocr_type in rows:
            out.setdefault(page_name, {})[ocr_type] = True
        return out

    def get_ocr_text(self, folder_id: str, kind: str) -> OcrTextResponse:
        """Assemble OCR from ocr_results into ===== page ===== marker text for the UI."""
        normalized = _normalize_kind(kind)
        ocr_type = KIND_TO_OCR_TYPE[normalized]

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT p.page_name, o.raw_text
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
        for page_name, raw_text in rows:
            body = (raw_text or "").strip()
            if ocr_type == "azuredocintel" and body.startswith("{"):
                try:
                    import json

                    parsed = json.loads(body)
                    if isinstance(parsed, dict) and "content" in parsed:
                        body = str(parsed.get("content") or "")
                except Exception:
                    pass
            chunks.append(f"===== {page_name} =====\n{body}".rstrip())

        return OcrTextResponse(
            folder_id=folder_id,
            kind=normalized,
            text="\n\n".join(chunks),
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

    def _page_imaging_from_db(self, folder_id: str) -> dict[str, dict[str, Any]]:
        """page_name → field dict from quality / BJ / member / DOS tables."""
        by_page: dict[str, dict[str, Any]] = {}

        def _ensure(name: str) -> dict[str, Any]:
            return by_page.setdefault(name, {})

        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    # Quality
                    cur.execute(
                        """
                        SELECT p.page_name, q.printed_or_handwritten, q.hw_confidence,
                               q.orientation_angle, q.tilt_angle, q.mirrored
                        FROM ocr_quality_results q
                        JOIN page_list p ON p.id = q.page_id
                        JOIN chart_list c ON c.id = q.chart_id
                        WHERE c.chart_name = %s
                        """,
                        (folder_id,),
                    )
                    for page_name, hw, conf, orient, tilt, mirrored in cur.fetchall():
                        fields = _ensure(str(page_name))
                        if hw:
                            label = str(hw).strip().lower()
                            fields["handwrittenOrPrinted"] = (
                                "Handwritten" if "hand" in label else "Printed"
                            )
                        if conf is not None:
                            # hw_confidence is the handwriting classifier's own
                            # score. quality_score is deliberately NOT surfaced:
                            # it is a placeholder derived from
                            # printed_or_handwritten, not a measured grade.
                            fields["handwrittenOrPrintedConfidence"] = float(conf)
                            fields["pageQualityConfidence"] = float(conf)
                        if orient is not None:
                            fields["orientationAngle"] = float(orient)
                        if tilt is not None:
                            fields["tiltAngle"] = float(tilt)
                        if mirrored is not None:
                            fields["mirrored"] = bool(mirrored)

                    # Blank/junk — v7 stamps exactly one final row per page,
                    # so precedence is not re-derived here any more.
                    cur.execute(
                        """
                        SELECT p.page_name, b.blank_junk_flag, b.junk_subtype,
                               b.confidence
                        FROM v_page_blank_junk_final b
                        JOIN page_list p ON p.id = b.page_id
                        JOIN chart_list c ON c.id = b.chart_id
                        WHERE c.chart_name = %s
                        """,
                        (folder_id,),
                    )
                    for page_name, flag, subtype, conf in cur.fetchall():
                        fields = _ensure(str(page_name))
                        blank, dup, page_type = _blank_junk_ui(flag, subtype)
                        fields["blankOrJunk"] = blank
                        fields["isDuplicate"] = dup
                        fields["pageType"] = page_type
                        if conf is not None:
                            fields["pageTypeConfidence"] = float(conf)

                    # Member extraction
                    cur.execute(
                        """
                        SELECT p.page_name,
                               m.extracted_name, m.extracted_dob, m.extracted_member_id,
                               m.confidence
                        FROM member_extraction_results m
                        JOIN chart_list c ON c.id = m.chart_id
                        JOIN page_list p ON p.id = m.page_id
                        WHERE c.chart_name = %s
                        """,
                        (folder_id,),
                    )
                    for page_name, name, dob, mid, conf in cur.fetchall():
                        if not page_name:
                            continue
                        fields = _ensure(str(page_name))
                        fields["memberName"] = name
                        fields["memberDob"] = _fmt_date(dob)
                        fields["memberId"] = mid
                        if conf is not None:
                            fields["memberConfidence"] = float(conf)

                    # DOS. There is one schema (schema/schema.sql), so there
                    # is one set of column names — no runtime sniffing.
                    cur.execute(
                        """
                        SELECT p.page_name,
                               d.date_of_service_from, d.date_of_service_to,
                               d.date_of_service_from_doclevel, d.date_of_service_to_doclevel,
                               d.confidence
                        FROM dos_extraction_results d
                        JOIN page_list p ON p.id = d.page_id
                        JOIN chart_list c ON c.id = d.chart_id
                        WHERE c.chart_name = %s
                        """,
                        (folder_id,),
                    )
                    for page_name, d_from, d_to, doc_from, doc_to, conf in cur.fetchall():
                        fields = _ensure(str(page_name))
                        fields["dosFrom"] = _fmt_date(d_from)
                        fields["dosTo"] = _fmt_date(d_to)
                        fields["docDosFrom"] = _fmt_date(doc_from)
                        fields["docDosTo"] = _fmt_date(doc_to)
                        if conf is not None:
                            fields["dosConfidence"] = float(conf)
        except Exception:
            return by_page

        return by_page

    def get_imaging(self, folder_id: str) -> ImagingDocumentResponse:
        """Build imaging panel entirely from Postgres; page skeleton from local files."""
        local = self._require_local()
        folder_dir = local._folder_dir(folder_id)  # noqa: SLF001 — shared path helper
        page_files = local._page_files(folder_dir)  # noqa: SLF001
        pages = empty_imaging_pages(page_files)

        db_fields = self._page_imaging_from_db(folder_id)
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

        manifest = self._manifest_from_db(folder_id) or ImagingManifestDetails()
        verification = self._verification_from_db(folder_id)
        sections = ImagingSectionsProcessed(
            member=any(p.memberName or p.memberId for p in merged),
            dos=any(p.dosFrom or p.dosTo or p.docDosFrom for p in merged),
            hw=any(p.handwrittenOrPrinted for p in merged),
            rotation=any(
                p.orientationAngle is not None or p.tiltAngle is not None for p in merged
            ),
            junk=any(p.blankOrJunk is not None or p.isDuplicate is not None for p in merged),
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
