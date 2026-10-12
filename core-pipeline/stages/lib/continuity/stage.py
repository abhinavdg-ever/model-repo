"""Stage: document continuity → ``page_continuity_results``.

Runs after page type and DOS, because pages sharing a date of service are one
document and an undated page's document ends on a signature, a page-type
change or text stopping mid-page (see ``engine``). ``link_strength`` (strong
only when every link from the start is a shared DOS) and ``start_confirmed``
feed the page-type continuation rules in ``imaging_final``. It writes only the
grouping; Final values are written by the ``imaging_final`` stage. Final DOS
is each page's own.

Also writes ``imaging/<chart>_continuity.csv`` for review-ui Local Mode.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Optional

from db import (
    connect,
    get_blank_junk_flags,
    get_dos_map,
    get_ocr_texts,
    get_page_classification_map,
    get_quality_map,
    upsert_continuity,
)
from db.paths import imaging_csv, write_csv
from stages._support import BJ_EXCLUDE, best_page_text, mark_completed, mark_skipped, stage_run
from stages.lib.continuity.engine import PageInput, assign, text_lines
from stages.lib.continuity.layout import load_pages, page_layout
from stages.lib.continuity.signature import has_signature_section, signed_page_names

logger = logging.getLogger(__name__)

STAGE = "continuity"

CONTINUITY_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "document",
    "continuation",
    "position",
    "page_in_document",
    "relation",
    "link_strength",
    "start_confirmed",
    "decided_by",
    "confidence",
    "page_no",
    "page_total",
    "page_type",
    "dos_from",
    "dos_to",
    "section_headers",
    "layout_source",
    "evidence",
]


def _iso(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")


def _printed(staged_page: Any) -> Optional[tuple[int, int]]:
    """The printed ``page N of M`` the key/value extraction chose."""
    if staged_page is None:
        return None
    for row in staged_page.selected("page_no"):
        try:
            number = int(row.get("page_no") or 0)
            total = int(row.get("page_total") or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= total:
            return number, total
    return None


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, force=force) as ctx:
        with connect() as conn:
            bj = get_blank_junk_flags(conn, chart_id, final_only=True)
            skipped_ids = [pid for pid in ctx.todo if bj.get(pid, "not_blank_junk") in BJ_EXCLUDE]
            mark_skipped(conn, ctx, skipped_ids, "blank_junk")
            prelim = get_ocr_texts(conn, chart_id, "tesseract")
            final1 = get_ocr_texts(conn, chart_id, "docling")
            final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
            quality = get_quality_map(conn, chart_id)
            classification = get_page_classification_map(conn, chart_id)
            dos = get_dos_map(conn, chart_id)

        from stages.lib.extraction.stage import ensure_staging
        staged = ensure_staging(chart_id, ctx.chart_name)
        signed_names = signed_page_names(ctx.chart_name)
        json2 = load_pages(ctx.chart_name, "final2")
        json1 = load_pages(ctx.chart_name, "final1")

        # Continuity is chart-global: every page is an input even on a resume.
        inputs: list[PageInput] = []
        sources: dict[Any, str] = {}
        for page in ctx.pages:
            page_id = page["id"]
            name = page["page_name"]
            flag = bj.get(page_id, "not_blank_junk")
            skipped = flag in BJ_EXCLUDE
            text = "" if skipped else best_page_text(
                final2=final2.get(page_id),
                final1=final1.get(page_id),
                prelim=prelim.get(page_id),
                quality_row=quality.get(page_id),
            )
            lines, headers, source = page_layout(json2.get(name), json1.get(name))
            sources[page_id] = source or "text"
            page_dos = dos.get(page_id) or {}
            kind = classification.get(page_id) or {}
            inputs.append(
                PageInput(
                    page_id=page_id,
                    page_name=name,
                    page_number=page.get("page_number"),
                    text=text,
                    lines=lines or text_lines(text),
                    section_headers=headers,
                    page_type=kind.get("page_type") or "",
                    page_subtype=kind.get("page_subtype") or "",
                    page_confidence=(
                        float(kind["confidence"]) if kind.get("confidence") is not None else None
                    ),
                    title_hit=bool(kind.get("keyword_title_hit")),
                    dos_from=_iso(page_dos.get("date_of_service_from")),
                    dos_to=_iso(page_dos.get("date_of_service_to")),
                    signed=name in signed_names or has_signature_section(text),
                    printed=_printed(staged.page(name)),
                    skipped=skipped,
                    breaks=flag in ("blank", "junk"),
                )
            )

        rows = assign(inputs)

        csv_rows: list[dict[str, Any]] = []
        documents = 0
        with connect() as conn:
            for page, row in zip(ctx.pages, rows):
                page_id = page["id"]
                # Blank / junk / duplicate pages belong to no document: no row.
                if row["relation"]:
                    upsert_continuity(conn, chart_id=chart_id, page_id=page_id, row=row)
                    if page_id in ctx.todo:
                        mark_completed(conn, ctx, page_id)
                documents = max(documents, row["document_seq"] or 0)
                csv_rows.append(
                    {
                        "chart_name": ctx.chart_name,
                        "page_name": page["page_name"],
                        "page_number": page.get("page_number"),
                        "document": row["document_seq"] or "",
                        "continuation": row["label"],
                        "position": row["position"] or "",
                        "page_in_document": row["seq"] or "",
                        "relation": row["relation"],
                        "link_strength": row["link_strength"] or "",
                        "start_confirmed": "y" if row["start_confirmed"] else ("n" if row["relation"] else ""),
                        "decided_by": row["decided_by"],
                        "confidence": row["confidence_level"],
                        "page_no": row["page_no"] or "",
                        "page_total": row["page_total"] or "",
                        "page_type": row["page_type"],
                        "dos_from": row["dos_from"],
                        "dos_to": row["dos_to"],
                        "section_headers": json.dumps(
                            [h["matched_canonical"] or h["text"] for h in row["section_headers"]]
                        ),
                        "layout_source": sources.get(page_id, ""),
                        "evidence": row["evidence"],
                    }
                )

        path = write_csv(imaging_csv(ctx.chart_name, "continuity"), CONTINUITY_COLS, csv_rows)
        logger.info(
            "continuity chart=%s pages=%d documents=%d → %s",
            ctx.chart_name, len(rows), documents, path,
        )
        return {
            "chart_id": chart_id,
            "continuity_csv": str(path),
            "documents": documents,
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
        }
