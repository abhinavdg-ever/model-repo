"""Stage: codeable / non-codeable / discharge → ``page_classification``.

* **Main pages** (not blank/junk/duplicate): term-frequency match against
  ``codeable_canon.json``. Visit Report / Progress Note / Discharge Report
  families (``continue=y``) keep their tag until the page DOS changes.
* **Blank / junk / duplicate**: always ``non_codeable``. ``page_subtype``
  is the existing junk label (Invoice, Cover Page, …) or Blank / Duplicate.

Also writes ``imaging/<chart>_codeable.csv`` for review-ui Local Mode.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from db import (
    connect,
    get_blank_junk_final,
    get_ocr_texts,
    get_quality_map,
    upsert_page_classification,
)
from db.paths import imaging_csv, write_csv
from stages._support import (
    BJ_EXCLUDE,
    best_page_text,
    mark_completed,
    stage_run,
)
from stages.lib.imaging.codeable_classify import classify_pages

logger = logging.getLogger(__name__)

STAGE = "page_subtype"

# Canon tags → page_classification.classification_category CHECK values.
_TAG_TO_CATEGORY = {
    "codeable": "codeable",
    "non_codeable": "non_codeable",
    "discharge_frequency": "discharge_summary",
    "discharge_summary": "discharge_summary",
}

CODEABLE_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "page_type",
    "tag",
    "is_codeable",
    "confidence",
    "continue",
    "continue_applied",
    "matched_keyword",
    "dos_from",
    "dos_to",
    "ocr_source",
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


def _bj_page_subtype(flag: str, junk_subtype: Optional[str]) -> str:
    """Reuse blank_junk labels as page_classification.page_subtype."""
    if flag == "blank":
        return "Blank"
    if flag == "duplicate":
        return "Duplicate"
    if flag == "junk":
        return (junk_subtype or "Others").strip() or "Others"
    return "Main"


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, force=force) as ctx:
        with connect() as conn:
            bj_rows = get_blank_junk_final(conn, chart_id)
            todo = set(ctx.todo)

            prelim = get_ocr_texts(conn, chart_id, "tesseract")
            final1 = get_ocr_texts(conn, chart_id, "docling")
            final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
            quality = get_quality_map(conn, chart_id)
            dos_by_page = _dos_map(conn, chart_id)

        # TF-classify main pages only; blank/junk/duplicate → non_codeable below.
        page_inputs: list[dict[str, Any]] = []
        sources: dict[int, str] = {}
        for page in ctx.pages:
            page_id = page["id"]
            flag = (bj_rows.get(page_id) or {}).get(
                "blank_junk_flag", "not_blank_junk"
            )
            if flag in BJ_EXCLUDE:
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
        main_tagged = 0
        bj_tagged = 0
        carried = 0

        with connect() as conn:
            for page in ctx.pages:
                page_id = page["id"]
                bj = bj_rows.get(page_id) or {}
                flag = bj.get("blank_junk_flag") or "not_blank_junk"
                dos = dos_by_page.get(page_id) or {}
                dos_from = str(
                    dos.get("date_of_service_from")
                    or dos.get("date_of_service_from_doclevel")
                    or ""
                )
                dos_to = str(
                    dos.get("date_of_service_to")
                    or dos.get("date_of_service_to_doclevel")
                    or ""
                )

                if flag in BJ_EXCLUDE:
                    subtype = _bj_page_subtype(flag, bj.get("junk_subtype"))
                    conf = bj.get("confidence")
                    conf_f = float(conf) if conf is not None else None
                    upsert_page_classification(
                        conn,
                        chart_id=chart_id,
                        page_id=page_id,
                        page_subtype=subtype,
                        classification_category="non_codeable",
                        confidence=conf_f,
                        duplicate_flag=(flag == "duplicate"),
                    )
                    bj_tagged += 1
                    if page_id in todo:
                        mark_completed(conn, ctx, page_id)
                    csv_rows.append(
                        {
                            "chart_name": ctx.chart_name,
                            "page_name": page["page_name"],
                            "page_number": page.get("page_number"),
                            "page_type": subtype,
                            "tag": "non_codeable",
                            "is_codeable": "Non Codeable",
                            "confidence": conf if conf is not None else "",
                            "continue": "n",
                            "continue_applied": "n",
                            "matched_keyword": "",
                            "dos_from": dos_from,
                            "dos_to": dos_to,
                            "ocr_source": "",
                        }
                    )
                    continue

                row = by_id.get(page_id) or {}
                tag = (row.get("tag") or "").strip()
                category = _TAG_TO_CATEGORY.get(tag)
                conf = row.get("confidence")
                conf_f = float(conf) if conf not in ("", None) else None
                if category:
                    upsert_page_classification(
                        conn,
                        chart_id=chart_id,
                        page_id=page_id,
                        page_subtype=row.get("page_type") or None,
                        classification_category=category,
                        confidence=conf_f,
                        duplicate_flag=False,
                    )
                    main_tagged += 1
                if page_id in todo:
                    mark_completed(conn, ctx, page_id)
                if row.get("continue_applied") == "y":
                    carried += 1
                csv_rows.append(
                    {
                        "chart_name": ctx.chart_name,
                        "page_name": page["page_name"],
                        "page_number": page.get("page_number"),
                        "page_type": row.get("page_type") or "",
                        "tag": tag,
                        "is_codeable": row.get("is_codeable") or "",
                        "confidence": row.get("confidence") or "",
                        "continue": row.get("continue") or "n",
                        "continue_applied": row.get("continue_applied") or "n",
                        "matched_keyword": row.get("matched_keyword") or "",
                        "dos_from": row.get("dos_from") or dos_from,
                        "dos_to": row.get("dos_to") or dos_to,
                        "ocr_source": sources.get(page_id, ""),
                    }
                )

        # Stable CSV order by page number / name.
        csv_rows.sort(
            key=lambda r: (
                r["page_number"] is None,
                r["page_number"] or 0,
                str(r["page_name"]),
            )
        )
        path = write_csv(
            imaging_csv(ctx.chart_name, "codeable"), CODEABLE_COLS, csv_rows
        )
        logger.info(
            "page_subtype chart=%s pages=%d main=%d blank_junk=%d continue=%d → %s",
            ctx.chart_name,
            len(csv_rows),
            main_tagged,
            bj_tagged,
            carried,
            path,
        )
        return {
            "chart_id": chart_id,
            "codeable_csv": str(path),
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            "tagged": main_tagged + bj_tagged,
            "main_tagged": main_tagged,
            "blank_junk_tagged": bj_tagged,
            "continue_applied": carried,
        }
