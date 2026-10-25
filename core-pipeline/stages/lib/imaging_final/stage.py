"""Stage: Final values → ``imaging_final`` and ``imaging/<chart>_final.csv``.

The last stage. It reads every stage's stored output and writes, per page, the
value a reviewer should take as final, so review-ui and reports read Finals
from one place instead of re-deriving them.

Most Finals are the page's own value. Three are decided across a document:

* **Page type** (with model type, codability): every page of a continuity
  document takes its first page's page type —
  ``page_classify.arbitration.decide_final`` (a confidence-weighted vote only
  when the first page is Unknown). The sub-type can differ: a page whose own
  type differs is re-placed under the document's L1 (a Lab Report page inside a
  Progress Note: Progress Note / Lab and Imaging Results; ``final_placement``).
  ``page_type_source`` is page / continuation / embedded, and
  ``continuation_rule`` names the rule.
* **Date of service**: the document's first dated page's; **encounter type**:
  its first page's (``continuity.engine.document_finals``; ``dos_source`` = document). A
  stand-alone document of non-visit page types takes DOS_DEFAULT_DATE.

Blank and junk pages belong to no document and keep their own values. A
duplicate page is skipped by every stage after blank/junk, so it takes the
content values of the page it duplicates (``COPIED_FROM_REFERENCE``):
``page_type_source`` / ``dos_source`` = duplicate and ``duplicate_of_page_id``
names that page.
"""
from __future__ import annotations

import json
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
    get_provider_signature_map,
    get_quality_map,
    get_sequencing_map,
    upsert_imaging_final,
)
from db.paths import imaging_csv, write_csv
from stages._support import mark_completed, stage_run
from stages.lib.continuity.engine import document_finals
from stages.lib.page_classify import axes
from stages.lib.page_classify.arbitration import FinalPage, decide_final
from stages.lib.page_classify.taxonomy import CATEGORY

logger = logging.getLogger(__name__)

STAGE = "imaging_final"

DEFAULT_SPECIALTY = "General"  # 2026-10-25 (user): never Unknown

FINAL_COLS = ["chart_name", "page_name", "page_number", *IMAGING_FINAL_COLUMNS]

# What a duplicate page takes from the page it duplicates. Every stage after
# blank/junk skips duplicates, so these are empty on the duplicate itself. It
# keeps its own image values (quality, orientation), its blank/junk flag and
# its position (``seq``).
COPIED_FROM_REFERENCE = (
    "member_name", "member_dob", "member_id",
    "document_seq",
    "page_type", "page_subtype", "specialty", "model_type", "codability", "classification_category",
    "continuation_rule", "needs_review",
    "dos_from", "dos_to",
    "encounter_type", "provider_name", "provider_credentials", "signers", "provider_version",
    "provider_confidence", "signature_present",
)


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, date):
        return value.isoformat()
    return str(value) if value else None


def _number(value: Any) -> Optional[float]:
    return None if value is None else float(value)


