"""Accuracy rows from v_accuracy_page.

The view joins page_ground_truth to the pipeline page. This module reads
those rows for charts that are on disk and turns them into the slim
documents the screen scores. Nothing is written back.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from pathlib import Path

from app.adapters.postgres.repository import _blank_junk_ui, _fmt_date
from app.core.config import Settings
from app.core.schemas import (
    AccuracyChartPayload,
    AccuracyReportResponse,
    FolderSummary,
    ImagingDocumentResponse,
    ImagingManifestDetails,
    ImagingPageResult,
    ImagingSectionsProcessed,
    PageGroundTruth,
)
from app.services.chart_run_batch import database_url_usable
from app.services.db import connection
from app.services.folder_list import FolderListParams, build_folder_list
from app.services import semantic
from app.services.ground_truth import GroundTruthSource, latest_bulk_labels, spell_codeable
from app.services.imaging_overlays import (
    compose_page_type,
    display_page_type,
    inherit_duplicate_blank_junk,
    signer_fields,
)

_ready: set[tuple[str, str]] = set()
_ready_lock = threading.Lock()

_CODEABLE = {
    "codeable": "Codable",
    "non_codeable": "Non-Codable",
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
    -- "Page Type (Sub-type)", the shape review-ui shows; a pre-taxonomy row
    -- already holds that string in page_subtype.
    (CASE WHEN pc.page_type IS NOT NULL AND pc.page_type <> pc.page_subtype
          THEN pc.page_type || ' (' || pc.page_subtype || ')'
          ELSE COALESCE(pc.page_subtype, pc.page_type) END)::VARCHAR(200) AS page_subtype,
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
    g.updated_at AS ground_truth_updated_at,
    q.orientation_angle,
    EXISTS (SELECT 1 FROM ocr_quality_results qx WHERE qx.chart_id = c.id) AS rotation_known,
    f.page_type AS final_page_type,
    f.page_subtype AS final_page_subtype,
    f.classification_category AS final_category,
    f.dos_from AS final_dos_from,
    f.dos_to AS final_dos_to,
    g.provider_credentials AS gt_provider_credentials,
    -- The signature rows' provider and credentials, as the page view shows them.
    f.provider_name AS final_provider_name,
    to_jsonb(f) ->> 'provider_credentials' AS final_provider_credentials,
    to_jsonb(f) -> 'signers' AS final_signers,
    to_jsonb(f) ->> 'provider_version' AS final_provider_version,
    -- Provider Signature accuracy: did the pipeline find a signature ('true' / 'false';
    -- NULL on a database whose imaging_final has no such column).
    to_jsonb(f) ->> 'signature_present' AS final_signature_present
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
LEFT JOIN ocr_quality_results q ON q.page_id = pl.id
LEFT JOIN imaging_final f ON f.page_id = pl.id
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
       ground_truth_updated_at,
       orientation_angle, rotation_known,
       final_page_type, final_page_subtype, final_category, final_dos_from, final_dos_to,
       gt_provider_credentials, final_provider_name, final_provider_credentials, final_signers,
       final_provider_version, final_signature_present
  FROM v_accuracy_page
 WHERE chart_name = ANY(%s)
 ORDER BY chart_name, page_number
"""


# Duplicate page -> the page it duplicates, so a duplicate scores with its original's
# Blank/Junk tag (as review-ui shows it).
_DUPLICATES = """
SELECT c.chart_name, p.page_name, orig.page_name
  FROM v_page_blank_junk_final b
  JOIN chart_list c ON c.id = b.chart_id
  JOIN page_list p ON p.id = b.page_id
  JOIN page_list orig ON orig.id = b.duplicate_of_page_id
 WHERE b.blank_junk_flag = 'duplicate' AND c.chart_name = ANY(%s)
"""


# Position in a _SELECT row of each ground-truth cell, by page_ground_truth column.
_GT_CELLS = {
    "member_name": 3, "member_dob": 4, "member_id": 5, "dos_from": 6, "dos_to": 7,
    "encounter_type": 8, "page_type": 9, "codeable": 10, "blank_page": 11,
    "junk_page": 12, "is_invoice": 13, "page_sequence": 14, "rotation": 15,
    "is_visible": 16, "rendering_provider": 17, "provider_signature": 18,
    "provider_credentials": 42,
}
_GT_UPDATED_AT = 34


def _with_bulk_labels(rows: list[tuple], bulk: dict) -> list[tuple]:
    """Rows whose ground truth is the page's latest bulk upload. A page with
    no bulk upload drops out: it has only manual labels."""
    out: list[tuple] = []
    for row in rows:
        hit = bulk.get((str(row[0]), int(row[1])))
        if hit is None:
            continue
        labels, loaded_at = hit
        cells = list(row)
        for column, index in _GT_CELLS.items():
            value = labels.get(column)
            cells[index] = None if value is None else str(value)
        cells[_GT_UPDATED_AT] = loaded_at
        out.append(tuple(cells))
    return out


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


