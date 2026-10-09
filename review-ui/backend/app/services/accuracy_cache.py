"""Accuracy rows from v_accuracy_page.

The view joins page_ground_truth to the pipeline page. This module reads
those rows for charts that are on disk and turns them into the slim
documents the screen scores. Nothing is written back.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from app.adapters.postgres.repository import _blank_junk_ui, _fmt_date
from app.core.config import Settings
from app.core.schemas import (
    AccuracyChartPayload,
    AccuracyReportResponse,
    ImagingDocumentResponse,
    ImagingManifestDetails,
    ImagingPageResult,
    ImagingSectionsProcessed,
    PageGroundTruth,
)
from app.services.chart_run_batch import database_url_usable
from app.services.db import connection
from app.services.folder_list import FolderListParams, build_folder_list
from app.services.ground_truth import spell_codeable
from app.services.imaging_overlays import display_page_type

_ready: set[tuple[str, str]] = set()
_ready_lock = threading.Lock()

_CODEABLE = {
    "codeable": "Codeable",
    "non_codeable": "Non Codeable",
    "discharge_summary": "Discharge",
    "discharge_frequency": "Discharge",
    "not_sure": "Not Sure",
}

# Kept in step with the statement at the bottom of schema/v1.sql.
_VIEW = """
CREATE OR REPLACE VIEW v_accuracy_page AS
SELECT
    g.chart_name,
    g.page_number,
    g.source_page_id,
    c.updated_at AS chart_updated_at,
    g.member_name,
    g.member_dob,
    g.member_id,
    g.dos_from AS gt_dos_from,
    g.dos_to AS gt_dos_to,
    g.encounter_type,
    g.page_type AS gt_page_type,
    g.codeable AS gt_codeable,
    g.blank_page,
    g.junk_page,
    g.is_invoice,
    g.page_sequence,
    g.rotation,
    g.is_visible,
    g.rendering_provider,
    g.provider_signature,
    pl.page_name,
    m.extracted_name,
    m.extracted_dob,
    m.extracted_member_id,
    d.date_of_service_from,
    d.date_of_service_to,
    b.blank_junk_flag,
    b.junk_subtype,
    pc.page_subtype,
    pc.classification_category,
    (
        SELECT mm.external_member_id
          FROM manifest_member_list mm
         WHERE mm.record_id = g.chart_name
         ORDER BY mm.id
         LIMIT 1
    ) AS external_member_id,
    (
        EXISTS (SELECT 1 FROM member_extraction_results mx WHERE mx.chart_id = c.id)
        OR EXISTS (SELECT 1 FROM member_verification_summary vx WHERE vx.chart_id = c.id)
    ) AS member_known,
    EXISTS (SELECT 1 FROM dos_extraction_results dx WHERE dx.chart_id = c.id) AS dos_known,
    EXISTS (SELECT 1 FROM blank_junk_classification bx WHERE bx.chart_id = c.id) AS junk_known,
    EXISTS (SELECT 1 FROM page_classification px WHERE px.chart_id = c.id) AS codeable_known,
    g.updated_at AS ground_truth_updated_at
FROM page_ground_truth g
LEFT JOIN chart_list c
       ON c.chart_name = g.chart_name
LEFT JOIN LATERAL (
    SELECT p.id, p.page_name
      FROM page_list p
     WHERE p.chart_id = c.id
       AND (
            p.page_name = g.source_page_id
         OR split_part(p.page_name, '.', 1) = g.page_number::text
       )
     ORDER BY (p.page_name IS NOT DISTINCT FROM g.source_page_id) DESC, p.id
     LIMIT 1
) pl ON TRUE
LEFT JOIN member_extraction_results m ON m.page_id = pl.id
LEFT JOIN dos_extraction_results d ON d.page_id = pl.id
LEFT JOIN v_page_blank_junk_final b ON b.page_id = pl.id
LEFT JOIN page_classification pc ON pc.page_id = pl.id
"""

_SELECT = """
SELECT chart_name, page_number, source_page_id,
       member_name, member_dob, member_id,
       gt_dos_from, gt_dos_to, encounter_type, gt_page_type, gt_codeable,
       blank_page, junk_page, is_invoice, page_sequence, rotation,
       is_visible, rendering_provider, provider_signature,
       page_name, extracted_name, extracted_dob, extracted_member_id,
       date_of_service_from, date_of_service_to,
       blank_junk_flag, junk_subtype, page_subtype, classification_category,
       external_member_id, member_known, dos_known, junk_known, codeable_known,
       ground_truth_updated_at
  FROM v_accuracy_page
 WHERE chart_name = ANY(%s)
 ORDER BY chart_name, page_number