def _signatures(rows: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Provider name, credentials, signers, signature and rule version per page_id, from
    provider_signature_results (written by kv_extract; never the staging)."""
    out: dict[int, dict[str, Any]] = {}
    for page_id, row in rows.items():
        signers = row.get("signers")
        if isinstance(signers, str):
            try:
                signers = json.loads(signers)
            except ValueError:
                signers = None
        out[page_id] = {
            "signature_present": bool(row.get("signature_present")),
            "provider_name": row.get("provider_name") or None,
            "provider_credentials": row.get("provider_credentials") or None,
            "signers": signers or None,
            "provider_version": row.get("provider_version"),
            "provider_confidence": row.get("confidence"),
        }
    return out


def _documents(
    pages: list[dict[str, Any]],
    continuity: dict[int, dict[str, Any]],
    junk: frozenset[int] = frozenset(),
) -> dict[int, list[int]]:
    """Document number → its content page ids in order. Junk pages share the numbering
    (junk groups, and a single junk page inside a document) but keep their own values."""
    documents: dict[int, list[tuple[int, int]]] = {}
    for page in pages:
        row = continuity.get(page["id"])
        if page["id"] in junk:
            continue
        if row and row.get("document_seq") is not None:
            documents.setdefault(int(row["document_seq"]), []).append(
                (int(row.get("seq") or 0), page["id"])
            )
    return {doc: [pid for _, pid in sorted(members)] for doc, members in documents.items()}


def encounter_specialties(
    documents: dict[int, list[int]], classification: dict[int, dict[str, Any]]
) -> dict[int, str]:
    """Page id -> its encounter's specialty: only the first page of the document decides
    (2026-10-25, user), and every page takes it, so it never changes midway. A first
    page with none (or Unknown / Others) gives General."""
    out: dict[int, str] = {}
    for document_pages in documents.values():
        if not document_pages:
            continue
        first = axes.stored(classification.get(document_pages[0]) or {}).get("specialty")
        found = first if first and first not in ("Unknown", "Others") else DEFAULT_SPECIALTY
        for pid in document_pages:
            out[pid] = found
    return out


def _final_page(page_id: int, kind: dict[str, Any], link: dict[str, Any]) -> FinalPage:
    return FinalPage(
        page_id=page_id,
        page_type=kind.get("page_type"),
        page_subtype=kind.get("page_subtype"),
        family=axes.family_of(kind) or None,
        names=axes.names_of(kind),
        l2=axes.l2_of(kind),
        under_l1=axes.under_l1_of(kind),
        category=kind.get("classification_category"),
        codeability_rule=axes.stored(kind).get("codeability_rule"),
        title_l1=axes.title_of(kind),
        model_type=kind.get("model_type"),
        document_seq=link.get("document_seq"),
        seq=link.get("seq"),
        link_strength=link.get("link_strength"),
        confidence=_number(kind.get("confidence")),
    )


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, force=force) as ctx:
        with connect() as conn:
            quality = get_quality_map(conn, chart_id)
            provider_rows = get_provider_signature_map(conn, chart_id)
            blank_junk = get_blank_junk_final(conn, chart_id)
            classification = get_page_classification_map(conn, chart_id)
            dos = get_dos_map(conn, chart_id)
            members = get_member_extraction_map(conn, chart_id)
            encounters = get_encounter_map(conn, chart_id)
            sequencing = get_sequencing_map(conn, chart_id)
            continuity = get_continuity_map(conn, chart_id)
        signatures = _signatures(provider_rows)

        # Each page's own DOS, Extracted page type and encounter type come
        # first; continuity grouped the pages from them; then every page of a
        # document takes its first page's page type, DOS and encounter type.
        # Continuity never reads a Final value, so a Final cannot feed back
        # into the grouping.
        own: dict[int, dict[str, Any]] = {}
        for page in ctx.pages:
            page_dos = dos.get(page["id"]) or {}
            kind = classification.get(page["id"]) or {}
            own[page["id"]] = {
                "dos_from": _iso(page_dos.get("date_of_service_from")),
                "dos_to": _iso(page_dos.get("date_of_service_to")),
                # A stand-alone non-visit document takes the default DOS.
                "page_type": kind.get("page_type"),
                "page_subtype": kind.get("page_subtype"),
                "names": axes.names_of(kind),
                "encounter_type": (encounters.get(page["id"]) or {}).get("encounter_type"),
            }
        junk_pages = frozenset(
            pid for pid, row in blank_junk.items() if (row or {}).get("blank_junk_flag") in ("blank", "junk", "duplicate")
        )
        documents = _documents(ctx.pages, continuity, junk_pages)
        carried: dict[int, dict[str, Any]] = {}
        document_of: dict[int, list[int]] = {}
        for document_pages in documents.values():
            finals = document_finals([own[pid] for pid in document_pages])
            for pid in document_pages:
                carried[pid] = finals
                document_of[pid] = document_pages
        specialty_of = encounter_specialties(documents, classification)
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
            signature = signatures.get(page_id) or {}
            # The page type is agreed across the document; the sub-type stays the page's.
            verdict = decide_final(
                final_pages[page_id],
                [final_pages[pid] for pid in document_of.get(page_id, [])] or None,
            )
            # DOS and encounter type: the document's (first dated page / first page) when
            # the page belongs to one, else its own.
            document = carried.get(page_id)
            dates = document or own[page_id]
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
                "encounter_type": (document or own[page_id])["encounter_type"],
                "provider_name": signature.get("provider_name"),
                "provider_credentials": signature.get("provider_credentials"),
                "signers": signature.get("signers") or None,
                "provider_version": signature.get("provider_version"),
                "provider_confidence": _number(signature.get("provider_confidence")),
                "signature_present": signature.get("signature_present") if signature else None,
                "seq": (sequencing.get(page_id) or {}).get("seq"),
                "duplicate_of_page_id": None,
                # The page's own, like the sub-type; not carried across the document.
                # Unknown on a classified page the axes gave no specialty.
                "specialty": (
                    specialty_of.get(page_id) if page_id in specialty_of else axes.stored(kind).get("specialty")
                ) or (DEFAULT_SPECIALTY if kind else None),
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
                        **{
                            k: "" if v is None else json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
                            for k, v in row.items()
                        },
                    }
                )

        path = write_csv(imaging_csv(ctx.chart_name, "final"), FINAL_COLS, csv_rows)
        changed = sum(
            1 for r in csv_rows
            if r["page_type_source"] in ("continuation", "embedded")
        )
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
