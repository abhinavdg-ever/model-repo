"""Stage: encounter type (Outpatient F2F / Tele / Inpatient / Home).

One answer per visit — a continuity document (consecutive pages sharing a date
when continuity has not run) — decided
from the highest tier of evidence it has (see ``encounter_classify``) and
stamped on every page of the visit. Pages without a usable date, and visits
with no setting evidence, stay empty with a reason; they never inherit.

Tier 1 evidence is the page type: the page's own (Extracted) classification
from ``page_classification``, its sub-type when the encounter canon names it,
else its page type.

Runs after ``continuity``. Writes ``encounter_type_results`` for resolved
pages (and removes rows for pages that are now unresolved), and
``imaging/<chart>_encounter.csv`` for every page. With ENCOUNTER_DEBUG on, one
evidence record per visit goes to ``<chart>/debug/<chart>_encounter_evidence.csv``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from config import ENCOUNTER_DEBUG, chart_dir
from db import (
    connect,
    delete_encounter,
    get_blank_junk_flags,
    get_continuity_map,
    get_page_classification_map,
    get_ocr_texts,
    get_quality_map,
    upsert_encounter,
)
from db.paths import imaging_csv, write_csv
from stages._support import (
    BJ_EXCLUDE,
    best_page_text,
    mark_completed,
    mark_skipped,
    stage_run,
)
from stages.lib.encounter.encounter_classify import classify_pages, visit_date
from stages.lib.encounter.encounter_classify import load_canon as load_encounter_canon
from stages.lib.page_classify.stage import _DOS_PROFILE

logger = logging.getLogger(__name__)

STAGE = "encounter_type"

ENCOUNTER_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "encounter_type",
    "encounter_label",
    "confidence",
    "matched_keyword",
    "continue_applied",
    "decided_by",
    "reason",
    "conflict",
    "dos_from",
    "dos_to",
    "ocr_source",
]

EVIDENCE_COLS = [
    "chart_name",
    "first_page",
    "pages",
    "dos_from",
    "dos_to",
    "encounter_type",
    "decided_by",
    "confidence",
    "conflict",
    "contenders",
    "reason",
    "findings",
    "cancelled",
    "negatives_fired",
    "context",
]


def _dos_map(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    from db import get_dos_map

    return get_dos_map(conn, chart_id)


def _ocr_source_label(
    *,
    final2: Optional[str],
    final1: Optional[str],
    prelim: Optional[str],
) -> str:
    if final2 and str(final2).strip():
        return "final2"
    if final1 and str(final1).strip():
        return "final1"
    if prelim and str(prelim).strip():
        return "prelim"
    return ""


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    default_date = str(_DOS_PROFILE.get().get("DOS_DEFAULT_DATE") or "")
    with stage_run(chart_id, STAGE, force=force) as ctx:
        with connect() as conn:
            bj = get_blank_junk_flags(conn, chart_id, final_only=True)
            drop = [
                pid
                for pid in ctx.todo
                if bj.get(pid, "not_blank_junk") in BJ_EXCLUDE
            ]
            mark_skipped(conn, ctx, drop, "blank_junk")
            todo = set(ctx.todo)

            prelim = get_ocr_texts(conn, chart_id, "tesseract")
            final1 = get_ocr_texts(conn, chart_id, "docling")
            final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
            quality = get_quality_map(conn, chart_id)
            dos_by_page = _dos_map(conn, chart_id)
            classification = get_page_classification_map(conn, chart_id)
            continuity = get_continuity_map(conn, chart_id)

        page_inputs: list[dict[str, Any]] = []
        sources: dict[int, str] = {}
        for page in ctx.pages:
            page_id = page["id"]
            if bj.get(page_id, "not_blank_junk") in BJ_EXCLUDE:
                continue
            f2 = final2.get(page_id)
            f1 = final1.get(page_id)
            pr = prelim.get(page_id)
            text = best_page_text(
                final2=f2,
                final1=f1,
                prelim=pr,
                quality_row=quality.get(page_id),
            )
            dos = dos_by_page.get(page_id) or {}
            sources[page_id] = _ocr_source_label(final2=f2, final1=f1, prelim=pr)
            dos_from, dos_to, reason = visit_date(
                page_from=str(dos.get("date_of_service_from") or ""),
                page_to=str(dos.get("date_of_service_to") or ""),
                doc_from=str(dos.get("date_of_service_from_doclevel") or ""),
                doc_to=str(dos.get("date_of_service_to_doclevel") or ""),
                default_date=default_date,
            )
            page_inputs.append(
                {
                    "page_id": page_id,
                    "page_name": page["page_name"],
                    "page_number": page.get("page_number"),
                    "text": text,
                    "dos_from": dos_from,
                    "dos_to": dos_to,
                    "reason": reason,
                }
            )

        # Tier 1 reads the page's own (Extracted) classification: its sub-type
        # when the canon names it, else its page type.
        tier1 = load_encounter_canon().tier1
        for page_input in page_inputs:
            kind = classification.get(page_input["page_id"]) or {}
            subtype, page_type = kind.get("page_subtype") or "", kind.get("page_type") or ""
            page_input["page_type_id"] = subtype if subtype in tier1 else page_type
            page_input["page_type_name"] = page_input["page_type_id"]
            # The continuity stage's document: one visit, one encounter type.
            page_input["document"] = (continuity.get(page_input["page_id"]) or {}).get("document_seq")

        visit_log: Optional[list[dict[str, Any]]] = [] if ENCOUNTER_DEBUG else None
        classified = classify_pages(page_inputs, visit_log=visit_log)

        csv_rows: list[dict[str, Any]] = []
        unresolved: list[int] = []
        with connect() as conn:
            for row in classified:
                page_id = row["page_id"]
                et = row["encounter_type"]
                if et:
                    upsert_encounter(
                        conn,
                        chart_id=chart_id,
                        page_id=page_id,
                        encounter_type=et,
                        confidence=float(row["confidence"]),
                        matched_keyword=row["matched_keyword"] or None,
                    )
                else:
                    unresolved.append(page_id)
                if page_id in todo:
                    mark_completed(conn, ctx, page_id)
                csv_rows.append(
                    {
                        "chart_name": ctx.chart_name,
                        "page_name": row["page_name"],
                        "page_number": row["page_number"],
                        "encounter_type": et,
                        "encounter_label": row["encounter_label"],
                        "confidence": row["confidence"],
                        "matched_keyword": row["matched_keyword"],
                        "continue_applied": row["continue_applied"],
                        "decided_by": row["decided_by"],
                        "reason": row["reason"],
                        "conflict": "y" if row["conflict"] else "n",
                        "dos_from": row["dos_from"],
                        "dos_to": row["dos_to"],
                        "ocr_source": sources.get(page_id, ""),
                    }
                )
            delete_encounter(conn, chart_id, unresolved)

        path = write_csv(
            imaging_csv(ctx.chart_name, "encounter"), ENCOUNTER_COLS, csv_rows
        )
        if visit_log is not None:
            write_csv(
                chart_dir(ctx.chart_name) / "debug" / f"{ctx.chart_name}_encounter_evidence.csv",
                EVIDENCE_COLS,
                (
                    {
                        **{k: json.dumps(v) if isinstance(v, (list, dict)) else v
                           for k, v in record.items()},
                        "chart_name": ctx.chart_name,
                    }
                    for record in visit_log
                ),
            )

        tagged = len(csv_rows) - len(unresolved)
        logger.info(
            "encounter_type chart=%s pages=%d tagged=%d unresolved=%d → %s",
            ctx.chart_name,
            len(csv_rows),
            tagged,
            len(unresolved),
            path,
        )
        return {
            "chart_id": chart_id,
            "encounter_csv": str(path),
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            "tagged": tagged,
            "unresolved": len(unresolved),
        }
