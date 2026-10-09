"""Stage: codeable / non-codeable / discharge → ``page_classification``.

* **Main pages** (not blank/junk/duplicate): the page-family model names the
  family when its top probability is at least 0.50. Otherwise a keyword
  family whose raw score is above 0.70 is used. Otherwise a family in both
  top-3 lists is used. Otherwise the page type is Others. Keyword rules name the subtype inside the chosen
  family. No subtype hit leaves the subtype equal to the family. An entry with
  ``continue`` opens a span for its family that later pages on the same date
  inherit. A page whose only date is the DOS default has no date here, so it
  shares a span with nobody.
* **Output page type** is ``Family (Page Type)`` — e.g. ``Progress Note (SOAP
  Note)`` — in the CSV ``page_type`` and ``page_classification.page_subtype``.
  ``confidence`` is assigned by the step that named the family: the model's
  probability, the keyword lead, the shared family's model probability, or 0
  for Others. ``type_confidence`` is the type's probability within the family.
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
from stages.lib.page_classify.family_model import annotate as annotate_families
from stages.lib.page_classify.postprocess import apply as apply_postprocess
from stages.lib.page_classify.postprocess import signed_page_names

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
    "continues_previous",
    "continue_reason",
    "matched_keyword",
    "family_source",
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
    "family_source",
    "tag",
    "confidence",
    "type_confidence",
    "type_scores",
    "continue_applied",
    "previous_family",
    "filled_between",
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
        texts: dict[int, str] = {}
        for page in ctx.pages:
            page_id = page["id"]
            flag = (bj_rows.get(page_id) or {}).get(
                "blank_junk_flag", "not_blank_junk"
            )
            f2 = final2.get(page_id)
            f1 = final1.get(page_id)
            pr = prelim.get(page_id)
            text = best_page_text(
                final2=f2,
                final1=f1,
                prelim=pr,
                quality_row=quality.get(page_id),
            )
            texts[page_id] = text
            if flag in BJ_EXCLUDE:
                continue
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

        family_source = annotate_families(page_inputs)
        classified = classify_pages(page_inputs)
        apply_postprocess(
            classified, signed_names=signed_page_names(ctx.chart_name)
        )
        by_id = {row["page_id"]: row for row in classified}
        from stages.lib.continuation import tag_pages

        continuation = tag_pages(
            [
                {
                    "page_id": page["id"],
                    "text": texts.get(page["id"], ""),
                    "family": (by_id.get(page["id"]) or {}).get("family") or "",
                }
                for page in ctx.pages
            ]
        )
        continuation_by_id = {
            page["id"]: tag for page, tag in zip(ctx.pages, continuation)
        }

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
                            "continues_previous": (continuation_by_id.get(page_id) or {}).get(
                                "continues_previous", "n"
                            ),
                            "continue_reason": (continuation_by_id.get(page_id) or {}).get(
                                "continue_reason", ""
                            ),
                            "matched_keyword": "",
                            "family_source": "",
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
                if page_type == "Others":
                    category = "non_codeable"
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
                        "continues_previous": (continuation_by_id.get(page_id) or {}).get(
                            "continues_previous", "n"
                        ),
                        "continue_reason": (continuation_by_id.get(page_id) or {}).get(
                            "continue_reason", ""
                        ),
                        "matched_keyword": row.get("matched_keyword") or "",
                        "family_source": row.get("family_source") or "",
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
            "page_subtype chart=%s pages=%d main=%d blank_junk=%d continue=%d family=%s → %s",
            ctx.chart_name,
            len(csv_rows),
            main_tagged,
            bj_tagged,
            carried,
            family_source,
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
