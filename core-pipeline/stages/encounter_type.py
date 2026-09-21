"""Stage: encounter type (Outpatient F2F / Tele / Inpatient / Home).

Term-frequency match against ``encounter_canon.json``. One type is chosen for
each page-level DOS and stamped on every page that shares it.

Runs after ``page_subtype``. Writes DB + ``imaging/<chart>_encounter.csv``.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from db import (
    connect,
    get_blank_junk_flags,
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
from stages.lib.imaging.encounter_classify import classify_pages

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
    "dos_from",
    "dos_to",
    "ocr_source",
]


def _dos_map(conn: Any, chart_id: int) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT page_id,
               date_of_service_from,
               date_of_service_to,
               date_of_service_from_doclevel,
               date_of_service_to_doclevel
          FROM dos_extraction_results
         WHERE chart_id = %s
        """,
        (chart_id,),
    ).fetchall()
    return {int(row["page_id"]): dict(row) for row in rows}


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

        page_inputs: list[dict[str, Any]] = []
        sources: dict[int, str] = {}
        classify_ids: set[int] = set()
        for page in ctx.pages:
            page_id = page["id"]
            if bj.get(page_id, "not_blank_junk") in BJ_EXCLUDE:
                continue
            classify_ids.add(page_id)
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
            dos_from = dos.get("date_of_service_from") or dos.get(
                "date_of_service_from_doclevel"
            )
            dos_to = dos.get("date_of_service_to") or dos.get(
                "date_of_service_to_doclevel"
            )
            sources[page_id] = _ocr_source_label(final2=f2, final1=f1, prelim=pr)
            page_inputs.append(
                {
                    "page_id": page_id,
                    "page_name": page["page_name"],
                    "page_number": page.get("page_number"),
                    "text": text,
                    "dos_from": str(dos_from or ""),
                    "dos_to": str(dos_to or ""),
                }
            )

        classified = classify_pages(page_inputs)
        by_id = {row["page_id"]: row for row in classified}

        csv_rows: list[dict[str, Any]] = []
        with connect() as conn:
            for page in ctx.pages:
                page_id = page["id"]
                if page_id not in classify_ids:
                    continue
                row = by_id.get(page_id) or {}
                et = (row.get("encounter_type") or "").strip()
                if et:
                    conf = row.get("confidence")
                    upsert_encounter(
                        conn,
                        chart_id=chart_id,
                        page_id=page_id,
                        encounter_type=et,
                        confidence=float(conf) if conf not in ("", None) else None,
                        matched_keyword=row.get("matched_keyword") or None,
                    )
                if page_id in todo:
                    mark_completed(conn, ctx, page_id)
                csv_rows.append(
                    {
                        "chart_name": ctx.chart_name,
                        "page_name": page["page_name"],
                        "page_number": page.get("page_number"),
                        "encounter_type": et,
                        "encounter_label": row.get("encounter_label") or "",
                        "confidence": row.get("confidence") or "",
                        "matched_keyword": row.get("matched_keyword") or "",
                        "continue_applied": row.get("continue_applied") or "n",
                        "dos_from": row.get("dos_from") or "",
                        "dos_to": row.get("dos_to") or "",
                        "ocr_source": sources.get(page_id, ""),
                    }
                )

        path = write_csv(
            imaging_csv(ctx.chart_name, "encounter"), ENCOUNTER_COLS, csv_rows
        )
        tagged = sum(1 for r in csv_rows if r["encounter_type"])
        logger.info(
            "encounter_type chart=%s pages=%d tagged=%d → %s",
            ctx.chart_name,
            len(csv_rows),
            tagged,
            path,
        )
        return {
            "chart_id": chart_id,
            "encounter_csv": str(path),
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            "tagged": tagged,
        }
