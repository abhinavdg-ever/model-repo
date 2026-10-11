"""Stage: Final values → ``imaging_final`` and ``imaging/<chart>_final.csv``.

The last stage. It reads every stage's stored output and writes, per page, the
value a reviewer should take as final, so review-ui and reports read Finals
from one place instead of re-deriving them.

Most Finals are the page's own value. Two are decided across a document:

* **Page type** (with sub-type, model type, codability): the page's own
  (Extracted) result unless a continuation rule changes it —
  ``page_classify.arbitration.decide_final`` on the continuity stage's
  documents. ``page_type_source`` is page / continuation / embedded, and
  ``continuation_rule`` names the rule.
* **Date of service**: the document's first page's, else its first dated
  page's (``continuity.engine.document_finals``); ``dos_source`` = document.

Blank and junk pages belong to no document and keep their own values. A
duplicate page is skipped by every stage after blank/junk, so it takes the
content values of the page it duplicates (``COPIED_FROM_REFERENCE``):
``page_type_source`` / ``dos_source`` = duplicate and ``duplicate_of_page_id``
names that page.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Optional

from db import (
    IMAGING_FINAL_COLUMNS,
    connect,
    get_blank_junk_final,
    get_continuity_map,
    get_dos_map,
    get_encounter_map,
    get_member_extraction_map,
    get_page_classification_map,
    get_quality_map,
    get_sequencing_map,
    upsert_imaging_final,
)
from db.paths import imaging_csv, write_csv
from stages._support import mark_completed, stage_run
from stages.lib.continuity.engine import document_finals
from stages.lib.page_classify.arbitration import FinalPage, decide_final
from stages.lib.page_classify.taxonomy import CATEGORY

logger = logging.getLogger(__name__)

STAGE = "imaging_final"

FINAL_COLS = ["chart_name", "page_name", "page_number", *IMAGING_FINAL_COLUMNS]

# What a duplicate page takes from the page it duplicates. Every stage after
# blank/junk skips duplicates, so these are empty on the duplicate itself. It
# keeps its own image values (quality, orientation), its blank/junk flag and
# its position (``seq``).
COPIED_FROM_REFERENCE = (
    "member_name", "member_dob", "member_id",
    "document_seq",
    "page_type", "page_subtype", "model_type", "codability", "classification_category",
    "continuation_rule", "needs_review",
    "dos_from", "dos_to",
    "encounter_type", "provider_name", "signature_present",
)


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, date):
        return value.isoformat()
    return str(value) if value else None


def _number(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _signatures(chart_id: int, chart_name: str) -> dict[str, dict[str, Any]]:
    """Provider name and signature per page name, from the extraction staging —
    the same rows the extraction stage writes to its signature CSV."""
    from stages.lib.extraction.results import signature_fields
    from stages.lib.extraction.stage import _provider_rows, _selected, ensure_staging

    staged = ensure_staging(chart_id, chart_name)
    out: dict[str, dict[str, Any]] = {}
    for staged_page in staged:
        out[staged_page.name] = signature_fields(
            _selected(staged_page, "electronic_signature"), _provider_rows(staged_page)
        )
    return out


def _documents(
    pages: list[dict[str, Any]], continuity: dict[int, dict[str, Any]]
) -> dict[int, list[int]]:
    """Document number → its page ids in order."""
    documents: dict[int, list[tuple[int, int]]] = {}
    for page in pages:
        row = continuity.get(page["id"])
        if row and row.get("document_seq") is not None:
            documents.setdefault(int(row["document_seq"]), []).append(
                (int(row.get("seq") or 0), page["id"])
            )
    return {doc: [pid for _, pid in sorted(members)] for doc, members in documents.items()}


def _final_page(page_id: int, kind: dict[str, Any], link: dict[str, Any]) -> FinalPage:
    return FinalPage(
        page_id=page_id,
        page_type=kind.get("page_type"),
        page_subtype=kind.get("page_subtype"),
        keyword_page_subtype=kind.get("keyword_page_subtype"),
        keyword_score=_number(kind.get("keyword_score")),
        keyword_title_hit=bool(kind.get("keyword_title_hit")),
        document_seq=link.get("document_seq"),
        seq=link.get("seq"),
        link_strength=link.get("link_strength"),
        start_confirmed=bool(link.get("start_confirmed")),
    )


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, force=force) as ctx:
        with connect() as conn:
            quality = get_quality_map(conn, chart_id)
            blank_junk = get_blank_junk_final(conn, chart_id)
            classification = get_page_classification_map(conn, chart_id)
            dos = get_dos_map(conn, chart_id)
            members = get_member_extraction_map(conn, chart_id)
            encounters = get_encounter_map(conn, chart_id)
            sequencing = get_sequencing_map(conn, chart_id)
            continuity = get_continuity_map(conn, chart_id)
        signatures = _signatures(chart_id, ctx.chart_name)

        # Order, with no step reading a later one's output (so no cycle):
        #   1. own values — each page's DOS (dos_extract) and Extracted page
        #      type (page_subtype), decided page by page;
        #   2. continue — continuity grouped the pages from those own values;
        #   3. page type replicated — the continuation rules, below;
        #   4. DOS — carried over the same documents, after the page type.
        # Continuity never reads a Final value, so a Final cannot feed back
        # into the continue decision.
        own_dos: dict[int, dict[str, Any]] = {}
        for page in ctx.pages:
            page_dos = dos.get(page["id"]) or {}
            own_dos[page["id"]] = {
                "dos_from": _iso(page_dos.get("date_of_service_from")),
                "dos_to": _iso(page_dos.get("date_of_service_to")),
                "page_number": page.get("page_number"),
            }
        documents = _documents(ctx.pages, continuity)
        carried_dos: dict[int, dict[str, Any]] = {}
        start_of: dict[int, int] = {}
        for document_pages in documents.values():
            finals = document_finals([own_dos[pid] for pid in document_pages])
            for pid in document_pages:
                carried_dos[pid] = finals
                start_of[pid] = document_pages[0]
        final_pages = {
            page["id"]: _final_page(
                page["id"], classification.get(page["id"]) or {}, continuity.get(page["id"]) or {}
            )
            for page in ctx.pages
        }

        rows: dict[int, dict[str, Any]] = {}
        # Every row first: a duplicate copies its reference page's, which can
        # come after it in page order.
        for page in ctx.pages:
            page_id = page["id"]
            q = quality.get(page_id) or {}
            flag = (blank_junk.get(page_id) or {}).get("blank_junk_flag")
            kind = classification.get(page_id) or {}
            member = members.get(page_id) or {}
            signature = signatures.get(page["page_name"]) or {}
            start = start_of.get(page_id)
            verdict = decide_final(
                final_pages[page_id],
                final_pages[start] if start is not None and start != page_id else None,
            )
            # DOS: the document's when the page belongs to one, else its own.
            document = carried_dos.get(page_id)
            dates = document or own_dos[page_id]
            dos_origin = "document" if document else "page"
            row = {
                "member_name": member.get("extracted_name") or None,
                "member_dob": _iso(member.get("extracted_dob")),
                "member_id": member.get("extracted_member_id") or None,
                "printed_or_handwritten": q.get("printed_or_handwritten"),
                "handwritten_area_pct": _number(q.get("handwritten_area_pct")),
                "is_visible": q.get("is_visible"),
                "quality_tag": q.get("quality_tag"),
                "orientation_angle": _number(q.get("orientation_angle")),
                "tilt_angle": _number(q.get("tilt_angle")),
                "mirrored": q.get("mirrored"),
                "blank_junk_flag": flag,
                "is_duplicate": flag == "duplicate" or bool(kind.get("duplicate_flag")),
                "document_seq": (continuity.get(page_id) or {}).get("document_seq"),
                "page_type": verdict.page_type,
                "page_subtype": verdict.page_subtype,
                "model_type": verdict.model_type,
                "codability": verdict.codability,
                "classification_category": (
                    CATEGORY.get(verdict.codability or "")
                    or kind.get("classification_category")
                ),
                "page_type_source": verdict.source if verdict.page_type or verdict.page_subtype else None,
                "continuation_rule": verdict.continuation_rule,
                "needs_review": bool(verdict.needs_review or kind.get("needs_review")),
                "dos_from": dates["dos_from"],
                "dos_to": dates["dos_to"],
                "dos_source": dos_origin if dates["dos_from"] else None,
                "encounter_type": (encounters.get(page_id) or {}).get("encounter_type"),
                "provider_name": signature.get("provider_name"),
                "signature_present": signature.get("signature_present") if signature else None,
                "seq": (sequencing.get(page_id) or {}).get("seq"),
                "duplicate_of_page_id": None,
            }
            rows[page_id] = row

        # Duplicates are skipped by every stage after blank/junk, so they have
        # no values of their own: they take the reference page's. Their own
        # quality / orientation and their blank-junk / duplicate status stay.
        for page_id, row in rows.items():
            original = (blank_junk.get(page_id) or {}).get("duplicate_of_page_id")
            if row["blank_junk_flag"] != "duplicate" or original not in rows:
                continue
            reference = rows[original]
            row.update({key: reference[key] for key in COPIED_FROM_REFERENCE})
            row["page_type_source"] = "duplicate" if reference["page_type"] else None
            row["dos_source"] = "duplicate" if reference["dos_from"] else None
            row["duplicate_of_page_id"] = original

        csv_rows: list[dict[str, Any]] = []
        with connect() as conn:
            for page in ctx.pages:
                page_id = page["id"]
                row = rows[page_id]
                upsert_imaging_final(conn, chart_id=chart_id, page_id=page_id, row=row)
                if page_id in ctx.todo:
                    mark_completed(conn, ctx, page_id)
                csv_rows.append(
                    {
                        "chart_name": ctx.chart_name,
                        "page_name": page["page_name"],
                        "page_number": page.get("page_number"),
                        **{k: "" if v is None else v for k, v in row.items()},
                    }
                )

        path = write_csv(imaging_csv(ctx.chart_name, "final"), FINAL_COLS, csv_rows)
        changed = sum(1 for r in csv_rows if r["page_type_source"] in ("continuation", "embedded"))
        logger.info(
            "imaging_final chart=%s pages=%d page_type_changed_by_continuation=%d → %s",
            ctx.chart_name, len(csv_rows), changed, path,
        )
        return {
            "chart_id": chart_id,
            "final_csv": str(path),
            "pages_done": ctx.done,
            "page_type_changed": changed,
        }