# A chart still running has half-written results; it joins the report once done.
_RUNNING = frozenset({"IN_PROGRESS", "IMAGING_IN_PROGRESS"})


def _eligible(settings: Settings) -> list[FolderSummary]:
    """Charts with ground truth that are not running. Local Mode also needs the chart's
    folder in the workspace; Production Mode takes chart_list alone. Each carries its run,
    batch and chart metadata (Source / Measure / Retrieval) for the filters."""
    production = settings.is_production_mode
    data_root = settings.resolved_data_root
    root = data_root if isinstance(data_root, Path) else Path(data_root)
    listed = build_folder_list(
        database_url=settings.database_url,
        db_schema=settings.db_schema,
        data_root=None if production else root,
        full_local_rows=None,
        params=FolderListParams(sort="filename", sort_dir="asc"),
        metadata_dir=None if production else settings.resolved_metadata_root,
    )
    return [
        folder
        for folder in listed.items
        if folder.ground_truth_available
        and (production or folder.on_disk)
        and folder.ocr_status not in _RUNNING
    ]


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _labels(value: str | None) -> list[str]:
    """A ground-truth cell can name several page types, split by ``;`` or ``|``."""
    return [part.strip() for part in re.split(r"[;|]", value or "") if part.strip()]


def _predicted(composed: str | None) -> list[str]:
    """Every name in a composed page type, outermost first:
    ``"Progress Note (Office Visit)"`` -> ``["Progress Note", "Office Visit"]``;
    ``"Lab Report/Result (Radiology) (CT Scan Report)"`` -> the three levels."""
    text = (composed or "").strip()
    head = re.split(r"\s*\(", text, maxsplit=1)[0].strip()
    inner = [part.strip() for part in re.findall(r"\(([^()]+)\)", text)]
    return [name for name in [head, *inner] if name]


def _page_type_semantic(gt_page_type: str | None, encounter_type: str | None,
                        composed: str | None) -> float | None:
    """Best semantic similarity between the ground-truth page type (the
    Encounter Type column, else Page Type) and the predicted type / sub-type."""
    expected = _labels(_text(encounter_type) or _text(gt_page_type))
    actual = _predicted(_text(composed))
    if not expected or not actual:
        return None
    return semantic.best_similarity(expected, actual)


def _signed(value: object) -> str | None:
    """imaging_final.signature_present as Yes / No; None when the database has no value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    folded = str(value).strip().lower()
    if folded in ("true", "t", "1", "yes"):
        return "Yes"
    if folded in ("false", "f", "0", "no"):
        return "No"
    return None


def _float(value: object) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _page(row: tuple, extra: tuple = ()) -> ImagingPageResult:
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
    (
        orientation, _rotation_known,
        final_type, final_subtype, final_category, final_from, final_to,
        gt_provider_credentials, final_provider, final_credentials, final_signers, final_version,
        final_signed,
    ) = (tuple(extra) + (None,) * 13)[:13]
    final_key = _text(final_category) or ""
    people = signer_fields(final_signers, _text(final_provider), final_credentials, final_version)
    provider_name, provider_credentials = people.get("providerName"), people.get("providerCredentials")
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
        pageTypeSemantic=_page_type_semantic(gt_page_type, encounter_type, page_subtype),
        orientationAngle=_float(orientation),
        finalPageType=compose_page_type(_text(final_type), _text(final_subtype)) or None,
        finalCodeable=_CODEABLE.get(final_key, final_key or None) if final_key else None,
        finalDosFrom=_fmt_date(final_from),
        finalDosTo=_fmt_date(final_to),
        providerName=provider_name or None,
        providerCredentials=provider_credentials or None,
        providerSignature=_signed(final_signed),
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
            providerCredentials=_text(gt_provider_credentials),
        ),
    )


# A built report is reused for this long, so reopening the page (or Back from
# a chart) does not re-query Postgres. A ground-truth save here clears it at
# once; a pipeline run elsewhere shows up within the window, or on Refresh.
REPORT_TTL_SEC = 60.0

_reports: dict[tuple[str, str, str], tuple[float, AccuracyReportResponse]] = {}
_reports_lock = threading.Lock()


def clear_report_cache() -> None:
    with _reports_lock:
        _reports.clear()


def load_report(
    settings: Settings, source: GroundTruthSource = "latest", fresh: bool = False
) -> AccuracyReportResponse:
    """The report, from the cache when it is under REPORT_TTL_SEC old and
    ``fresh`` is not asked for."""
    key = (settings.database_url, settings.db_schema, source)
    asked = time.monotonic()
    if not fresh:
        with _reports_lock:
            cached = _reports.get(key)
        if cached and asked - cached[0] < REPORT_TTL_SEC:
            return cached[1]
    # One build at a time per key: a second request (React StrictMode fires the
    # screen's fetch twice in dev) waits and takes the report built meanwhile.
    with _build_lock(key):
        with _reports_lock:
            cached = _reports.get(key)
        if cached and cached[0] >= asked:
            return cached[1]
        report = _build_report(settings, source)
        with _reports_lock:
            _reports[key] = (time.monotonic(), report)
        return report


_build_locks: dict[tuple[str, str, str], threading.Lock] = {}


def _build_lock(key: tuple[str, str, str]) -> threading.Lock:
    with _reports_lock:
        return _build_locks.setdefault(key, threading.Lock())


# Each chart's manifest member: its first row, as the external_member_id column of _SELECT.
_MANIFEST_MEMBERS = """
SELECT DISTINCT ON (mm.record_id) mm.record_id, mm.member_name, mm.member_dob
  FROM manifest_member_list mm
 WHERE mm.record_id = ANY(%s)
 ORDER BY mm.record_id, mm.id
