"""Stage: member extraction + manifest verification.

Runs the ported V1 engine (``modules/member``) — rule-based extraction over
the whole page, NER escalation only where the rules found nothing, the
wrong-member check, and the what-if Accept/Reject threshold. See
``modules/member/engine.py`` for the algorithm and how it maps to the
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

Confidence (2026-11-14, docs/specials/CONFIDENCE_LOGIC.md section 12): name, DOB and member
ID each have their own (``member_field_confidences``: the KV row's model score, backup or
flipped); the page column keeps the lowest; the chart value is an agreement rate.
"""
from __future__ import annotations

import logging
import re
import sys
from datetime import date, datetime
from typing import Any, Optional

from common.config import MEMBER_NER_ENABLED, MEMBER_NER_MODEL_ID
from common.db import (
    connect,
    get_blank_junk_flags,
    get_ocr_texts,
    get_quality_map,
    list_manifest_members,
    upsert_member_extraction,
    upsert_member_summary,
)
from common.db.paths import imaging_csv, write_csv
from modules import confidence as conf
from apps.worker.stages._support import (
    BJ_EXCLUDE,
    best_page_text,
    mark_completed,
    mark_failed,
    mark_processing,
    mark_skipped,
    stage_run,
)

# Imported by its package name only. A second name (a bare ``member`` via a
# sys.path insert) would load the engine twice with separate module state.
from modules.member import (
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


MEMBER_FIELDS = (
    # (field id in the extraction staging, PageResult / DB value, source, key source)
    ("name", "name", "detected_name", "detection_source_name", "ner_key_source_name"),
    ("dob", "dob", "detected_dob", "detection_source_dob", "ner_key_source_dob"),
    ("member_id", "member_id", "detected_member_id", "detection_source_member_id", "ner_key_source_member_id"),
)


def _norm(field_id: str, value: Any) -> str:
    text = str(value or "").strip()
    if field_id == "dob":
        from modules.extraction.util.dates import canonical_date

        return canonical_date(text) if text else ""
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _field_row(field_id: str, value: str, staged_page: Any) -> Optional[dict[str, Any]]:
    """The staged extraction row the value was taken from: same value (a name: the value
    contains it), the selected / accepted rows first. None when no row reads it (the rules'
    whole-page find)."""
    if staged_page is None:
        return None
    want = _norm(field_id, value)
    if not want:
        return None
    rows = sorted(
        (row for row in staged_page.rows(field_id) if isinstance(row, dict) and str(row.get("value") or "").strip()),
        key=lambda row: (not row.get("selected"), not row.get("accepted")),
    )
    for row in rows:
        got = _norm(field_id, row.get("value"))
        if got == want or (field_id == "name" and want in got):
            return row
    return None


def _field_confidence(field_id: str, value: Any, source: Any, key_source: Any, staged_page: Any) -> Optional[float]:
    """One member identifier's confidence (2026-11-14, user; CONFIDENCE_LOGIC.md 12.2):
    the KV model_score of the extraction row it came from (``row_confidence``: rules only
    -> backup, model vs rules -> flipped; a row the model turned down -> flipped). Read by
    NER, by the manifest text search ("sentence: ..."), or by the rules with no staged row:
    backup. Nothing read: None."""
    value = _clean(value)
    if not value:
        return None
    source = str(source or "").replace("_", " ").strip()
    if source == "ner" or str(key_source or "").startswith("sentence:"):
        return conf.backup()
    row = _field_row(field_id, value, staged_page)
    if row is None:
        return conf.backup()
    from modules.extraction.results import row_confidence

    if row.get("model_score") not in (None, "") and not row.get("accepted", True):
        return conf.flipped()  # used although the model turned it down
    found = row_confidence(row)
    return conf.backup() if found is None else found


def member_field_confidences(page: Any, staged_page: Any = None) -> dict[str, Optional[float]]:
    """``{"name", "dob", "member_id"}`` -> each identifier's own confidence
    (``_field_confidence``). ``page`` is a PageResult or a member_extraction_results row
    (``extracted_*`` / ``detection_source_*`` keys)."""
    def get(obj: Any, *names: str) -> Any:
        for name in names:
            found = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
            if found not in (None, ""):
                return found
        return None

    out: dict[str, Optional[float]] = {}
    for field_id, short, detected, source, key_source in MEMBER_FIELDS:
        value = get(page, detected, f"extracted_{short}")
        if isinstance(value, date):
            value = value.isoformat()
        out[field_id] = _field_confidence(field_id, value, get(page, source), get(page, key_source), staged_page)
    return out


def _page_confidence(page: Any, staged_page: Any = None) -> Optional[float]:
    """``member_extraction_results.confidence`` (2026-11-14): the lowest of the page's name,
    DOB and member ID confidences (``member_field_confidences``), so the column keeps one
    number; None when nothing was read. Final keeps the three apart
    (``imaging_final.confidences.member_name`` / ``member_dob`` / ``member_id``)."""
    return conf.weakest(member_field_confidences(page, staged_page).values())


def _agreement_rate(result: Any, status: str) -> Optional[float]:
    """The chart's member decision as an agreement rate (2026-11-14, user: not a
    confidence): pages that agree with the decision (wrong-member pages for failed,
    verified pages otherwise) / pages checked. The same number in the database
    (``member_verification_summary.confidence``) and the CSV. None when nothing was checked."""
    if not result.pages_checked:
        return None
    agreeing = "wrong_member" if status == "failed" else "verified"
    backers = sum(1 for p in result.pages if p.db_page_status == agreeing)
    return round(backers / result.pages_checked, 4)


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


# 2026-11-04 (user): pages whose name, ID and DOB are read but never verified.
EXTRACT_ONLY_FLAGS = frozenset({"blank", "junk"})


def _extract_only_pages(
    chart_id: int, chart_name: str, ctx: Any, pages: list[dict[str, Any]], texts: dict[int, str],
    staged: Any, expected: Any, name_mode: Any, ner_model_id: Any, manifest: dict[str, Any],
    provided_dob: Any, page_map: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Blank and junk pages: the member name, ID and DOB as read, stored for display with
    page_status 'not_verified' and no verification (page_verified NULL, no other names, no
    matched member). The chart's decision is made on the other pages alone."""
    if not pages:
        return []
    engine_pages = [
        {"page_no": p.get("page_number") or 0, "page_name": p["page_name"], "text": texts.get(p["id"], ""),
         "staged": staged.page(p["page_name"]), "_page_id": p["id"]}
        for p in pages
    ]
    try:
        result = verify_record(record_id=chart_name, pages=engine_pages, expected=expected,
                               name_mode=name_mode, model_id=ner_model_id, total_pages=len(ctx.pages))
    except Exception:  # display only: never sinks the chart
        logger.exception("chart %s: member read on blank / junk pages failed", chart_name)
        with connect() as conn:
            mark_skipped(conn, ctx, [p["id"] for p in pages], "blank_junk")
        return []
    by_page_no = {p["page_no"]: p["_page_id"] for p in engine_pages}
    rows: list[dict[str, Any]] = []
    with connect() as conn:
        for page in result.pages:
            page_id = by_page_no.get(page.page_no)
            if page_id is None:
                continue
            try:
                upsert_member_extraction(
                    conn, chart_id=chart_id, page_id=page_id,
                    extracted_name=_clean(page.detected_name),
                    extracted_dob=_iso_dob(page.detected_dob),
                    extracted_member_id=_clean(page.detected_member_id),
                    detection_source_name=DETECTION_SOURCE_DB.get(page.detection_source_name, ""),
                    detection_source_dob=DETECTION_SOURCE_DB.get(page.detection_source_dob, ""),
                    detection_source_member_id=DETECTION_SOURCE_DB.get(page.detection_source_member_id, ""),
                    ner_key_source_name=_clean(page.ner_key_source_name),
                    ner_key_source_dob=_clean(page.ner_key_source_dob),
                    ner_key_source_member_id=_clean(page.ner_key_source_member_id),
                    page_status="not_verified", page_verified=None,
                    confidence=_page_confidence(page, staged.page(page.page_name)),
                    provided_name=manifest.get("member_name"), provided_dob=provided_dob,
                    provided_external_member_id=manifest.get("external_member_id"),
                    other_names=None, matched_member_list_id=None,
                )
                mark_completed(conn, ctx, page_id)
            except Exception as exc:  # one bad page must not sink the chart
                logger.exception("member read failed for %s", page.page_name)
                mark_failed(conn, ctx, page_id, str(exc), page.page_name)
                continue
            row = page_map.get(page_id, {})
            rows.append({
                "chart_name": chart_name, "page_name": page.page_name,
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
                "page_status": "not_verified (blank / junk)", "page_verified": "",
                "confidence": _page_confidence(page, staged.page(page.page_name)),
                "provided_name": manifest.get("member_name") or "", "provided_dob": provided_dob or "",
                "provided_external_member_id": manifest.get("external_member_id") or "",
                "matched": "", "ner_enabled": result.ner_enabled,
            })
    logger.info("chart %s: member name / ID / DOB read on %d blank / junk page(s), not verified",
                chart_name, len(rows))
    return rows


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

        # Every page blank/junk/duplicate → nothing to verify; skip and complete.
        # A chart of only blank / junk pages still has their member details read (2026-11-04,
        # user) further down; only a chart of nothing but duplicates stops here.
        all_blank_junk = bool(ctx.pages) and all(
            bj_flags.get(p["id"], "not_blank_junk") in BJ_EXCLUDE - EXTRACT_ONLY_FLAGS for p in ctx.pages
        )
        if all_blank_junk:
            logger.info(
                "chart %s: all %d page(s) blank/junk/duplicate — member stage skipped",
                chart_id,
                len(ctx.pages),
            )
            with connect() as conn:
                mark_skipped(conn, ctx, sorted(ctx.todo), "blank_junk")
                # final_status is triage (verified|failed|needs_review|skipped),
                # not chart_list.status. All-junk → skipped; chart still completes.
                upsert_member_summary(
                    conn,
                    chart_id=chart_id,
                    final_status="skipped",
                    document_decision=None,
                    matched_member_list_id=None,
                    matched_name=None,
                    name_mode=None,
                    confidence=None,
                    pages_checked=0,
                    pages_matched=0,
                    wrong_member_pages=0,
                    reject_threshold=None,
                    decision_reason="all_blank_junk",
                )
            write_csv(
                imaging_csv(chart_name, "member_extraction"),
                MEMBER_EXTRACT_COLS,
                [],
            )
            write_csv(
                imaging_csv(chart_name, "member_verification"),
                MEMBER_SUMMARY_COLS,
                [
                    {
                        "chart_name": chart_name,
                        "final_status": "skipped",
                        "decision_reason": "all_blank_junk",
                        "ner_enabled": MEMBER_NER_ENABLED,
                        "pages_checked": 0,
                        "pages_matched": 0,
                    }
                ],
            )
            return {
                "chart_id": chart_id,
                "status": "completed",
                "reason": "all_blank_junk",
                "pages_checked": 0,
                "skipped": ctx.skipped,
            }

        # Manifest rows come only from manifest_member_list, loaded by db-utilities (2026-11-07).
        manifest = _pick_manifest_row(manifest_rows)
        if manifest is None:
            # No roster to verify against. Blank/junk pages are skipped as usual; the
            # other pages are skipped as manifest_missing (2026-11-10, user: was left
            # pending, so the chart never completed and showed Imaging in Progress).
            # Loading the manifest (db-utilities) and re-running member_verify fills it in.
            logger.warning(
                "chart %s: no manifest rows for record_id=%s; member stage skipped (manifest_missing)",
                chart_id, chart_name,
            )
            excluded = [
                pid
                for pid in ctx.todo
                if bj_flags.get(pid, "not_blank_junk") in BJ_EXCLUDE
            ]
            no_roster = [pid for pid in ctx.todo if pid not in set(excluded)]
            with connect() as conn:
                mark_skipped(conn, ctx, excluded, "blank_junk")
                mark_skipped(conn, ctx, no_roster, "manifest_missing")
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
                "skipped": ctx.skipped,
            }

        expected = expected_from_manifest(manifest)
        name_mode = detect_name_mode(expected)
        if not name_mode:
            logger.warning(
                "chart %s: manifest row %s has no usable first/last name; "
                "every page will fail verification",
                chart_id, manifest.get("id"),
            )

        # Blank / junk / duplicate pages never take part in verification. Since 2026-11-04
        # (user) the member name, ID and DOB are still read on blank and junk pages, for
        # display only (EXTRACT_ONLY_FLAGS); a duplicate takes its original's values.
        page_map = ctx.page_map()
        extract_only = [
            p for p in ctx.pages
            if p["id"] in ctx.todo and bj_flags.get(p["id"], "not_blank_junk") in EXTRACT_ONLY_FLAGS
        ]
        extract_only_ids = {p["id"] for p in extract_only}
        excluded = [
            pid for pid in ctx.todo
            if bj_flags.get(pid, "not_blank_junk") in BJ_EXCLUDE and pid not in extract_only_ids
        ]
        with connect() as conn:
            mark_skipped(conn, ctx, excluded, "blank_junk")

        todo_pages = [p for p in ctx.pages if p["id"] in ctx.todo and p["id"] not in extract_only_ids]
        if not todo_pages:
            # Nothing left to verify. An empty page list is Reject in the
            # what-if rules, which stored final_status=failed for a chart
            # whose only page was already blank/junk.
            logger.info(
                "chart %s: no pages left to verify — member stage skipped",
                chart_id,
            )
            if extract_only:
                # Still read the member details on the blank / junk pages (2026-11-04, user).
                from modules.extraction.stage import ensure_staging

                dob = manifest.get("member_dob")
                rows = _extract_only_pages(
                    chart_id, chart_name, ctx, extract_only, texts, ensure_staging(chart_id, chart_name),
                    expected, name_mode, ner_model_id, manifest,
                    dob.isoformat() if isinstance(dob, date) else dob, page_map,
                )
                write_csv(imaging_csv(chart_name, "member_extraction"), MEMBER_EXTRACT_COLS, rows)
            with connect() as conn:
                upsert_member_summary(
                    conn,
                    chart_id=chart_id,
                    final_status="skipped",
                    document_decision=None,
                    matched_member_list_id=None,
                    matched_name=None,
                    name_mode=None,
                    confidence=None,
                    pages_checked=0,
                    pages_matched=0,
                    wrong_member_pages=0,
                    reject_threshold=None,
                    decision_reason="all_blank_junk",
                )
            write_csv(
                imaging_csv(chart_name, "member_verification"),
                MEMBER_SUMMARY_COLS,
                [
                    {
                        "chart_name": chart_name,
                        "final_status": "skipped",
                        "decision_reason": "all_blank_junk",
                        "ner_enabled": MEMBER_NER_ENABLED,
                        "pages_checked": 0,
                        "pages_matched": 0,
                    }
                ],
            )
            return {
                "chart_id": chart_id,
                "status": "completed",
                "reason": "all_blank_junk",
                "pages_checked": 0,
                "skipped": ctx.skipped,
            }

        from modules.extraction.stage import ensure_staging

        staged = ensure_staging(chart_id, chart_name)
        engine_pages = [
            {
                "page_no": p.get("page_number") or 0,
                "page_name": p["page_name"],
                "text": texts.get(p["id"], ""),
                "staged": staged.page(p["page_name"]),
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
                        confidence=_page_confidence(page, staged.page(page.page_name)),
                        provided_name=manifest.get("member_name"),
                        provided_dob=provided_dob,
                        provided_external_member_id=manifest.get("external_member_id"),
                        other_names=page.other_names,
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
                        "confidence": _page_confidence(page, staged.page(page.page_name)),
                        "provided_name": manifest.get("member_name") or "",
                        "provided_dob": provided_dob or "",
                        "provided_external_member_id": manifest.get("external_member_id") or "",
                        "matched": page.page_verified,
                        "ner_enabled": result.ner_enabled,
                    }
                )

        csv_rows += _extract_only_pages(
            chart_id, chart_name, ctx, extract_only, texts, staged, expected, name_mode,
            ner_model_id, manifest, provided_dob, page_map,
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
                confidence=_agreement_rate(result, final_status),
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
                    # An agreement rate, the database's number (2026-11-14).
                    "confidence": _agreement_rate(result, final_status) if result.pages_checked else "",
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
