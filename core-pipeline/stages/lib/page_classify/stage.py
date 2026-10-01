"""Stage: codeable / non-codeable / discharge → ``page_classification``.

* **Main pages** (not blank/junk/duplicate): keyword match against
  ``codeable_canon.json`` (see ``codeable_classify``). An entry with
  ``continue`` opens a span for its family that later pages on the same date
  inherit. A page whose only date is the DOS default has no date here, so it
  shares a span with nobody.
* **Output page type** is ``Family (Page Type)`` — e.g. ``Progress Note (SOAP
  Note)`` — in the CSV ``page_type`` and ``page_classification.page_subtype``.
  ``confidence`` is the family's; ``type_confidence`` is the type's probability
  within the family.
* **Blank / junk / duplicate**: always ``non_codeable``. ``page_subtype``
  is the existing junk label (Invoice, Cover Page, …) or Blank / Duplicate.

Also writes ``imaging/<chart>_codeable.csv`` for review-ui Local Mode. With
PAGE_CLASSIFY_DEBUG on, the evidence behind every page — per-family scores,
each keyword hit with its role and band, the previous page's family, the OCR
source — goes to ``<chart>/debug/<chart>_page_classify_evidence.csv``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from config import PAGE_CLASSIFY_DEBUG, chart_dir

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
from stages.lib.canon_store import CANON_DIR, CanonFile
from stages.lib.page_classify.codeable_classify import classify_pages

logger = logging.getLogger(__name__)

STAGE = "page_subtype"

# Canon tags → page_classification.classification_category CHECK values.
# ``not_sure`` is CSV/UI only (no DB row) until a schema value exists.
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
    "type_confidence",
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


EVIDENCE_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "page_position",
    "page_type",
    "entry_id",
    "family",
    "tag",
    "confidence",
    "type_confidence",
    "type_scores",
    "continue_applied",
    "previous_family",
    "ocr_source",
    "family_scores",
    "hits",
]

_DOS_PROFILE: CanonFile[dict[str, Any]] = CanonFile(CANON_DIR / "dos_canon.json")


def _page_dos(dos: dict[str, Any]) -> tuple[str, str]:
    """The page's date for span keys: page level, else document level.

    The DOS default is not a date. Every page where extraction failed carries
    it, so treating it as one would let a single Progress Note span the lot.
    """
    dos_from = dos.get("date_of_service_from") or dos.get("date_of_service_from_doclevel")
    dos_to = dos.get("date_of_service_to") or dos.get("date_of_service_to_doclevel")
    default = str(_DOS_PROFILE.get().get("DOS_DEFAULT_DATE") or "")
    if not dos.get("date_of_service_from") and str(dos_from or "") == default:
        return "", ""
    return str(dos_from or ""), str(dos_to or "")


def _output_page_type(row: dict[str, Any]) -> str:
    """``Family (Page Type)``; just the name when the two are the same.

    "Not Available" when nothing matched.
    """
    family = row.get("family_display") or ""
    page_type = row.get("page_type") or ""
    if not family:
        return page_type
    if not page_type or page_type.casefold() == family.casefold():
        return family
    return f"{family} ({page_type})"


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
            dos_from, dos_to = _page_dos(dos_by_page.get(page_id) or {})
            sources[page_id] = _ocr_source_label(final2=f2, final1=f1, prelim=pr)
            page_inputs.append(
                {
                    "page_id": page_id,
                    "page_name": page["page_name"],
                    "page_number": page.get("page_number"),
                    "text": text,
                    "dos_from": dos_from,
                    "dos_to": dos_to,
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
                            "type_confidence": "",
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
                page_type = _output_page_type(row)
                tag = (row.get("tag") or "").strip()
                category = _TAG_TO_CATEGORY.get(tag)
                conf = row.get("confidence")
                conf_f = float(conf) if conf not in ("", None) else None
                if category:
                    upsert_page_classification(
                        conn,
                        chart_id=chart_id,
                        page_id=page_id,
                        page_subtype=page_type or None,
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
                        "page_type": page_type,
                        "type_confidence": row.get("type_confidence", ""),
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
        if PAGE_CLASSIFY_DEBUG:
            total = max(1, len(ctx.pages))
            position = {p["id"]: i for i, p in enumerate(ctx.pages, start=1)}
            write_csv(
                chart_dir(ctx.chart_name) / "debug"
                / f"{ctx.chart_name}_page_classify_evidence.csv",
                EVIDENCE_COLS,
                (
                    {
                        **row,
                        "chart_name": ctx.chart_name,
                        "page_position": round(position[row["page_id"]] / total, 4),
                        "ocr_source": sources.get(row["page_id"], ""),
                        "family_scores": json.dumps(row["family_scores"]),
                        "type_scores": json.dumps(row["type_scores"]),
                        "hits": json.dumps(row["hits"]),
                    }
                    for row in classified
                ),
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