"""


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    text = str(value).strip()
    return text or None


def _build_report(settings: Settings, source: GroundTruthSource) -> AccuracyReportResponse:
    if not database_url_usable(settings.database_url):
        raise RuntimeError("database is not configured")
    eligible = _eligible(settings)
    if not eligible:
        return AccuracyReportResponse(charts=[])
    names = [folder.id for folder in eligible]
    with connection(settings.database_url, settings.db_schema) as conn:
        _ensure(conn, settings.database_url, settings.db_schema)
        with conn.cursor() as cur:
            cur.execute(_SELECT, (names,))
            rows = cur.fetchall()
            cur.execute(_DUPLICATES, (names,))
            duplicates = {(str(chart), str(page)): str(original) for chart, page, original in cur.fetchall()}
            # The manifest member of each chart: member accuracy scores a name or DOB as
            # found only when it is this member's (2026-10-27, user).
            cur.execute(_MANIFEST_MEMBERS, (names,))
            members = {str(chart): (_text(name), _iso(dob)) for chart, name, dob in cur.fetchall()}
        if source == "bulk_upload":
            rows = _with_bulk_labels(
                rows, latest_bulk_labels(conn, settings.database_url, names, settings.db_schema)
            )
    pages: dict[str, list[ImagingPageResult]] = {}
    sections: dict[str, ImagingSectionsProcessed] = {}
    manifests: dict[str, str | None] = {}
    verified: dict[str, datetime | None] = {}
    for row in rows:
        chart_name = str(row[0])
        page = _page(row[:34], row[35:])
        original = duplicates.get((chart_name, page.fileName))
        if original:
            page = page.model_copy(update={"isDuplicate": True, "duplicateOf": original})
        pages.setdefault(chart_name, []).append(page)
        sections[chart_name] = ImagingSectionsProcessed(
            member=bool(row[30]),
            dos=bool(row[31]),
            junk=bool(row[32]),
            codeable=bool(row[33]),
            rotation=bool(row[36]),
        )
        manifests[chart_name] = _text(row[29])
        moment = row[34]
        previous = verified.get(chart_name)
        if isinstance(moment, datetime) and (previous is None or moment > previous):
            verified[chart_name] = moment
    for chart_name, chart_pages in pages.items():
        # Provider credentials come with the row (imaging_final.provider_name), as the page
        # view shows them: no staging, no signature CSV.
        pages[chart_name] = inherit_duplicate_blank_junk(chart_pages)
    charts = [
        AccuracyChartPayload(
            chartId=folder.id,
            chartName=folder.name,
            lastUpdatedAt=folder.last_updated_at,
            lastVerifiedAt=verified.get(folder.id),
            runId=folder.run_id,
            batchId=folder.batch_id,
            projectScope=folder.project_scope,
            sourceName=folder.source_name,
            measure=folder.measure,
            rrMethod=folder.rr_method,
            document=ImagingDocumentResponse(
                folder_id=folder.id,
                manifest=ImagingManifestDetails(
                    member=members.get(folder.id, (None, None))[0],
                    dob=members.get(folder.id, (None, None))[1],
                    memberId=manifests.get(folder.id),
                ),
                pages=pages.get(folder.id, []),
                sectionsProcessed=sections.get(folder.id, ImagingSectionsProcessed()),
            ),
        )
        for folder in eligible
    ]
    return AccuracyReportResponse(charts=charts)
