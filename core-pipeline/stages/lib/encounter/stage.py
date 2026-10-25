"""Stage: encounter type (Outpatient F2F / Tele / Inpatient / Home).

One answer per visit — a continuity document (consecutive pages sharing a date
when continuity has not run) — decided
from the highest tier of evidence it has (see ``encounter_classify``) and
stamped on every page of the visit. Pages without a usable date, and visits
with no setting evidence, stay empty with a reason; they never inherit.

Tier 1 evidence is the page type: the page's own (Extracted) classification
from ``page_classification``, its most specific name the encounter canon
lists (canon page type, L3, L2, L1), else its family (L1).

A page with no tier 1 / tier 2 evidence asks the encounter model (``model_vote``,
``models/encounter/``): its answer votes when ``encounter_canon.json`` → ``model_vote``
takes that class at that confidence (Evisit, Home Visit, Inpatient), else the page votes
its family's default. Without the model the rules decide alone.

Runs after ``continuity``. Writes ``encounter_type_results`` for resolved
pages, including visits with no evidence or no date, which default to
Outpatient (F2F) at confidence 0.3 (canon ``default``); it removes rows for
pages left unresolved (only when that default is emptied), and
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
    replace_encounter_results,
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
from stages.lib.continuity.stage import scores_path as continuity_scores_path
from stages.lib.encounter.encounter_classify import classify_pages, visit_date
from stages.lib.encounter import model_vote
from stages.lib.encounter.encounter_classify import load_canon as load_encounter_canon
from stages.lib.page_classify import axes
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
    "seq",
    "document",
    "page_family",
    "first_page",
    "pages",
    "dos_from",
    "dos_to",
    "encounter_type",
    "encounter_label",
    "matched_keyword",
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


def _stage_encounters(chart_name: str, visit_log: list[dict[str, Any]]) -> str:
    """The encounter grouping, at the end of ``staging/continuity.json``: one entry per
    encounter (its pages, page family, date and the one decision every page carries)."""
    path = continuity_scores_path(chart_name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        payload = {}
    payload["encounters"] = [
        {k: record.get(k) for k in (
            "seq", "document", "page_family", "pages", "dos_from", "dos_to",
            "encounter_type", "encounter_label", "confidence", "decided_by", "reason",
            "matched_keyword", "conflict", "contenders",
        )}
        for record in visit_log
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return str(path)


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

        # Tier 1 reads the page's own (Extracted) classification: its most
        # specific name the canon lists (canon page type, L3, L2, L1),
        # else its family.
        tier1 = load_encounter_canon().tier1
        for page_input in page_inputs:
            kind = classification.get(page_input["page_id"]) or {}
            names = axes.names_of(kind)
            page_input["page_type_id"] = next((n for n in names if n in tier1), axes.family_of(kind))
            page_input["page_type_name"] = page_input["page_type_id"]
            # Encounter type applies only to some families (canon applies_to). A page
            # neither the model nor the canon was sure of (decided_by others) has no
            # family: it votes its own evidence, as an Unknown page did.
            unsure = axes.decided_by_of(kind) == axes.BY_OTHERS
            page_input["page_family"] = "" if unsure else axes.family_of(kind)
            # The continuity stage's document: one visit, one encounter type.
            page_input["document"] = (continuity.get(page_input["page_id"]) or {}).get("document_seq")

        # The encounter model's answer per page; the vote uses it only where the rules
        # found no evidence (encounter_canon.json -> model_vote).
        for page_input, answer in zip(page_inputs, model_vote.predict([p["text"] for p in page_inputs])):
            page_input["encounter_model"] = answer

        # One record per encounter: always staged in continuity.json; the debug CSV too
        # with ENCOUNTER_DEBUG.
        visit_log: list[dict[str, Any]] = []
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
            # The grouping itself, for review-ui's Production Mode (as staged below).
            replace_encounter_results(conn, chart_id, visit_log)

        path = write_csv(
            imaging_csv(ctx.chart_name, "encounter"), ENCOUNTER_COLS, csv_rows
        )
        encounters_json = _stage_encounters(ctx.chart_name, visit_log)
        if ENCOUNTER_DEBUG:
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
            "encounters_json": encounters_json,
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            "tagged": tagged,
            "unresolved": len(unresolved),
        }