"""


def _ensure(conn, database_url: str, db_schema: str) -> None:
    key = (database_url, db_schema)
    if key in _ready:
        return
    with _ready_lock:
        if key in _ready:
            return
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS accuracy_snapshot")
            cur.execute(_VIEW)
        _ready.add(key)


def _eligible(settings: Settings) -> list[tuple[str, str, datetime | None]]:
    data_root = settings.resolved_data_root
    root = data_root if isinstance(data_root, Path) else Path(data_root)
    listed = build_folder_list(
        database_url=settings.database_url,
        db_schema=settings.db_schema,
        data_root=root,
        full_local_rows=None,
        params=FolderListParams(sort="filename", sort_dir="asc"),
    )
    return [
        (folder.id, folder.name, folder.last_updated_at)
        for folder in listed.items
        if folder.ground_truth_available and folder.on_disk
    ]


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _page(row: tuple) -> ImagingPageResult:
    (
        _chart, page_number, source_page_id,
        member_name, member_dob, member_id,
        gt_dos_from, gt_dos_to, encounter_type, gt_page_type, gt_codeable,
        blank_page, junk_page, is_invoice, page_sequence, rotation,
        is_visible, rendering_provider, provider_signature,
        page_name, extracted_name, extracted_dob, extracted_member_id,
        dos_from, dos_to,
        blank_flag, junk_subtype, page_subtype, category,
        _manifest_id, _member_known, _dos_known, _junk_known, _codeable_known,
    ) = row
    file_name = _text(page_name) or _text(source_page_id) or f"{page_number}.jpg"
    blank, _dup, junk_type = _blank_junk_ui(
        _text(blank_flag), _text(junk_subtype)
    )
    subtype = _text(page_subtype)
    page_type = display_page_type(subtype) if subtype else junk_type
    key = _text(category) or ""
    is_codeable = _CODEABLE.get(key, key or None) if key or subtype else None
    return ImagingPageResult(
        pageNumber=int(page_number),
        fileName=file_name,
        memberName=_text(extracted_name),
        memberDob=_fmt_date(extracted_dob),
        memberId=_text(extracted_member_id),
        dosFrom=_fmt_date(dos_from),
        dosTo=_fmt_date(dos_to),
        blankOrJunk=blank,
        pageType=page_type,
        isCodeable=is_codeable,
        groundTruth=PageGroundTruth(
            pageNumber=int(page_number),
            sourcePageId=_text(source_page_id),
            memberName=_text(member_name),
            memberDob=_text(member_dob),
            memberId=_text(member_id),
            dosFrom=_text(gt_dos_from),
            dosTo=_text(gt_dos_to),
            encounterType=_text(encounter_type),
            pageType=_text(gt_page_type),
            codeable=spell_codeable(_text(gt_codeable)),
            blankPage=_text(blank_page),
            junkPage=_text(junk_page),
            isInvoice=_text(is_invoice),
            pageSequence=_text(page_sequence),
            rotation=_text(rotation),
            isVisible=_text(is_visible),
            renderingProvider=_text(rendering_provider),
            providerSignature=_text(provider_signature),
        ),
    )


def _with_signatures(
    pages: list[ImagingPageResult], data_root: Path, chart_name: str
) -> list[ImagingPageResult]:
    """Yes/No signature from the chart CSV, so the accuracy donut can score it."""
    from app.services.imaging_overlays import (
        collect_rows,
        index_signature_rows,
        overlay_fields,
    )

    try:
        rows = collect_rows(
            folder_dir=data_root / chart_name,
            data_root=data_root,
            per_chart_name=f"{chart_name}_provider_signature.csv",
            combined_rel=None,
            chart_name=chart_name,
        )
    except OSError:
        return pages
    return overlay_fields(pages, index_signature_rows(rows, chart_name))


def load_report(settings: Settings) -> AccuracyReportResponse:
    if not database_url_usable(settings.database_url):
        raise RuntimeError("database is not configured")
    eligible = _eligible(settings)
    if not eligible:
        return AccuracyReportResponse(charts=[])
    names = [chart_id for chart_id, _name, _updated in eligible]
    with connection(settings.database_url, settings.db_schema) as conn:
        _ensure(conn, settings.database_url, settings.db_schema)
        with conn.cursor() as cur:
            cur.execute(_SELECT, (names,))
            rows = cur.fetchall()
    pages: dict[str, list[ImagingPageResult]] = {}
    sections: dict[str, ImagingSectionsProcessed] = {}
    manifests: dict[str, str | None] = {}
    verified: dict[str, datetime | None] = {}
    for row in rows:
        chart_name = str(row[0])
        pages.setdefault(chart_name, []).append(_page(row[:34]))
        sections[chart_name] = ImagingSectionsProcessed(
            member=bool(row[30]),
            dos=bool(row[31]),
            junk=bool(row[32]),
            codeable=bool(row[33]),
        )
        manifests[chart_name] = _text(row[29])
        moment = row[34]
        previous = verified.get(chart_name)
        if isinstance(moment, datetime) and (previous is None or moment > previous):
            verified[chart_name] = moment
    data_root = settings.resolved_data_root
    root = data_root if isinstance(data_root, Path) else Path(data_root)
    for chart_name, chart_pages in pages.items():
        pages[chart_name] = _with_signatures(chart_pages, root, chart_name)
    charts = [
        AccuracyChartPayload(
            chartId=chart_id,
            chartName=chart_name,
            lastUpdatedAt=updated,
            lastVerifiedAt=verified.get(chart_id),
            document=ImagingDocumentResponse(
                folder_id=chart_id,
                manifest=ImagingManifestDetails(memberId=manifests.get(chart_id)),
                pages=pages.get(chart_id, []),
                sectionsProcessed=sections.get(chart_id, ImagingSectionsProcessed()),
            ),
        )
        for chart_id, chart_name, updated in eligible
    ]
    return AccuracyReportResponse(charts=charts)
