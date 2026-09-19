"""Stage: member extraction + manifest verification.

Runs the ported V1 engine (``stages/lib/member``) — rule-based extraction over
the whole page, NER escalation only where the rules found nothing, the
wrong-member check, and the what-if Accept/Reject threshold. See
``stages/lib/member/engine.py`` for the algorithm and how it maps to the
reference.

This stage is responsible for the plumbing around that engine:

  * pick the manifest row for the chart (by record_id, so a manifest swept
    before ingest still counts),
  * choose which pages to feed it (blank/junk/duplicate pages are excluded),
  * feed it the best OCR text available per page,
  * persist page rows, the summary, and the two CSVs the review UI reads.

Important: the reject path needs the NER layer. ``wrong_member_on_page`` decides
from the names NER read out of the page's patient-name sentences, so with
``MEMBER_NER_ENABLED=false`` no page can be classified wrong_member and no
document can be Rejected. That is recorded on every row (``ner_enabled``) and in
the summary's decision_reason so a rules-only run is never read as a full one.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from typing import Any, Optional

from config import CORE_ROOT, MEMBER_NER_ENABLED, MEMBER_NER_MODEL_ID
from db import (
    connect,
    get_blank_junk_flags,
    get_ocr_texts,
    get_quality_map,
    list_manifest_members,
    upsert_member_extraction,
    upsert_member_summary,
)
from db.paths import imaging_csv, write_csv
from stages._support import (
    BJ_EXCLUDE,
    best_page_text,
    mark_completed,
    mark_failed,
    mark_processing,
    mark_skipped,
    stage_run,
)

_LIB = CORE_ROOT / "stages" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from member import (  # noqa: E402  (resolved at runtime via _LIB above)
    DETECTION_SOURCE_DB,
    detect_name_mode,
    expected_from_manifest,
    ner_status,
    page_result_to_v1_row,
    summary_status,
    verify_record,
)

logger = logging.getLogger(__name__)

STAGE = "member_verify"

MEMBER_EXTRACT_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "extracted_name",
    "extracted_dob",
    "extracted_member_id",
    "detection_source_name",
    "detection_source_dob",
    "detection_source_member_id",
    "ner_key_source_name",
    "ner_key_source_dob",
    "ner_key_source_member_id",
    "page_status",
    "page_verified",
    "confidence",
    "provided_name",
    "provided_dob",
    "provided_external_member_id",
    "matched",
    "ner_enabled",
]

MEMBER_SUMMARY_COLS = [
    "chart_name",
    "final_status",
    "document_decision",
    "matched_name",
    "name_mode",
    "confidence",
    "pages_checked",
    "pages_matched",
    "wrong_member_pages",
    "reject_threshold",
    "decision_reason",
    "ner_enabled",
]

# V1 reports "N/A" for a field it did not find; the DB stores NULL.
NA = {"", "n/a", "na", "none"}


def _clean(value: Optional[str]) -> Optional[str]:
    text = (value or "").strip()
    return None if text.casefold() in NA else text


def _iso_dob(value: Optional[str]) -> Optional[str]:
    """V1 emits DummyDOB as MM/DD/YYYY; the DB column is DATE."""
    text = _clean(value)
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _page_confidence(page: Any) -> float:
    """A transparent score from how many fields were found and how.

    The reference carried no numeric confidence — it reported the detection
    source per field instead, which is what the review UI actually shows. This
    keeps the column populated without inventing a model score: 0.5 for a
    verified page plus 1/6 per field found, rules weighted above NER.
    """
    score = 0.5 if page.page_verified else 0.1
    for value, source in (
        (page.detected_name, page.detection_source_name),
        (page.detected_dob, page.detection_source_dob),
        (page.detected_member_id, page.detection_source_member_id),
    ):
        if not _clean(value):
            continue
        score += 0.1667 if source == "rule based" else 0.1
    return round(min(score, 0.99), 4)


def _ocr_text_map(conn: Any, chart_id: int) -> dict[int, str]:
    """Best available text per page: final2 → final1 → prelim (restricted).

    Read from ocr_results in three queries rather than re-parsing the combined
    text files once per page, which was O(pages²) file parsing in v6.

    Prelim is never used for handwritten / uncertain / mixed / low-quality
    pages. If final2 is empty (skipped or failed), final1 is used.
    """
    prelim = get_ocr_texts(conn, chart_id, "tesseract")
    final1 = get_ocr_texts(conn, chart_id, "docling")
    final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
    quality = get_quality_map(conn, chart_id)

    merged: dict[int, str] = {}
    for page_id in set(prelim) | set(final1) | set(final2) | set(quality):
        merged[page_id] = best_page_text(
            final2=final2.get(page_id),
            final1=final1.get(page_id),
            prelim=prelim.get(page_id),
            quality_row=quality.get(page_id),
        )
    return merged


def _pick_manifest_row(rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The manifest row to verify against.

    A batch file carries one row per chart, so normally there is exactly one.
    When a chart has several candidates, prefer the most complete one — a row
    with a MemberID and a DOB gives the rules the most to match on.
    """
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]

    def completeness(row: dict[str, Any]) -> tuple[int, int, int, int]:
        return (
            1 if (row.get("external_member_id") or "").strip() else 0,
            1 if row.get("member_dob") else 0,
            1 if (row.get("middle_name") or "").strip() else 0,
            -int(row.get("id") or 0),
        )

    return max(rows, key=completeness)


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    # Say up front what this run can and cannot conclude. Without the NER layer
    # no page can be classified wrong_member, so no document can be Rejected.
    ner = ner_status()
    if ner["ready"]:
        logger.info("member: NER layer active (%s)", ", ".join(ner["enabled_models"]))
    else:
        logger.warning(
            "member: NER layer INACTIVE (%s) — rules-only. No page can be marked "
            "wrong_member, so this chart cannot be Rejected on member evidence.",
            ner["reason"],
        )
    # Honour that warning. Gating on MEMBER_NER_ENABLED alone let the flag be
    # true while the checkpoints were absent, so the engine called NER anyway
    # and ModelLoadError killed the whole chart — after five stages of work —
    # having just logged that it would run rules-only.
    ner_model_id = MEMBER_NER_MODEL_ID if ner["ready"] else None

    with stage_run(chart_id, STAGE, force=force) as ctx:
        chart_name = ctx.chart_name

        with connect() as conn:
            manifest_rows = list_manifest_members(conn, chart_id=chart_id)
            bj_flags = get_blank_junk_flags(conn, chart_id, final_only=True)
            texts = _ocr_text_map(conn, chart_id)

        manifest = _pick_manifest_row(manifest_rows)
        if manifest is None:
            # No roster to verify against. The pages are not "done" — they are
            # waiting on a manifest sweep — so they stay pending and the chart
            # reports needs_review rather than silently completing.
            logger.warning(
                "chart %s: no manifest rows for record_id=%s; member stage cannot run",
                chart_id, chart_name,
            )
            with connect() as conn:
                upsert_member_summary(
                    conn,
                    chart_id=chart_id,
                    final_status="needs_review",
                    document_decision=None,
                    matched_member_list_id=None,
                    matched_name=None,
                    name_mode=None,
                    confidence=None,
                    pages_checked=0,
                    pages_matched=0,
                    wrong_member_pages=0,
                    reject_threshold=None,
                    decision_reason="manifest_missing",
                )
            write_csv(
                imaging_csv(chart_name, "member_verification"),
                MEMBER_SUMMARY_COLS,
                [
                    {
                        "chart_name": chart_name,
                        "final_status": "needs_review",
                        "decision_reason": "manifest_missing",
                        "ner_enabled": MEMBER_NER_ENABLED,
                    }
                ],
            )
            return {
                "chart_id": chart_id,
                "status": "needs_review",
                "reason": "manifest_missing",
                "pages_checked": 0,
            }

        expected = expected_from_manifest(manifest)
        name_mode = detect_name_mode(expected)
        if not name_mode:
            logger.warning(
                "chart %s: manifest row %s has no usable first/last name; "
                "every page will fail verification",
                chart_id, manifest.get("id"),
            )

        # Blank / junk / duplicate pages carry no member details worth reading.
        page_map = ctx.page_map()
        excluded = [
            pid for pid in ctx.todo
            if bj_flags.get(pid, "not_blank_junk") in BJ_EXCLUDE
        ]
        with connect() as conn:
            mark_skipped(conn, ctx, excluded, "blank_junk")

        todo_pages = [p for p in ctx.pages if p["id"] in ctx.todo]
        engine_pages = [
            {
                "page_no": p.get("page_number") or 0,
                "page_name": p["page_name"],
                "text": texts.get(p["id"], ""),
                "_page_id": p["id"],
            }
            for p in todo_pages
        ]

        with connect() as conn:
            for page in todo_pages:
                mark_processing(conn, ctx, page["id"])

        # The reject threshold is a proportion of the whole document, so the
        # engine gets the chart's real page count, not just the subset here.
        result = verify_record(
            record_id=chart_name,
            pages=engine_pages,
            expected=expected,
            name_mode=name_mode,
            model_id=ner_model_id,
            total_pages=len(ctx.pages),
        )

        by_page_no = {p["page_no"]: p["_page_id"] for p in engine_pages}
        provided_dob = manifest.get("member_dob")
        if isinstance(provided_dob, date):
            provided_dob = provided_dob.isoformat()

        csv_rows: list[dict[str, Any]] = []
        with connect() as conn:
            for page in result.pages:
                page_id = by_page_no.get(page.page_no)
                if page_id is None:
                    continue
                row = page_map.get(page_id, {})
                try:
                    upsert_member_extraction(
                        conn,
                        chart_id=chart_id,
                        page_id=page_id,
                        extracted_name=_clean(page.detected_name),
                        extracted_dob=_iso_dob(page.detected_dob),
                        extracted_member_id=_clean(page.detected_member_id),
                        detection_source_name=DETECTION_SOURCE_DB.get(
                            page.detection_source_name, ""
                        ),
                        detection_source_dob=DETECTION_SOURCE_DB.get(
                            page.detection_source_dob, ""
                        ),
                        detection_source_member_id=DETECTION_SOURCE_DB.get(
                            page.detection_source_member_id, ""
                        ),
                        ner_key_source_name=_clean(page.ner_key_source_name),
                        ner_key_source_dob=_clean(page.ner_key_source_dob),
                        ner_key_source_member_id=_clean(page.ner_key_source_member_id),
                        page_status=page.db_page_status,
                        page_verified=page.page_verified,
                        confidence=_page_confidence(page),
                        provided_name=manifest.get("member_name"),
                        provided_dob=provided_dob,
                        provided_external_member_id=manifest.get("external_member_id"),
                        matched_member_list_id=(
                            manifest.get("id") if page.page_verified else None
                        ),
                    )
                    mark_completed(conn, ctx, page_id)
                except Exception as exc:  # one bad page must not sink the chart
                    logger.exception("member persist failed for %s", page.page_name)
                    mark_failed(conn, ctx, page_id, str(exc), page.page_name)
                    continue

                csv_rows.append(
                    {
                        "chart_name": chart_name,
                        "page_name": page.page_name,
                        "page_number": row.get("page_number") or page.page_no,
                        "extracted_name": _clean(page.detected_name) or "",
                        "extracted_dob": _clean(page.detected_dob) or "",
                        "extracted_member_id": _clean(page.detected_member_id) or "",
                        "detection_source_name": page.detection_source_name,
                        "detection_source_dob": page.detection_source_dob,
                        "detection_source_member_id": page.detection_source_member_id,
                        "ner_key_source_name": page.ner_key_source_name,
                        "ner_key_source_dob": page.ner_key_source_dob,
                        "ner_key_source_member_id": page.ner_key_source_member_id,
                        "page_status": page.db_page_status,
                        "page_verified": page.page_verified,
                        "confidence": _page_confidence(page),
                        "provided_name": manifest.get("member_name") or "",
                        "provided_dob": provided_dob or "",
                        "provided_external_member_id": manifest.get("external_member_id") or "",
                        "matched": page.page_verified,
                        "ner_enabled": result.ner_enabled,
                    }
                )

        final_status, reason = summary_status(result)
        if not result.ner_enabled and reason not in {"manifest_name_incomplete"}:
            # Make the limitation legible wherever the decision is read.
            reason = f"{reason}|ner_disabled"

        with connect() as conn:
            upsert_member_summary(
                conn,
                chart_id=chart_id,
                final_status=final_status,
                document_decision=result.db_document_decision,
                matched_member_list_id=manifest.get("id"),
                matched_name=manifest.get("member_name"),
                name_mode=result.name_mode or None,
                confidence=(
                    round(result.pages_verified / result.pages_checked, 4)
                    if result.pages_checked
                    else None
                ),
                pages_checked=result.pages_checked,
                pages_matched=result.pages_verified,
                wrong_member_pages=result.pages_wrong_member,
                reject_threshold=result.reject_threshold,
                decision_reason=reason[:50],
            )

        write_csv(imaging_csv(chart_name, "member_extraction"), MEMBER_EXTRACT_COLS, csv_rows)
        write_csv(
            imaging_csv(chart_name, "member_verification"),
            MEMBER_SUMMARY_COLS,
            [
                {
                    "chart_name": chart_name,
                    "final_status": final_status,
                    "document_decision": result.db_document_decision,
                    "matched_name": manifest.get("member_name") or "",
                    "name_mode": result.name_mode,
                    "confidence": (
                        round(result.pages_verified / result.pages_checked, 4)
                        if result.pages_checked
                        else ""
                    ),
                    "pages_checked": result.pages_checked,
                    "pages_matched": result.pages_verified,
                    "wrong_member_pages": result.pages_wrong_member,
                    "reject_threshold": result.reject_threshold,
                    "decision_reason": reason,
                    "ner_enabled": result.ner_enabled,
                }
            ],
        )

        # The reference's own CSV shape, for diffing a port against a V1 run.
        write_csv(
            imaging_csv(chart_name, "member_v1_compare"),
            [
                "RecordId", "Total_Page_Count", "Page_No",
                "Detection_Source_Name", "ner_key_source_Name", "Detected_Full_Name",
                "Detection_Source_DOB", "ner_key_source_DOB", "Detected_DOB",
                "Detection_Source_MemberID", "ner_key_source_MemberID", "Detected_MemberID",
                "Page_Verified", "Document_Verified",
                "Page_Detection_Correct", "Page_Detection_InCorrect",
            ],
            [page_result_to_v1_row(result, p) for p in result.pages],
        )

        return {
            "chart_id": chart_id,
            "final_status": final_status,
            "document_decision": result.db_document_decision,
            "name_mode": result.name_mode,
            "pages_checked": result.pages_checked,
            "pages_verified": result.pages_verified,
            "pages_wrong_member": result.pages_wrong_member,
            "reject_threshold": result.reject_threshold,
            "ner_enabled": result.ner_enabled,
            "decision_reason": reason,
        }
