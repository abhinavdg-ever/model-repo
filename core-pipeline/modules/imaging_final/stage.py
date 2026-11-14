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
names that page. A copied page continuity's duplicate check made a Main page again
("duplicate content") fills the values it has none of from that page.

Confidences (2026-11-14, docs/specials/CONFIDENCE_LOGIC.md section 12, ``final_confidences``):
the anchor page keeps its own; a value carried from it reads anchor x 0.90; a page rules 7 / 8
re-placed reads flipped; member name / DOB / ID are three keys.
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import date
from typing import Any, Optional

from common.db import (
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
from common.db.paths import imaging_csv, write_csv
from apps.worker.stages._support import (
    INVOICE_DECIDED_BY, INVOICE_PAGE_SUBTYPE, INVOICE_PAGE_TYPE, blank_junk_weights, mark_completed, stage_run,
)
from modules import confidence as conf
from modules.continuity.engine import document_finals
from modules.page_classify import axes
from modules.page_classify.arbitration import FinalPage, decide_final, note_sections, packets, path_of
from modules.non_codable_credentials import only_non_codable
from modules.page_classify.taxonomy import CATEGORY

logger = logging.getLogger(__name__)

STAGE = "imaging_final"

DEFAULT_SPECIALTY = "General"  # 2026-10-25 (user): never Unknown
# 2026-11-01 (user): a specialty is given only to a page whose Final L1 is one of these;
# every other page has none.
SPECIALTY_FAMILIES = frozenset({
    "Progress Note", "Lab Report", "Discharge Summary", "Operative Note", "Procedure Note",
})


def _document_signature(page_ids, signatures: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """Every credential of a document's pages as one signature row (for only_non_codable)."""
    credentials, signers = [], []
    for pid in page_ids:
        row = signatures.get(pid) or {}
        if row.get("provider_credentials"):
            credentials.append(str(row["provider_credentials"]))
        signers.extend(s for s in row.get("signers") or [] if isinstance(s, dict))
    return {"provider_credentials": ", ".join(credentials), "signers": signers}


def has_specialty(page_type: Optional[str], page_subtype: Optional[str] = None) -> bool:
    """True when the Final page type's L1 is one that carries a specialty."""
    return path_of(page_type, page_subtype)[0] in SPECIALTY_FAMILIES

# The reason a "Copy of N" page carries (blank / junk, or continuity's date-of-service block:
# modules/continuity/duplicates.py CONTENT_MARK, DOS_NOTE).
CONTENT_MARK = "duplicate_content_of_page_id:"
DOS_NOTE = "blocked by date of service"

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


def _visit_setting(setting: Any) -> Optional[str]:
    """The encounter type Final shows: blank for a document encounter type does not apply
    to (2026-11-02, user: only Progress, Procedure and Operative Notes carry one)."""
    return None if not setting or setting == "not_applicable" else setting


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
        from modules.names import trim_provider_name, trim_signers

        out[page_id] = {
            "signature_present": bool(row.get("signature_present")),
            # Without stray marks at either end ("→ Sameera T Raza"), 2026-10-28.
            # Role words out ("Legal Authenticator", "Signing Authority"), 2026-11-01.
            "provider_name": trim_provider_name(row.get("provider_name")) or None,
            "provider_credentials": row.get("provider_credentials") or None,
            "signers": trim_signers(signers) or None,
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


MEMBER_CONFIDENCE_KEYS = {"name": "member_name", "dob": "member_dob", "member_id": "member_id"}


def _own_or_carried(page_id: int, anchor: Optional[int], value: Optional[float]) -> Optional[float]:
    """The anchor page keeps its own confidence; a page that took the value from it carries
    anchor x ``carried_factor`` (2026-11-14, user: CONFIDENCE_LOGIC.md 12.1 rule 6)."""
    if value is None:
        return None
    return value if anchor is None or anchor == page_id else conf.carried(value)


def final_confidences(
    page_id: int,
    *,
    page_type: Optional[str],
    decider: int,
    type_pages: list[int],
    specialty_page: Optional[int],
    dos_page: Optional[int],
    encounter_page: int,
    classification: dict[int, dict[str, Any]],
    dos: dict[int, dict[str, Any]],
    encounters: dict[int, dict[str, Any]],
    members: dict[int, dict[str, Any]],
    provider_confidence: Optional[float],
    blank_junk: dict[int, dict[str, Any]],
    quality: dict[int, dict[str, Any]],
    replaced: bool = False,
    member_fields: Optional[dict[str, Optional[float]]] = None,
) -> dict[str, float]:
    """Each Final value's confidence (2026-11-14, user; CONFIDENCE_LOGIC.md section 12),
    from the page that decided it (the anchor): the anchor keeps its own; every other page
    of the document carries the anchor's x ``carried_factor`` (0.90). Page type and
    codability from the page whose type the document took (``decider``); a page rules 7 / 8
    re-placed under the document's L1 (``replaced``: a lab page inside a Progress Note) reads
    ``confidence.flipped``. Specialty and encounter type from the encounter's first page; DOS
    from the document's dated page (none for a default date). Member name, DOB and member ID
    each their own (``member_fields``; a row without them: the page's single member
    confidence for each value it has). Provider, blank / junk and handwriting from the page
    itself (blank / junk: every page's verdict, Main too). Values with no confidence are
    left out."""
    out: dict[str, Optional[float]] = {}
    if page_type:
        if replaced:
            out["page_type"] = conf.flipped()
        else:
            out["page_type"] = _own_or_carried(
                page_id, decider, _number((classification.get(decider) or {}).get("confidence")))
        # Codability is a lookup from the page type: the same confidence.
        out["codability"] = out["page_type"]
    if specialty_page is not None:
        out["specialty"] = _own_or_carried(
            page_id, specialty_page,
            _number(axes.stored(classification.get(specialty_page) or {}).get("specialty_confidence")))
    if dos_page is not None:
        out["dos"] = _own_or_carried(page_id, dos_page, _number((dos.get(dos_page) or {}).get("confidence")))
    encounter = encounters.get(encounter_page) or {}
    out["encounter_type"] = (
        _own_or_carried(page_id, encounter_page, _number(encounter.get("confidence")))
        if _visit_setting(encounter.get("encounter_type")) else None
    )
    member = members.get(page_id) or {}
    if member_fields is None:
        # No extraction staging to read the fields' own confidences from: the row's one value
        # for each identifier it has.
        single = _number(member.get("confidence"))
        member_fields = {
            field: single if member.get(column) not in (None, "") else None
            for field, column in (("name", "extracted_name"), ("dob", "extracted_dob"),
                                  ("member_id", "extracted_member_id"))
        }
    for field, key in MEMBER_CONFIDENCE_KEYS.items():
        out[key] = member_fields.get(field)
    out["provider"] = provider_confidence
    # Every page's verdict, Main included: a page kept Main on a weak verdict is flagged too.
    out["blank_junk"] = _number((blank_junk.get(page_id) or {}).get("confidence"))
    out["handwriting"] = _number((quality.get(page_id) or {}).get("hw_confidence"))
    return {k: round(v, 4) for k, v in out.items() if v is not None}


def _anchor(type_pages: list[int], page_type: Optional[str], page_subtype: Optional[str],
            classification: dict[int, dict[str, Any]]) -> int:
    """The page the document's Final page type came from: its first page of that L1."""
    l1 = path_of(page_type, page_subtype)[0]
    return next((pid for pid in type_pages if axes.family_of(classification.get(pid) or {}) == l1), type_pages[0])


def review_fields(confidences: dict[str, float]) -> list[str]:
    """The Final values below the review line."""
    return sorted(k for k, v in confidences.items() if conf.needs_review(v))


def page_texts(conn: Any, chart_id: int, pages: list[dict[str, Any]], quality: dict[int, Any]) -> dict[int, str]:
    """Each page's best OCR text (Final 2, Final 1, then Tesseract), for note_sections."""
    from common.db import get_ocr_texts
    from apps.worker.stages._support import best_page_text

    prelim = get_ocr_texts(conn, chart_id, "tesseract")
    final1 = get_ocr_texts(conn, chart_id, "docling")
    final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
    return {
        page["id"]: best_page_text(final2=final2.get(page["id"]), final1=final1.get(page["id"]),
                                   prelim=prelim.get(page["id"]), quality_row=quality.get(page["id"]))
        for page in pages
    }


def _final_page(page_id: int, kind: dict[str, Any], link: dict[str, Any], *,
                text: Optional[str] = None, signed: bool = False, weight: Optional[float] = None) -> FinalPage:
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
        # The pre-2026-11-14 confidence: the vote weight (no decision changes with the new one).
        confidence=weight if weight is not None else axes.decision_confidence(kind),
        sections=note_sections(text),
        signed=signed,
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
            texts = page_texts(conn, chart_id, ctx.pages, quality)
        signatures = _signatures(provider_rows)
        from modules.extraction import staging as extraction_staging
        from modules.member.stage import member_field_confidences

        # Member name / DOB / member ID confidences from the extraction rows they came from.
        staged = extraction_staging.read(ctx.chart_name)
        # 2026-11-02 (user): a page junked after page type / DOS / member ran on it (continuity's
        # short page contained in a nearby page) shows what any blank / junk page shows:
        # none of those stages' values.
        # 2026-11-14 (user, "for now"): the member and provider extraction stay on blank / junk
        # pages (pass 1 and 2) -- kv_extract reads them -- so they are shown (was cleared
        # 2026-11-02 / 2026-11-06). Page type, DOS and encounter type are still cleared.
        for pid in [pid for pid, row in blank_junk.items() if (row or {}).get("blank_junk_flag") in ("blank", "junk")]:
            for values in (classification, dos, encounters):
                values.pop(pid, None)

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
                "encounter_type": _visit_setting((encounters.get(page["id"]) or {}).get("encounter_type")),
            }
        junk_pages = frozenset(
            pid for pid, row in blank_junk.items() if (row or {}).get("blank_junk_flag") in ("blank", "junk", "duplicate")
        )
        documents = _documents(ctx.pages, continuity, junk_pages)
        carried: dict[int, dict[str, Any]] = {}
        for document_pages in documents.values():
            finals = document_finals([own[pid] for pid in document_pages])
            for pid in document_pages:
                carried[pid] = finals
        specialty_of = encounter_specialties(documents, classification)
        # An Invoice page's vote weight is its blank / junk verdict's older number (as before).
        junk_weights = blank_junk_weights(ctx.chart_name)
        final_pages = {
            page["id"]: _final_page(
                page["id"], classification.get(page["id"]) or {}, continuity.get(page["id"]) or {},
                text=texts.get(page["id"]), signed=bool((signatures.get(page["id"]) or {}).get("signature_present")),
                weight=junk_weights.get(page["page_name"])
                if (classification.get(page["id"]) or {}).get("decided_by") == INVOICE_DECIDED_BY else None,
            )
            for page in ctx.pages
        }
        # The page type's documents: continuity's, with short packets decided by their
        # last page joined (a consent's notice pages and its signed page). DOS and
        # encounter type keep continuity's documents.
        type_document_of: dict[int, list[int]] = {}
        for packet in packets([[final_pages[pid] for pid in pages] for _, pages in sorted(documents.items())]):
            for p in packet:
                type_document_of[p.page_id] = [q.page_id for q in packet]

        document_of = {pid: pages for pages in documents.values() for pid in pages}
        rows: dict[int, dict[str, Any]] = {}
        non_codable_pages = 0
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
                [final_pages[pid] for pid in type_document_of.get(page_id, [])] or None,
            )
            if kind.get("decided_by") == INVOICE_DECIDED_BY:
                # 2026-11-05 (user): an invoice blank / junk kept stays Invoice / Invoice Page.
                verdict = replace(verdict, page_type=INVOICE_PAGE_TYPE, page_subtype=INVOICE_PAGE_SUBTYPE,
                                  codability="Non-Codable", source="page", continuation_rule="invoice_page",
                                  needs_review=False)
            # DOS and encounter type: the document's (first dated page / first page) when
            # the page belongs to one, else its own.
            document = carried.get(page_id)
            dates = document or own[page_id]
            dos_origin = "document" if document else "page"
            with_specialty = has_specialty(verdict.page_type, verdict.page_subtype)
            # 2026-11-01 (user): a Codable page is Non-Codable when every provider credential
            # in its *document* is a non-codable one (RN, LPN, MA, PharmD ...): one nurse's
            # line on a page of a physician's note does not change that page.
            codability = verdict.codability
            downgraded = codability == "Codable" and only_non_codable(
                _document_signature(document_of.get(page_id) or [page_id], signatures)
            ) is not None
            if downgraded:
                codability = "Non-Codable"
                non_codable_pages += 1
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
                "codability": codability,
                "classification_category": (
                    CATEGORY.get(codability or "")
                    or kind.get("classification_category")
                ),
                "page_type_source": verdict.source if verdict.page_type or verdict.page_subtype else None,
                "continuation_rule": verdict.continuation_rule,
                # Codability is the Final type's (verdict); the Extracted row's flag counts
                # only for a page type nobody was sure of, not for its own codability.
                "needs_review": bool(verdict.needs_review or (
                    kind.get("needs_review") and axes.decided_by_of(kind) in (None, axes.BY_OTHERS))),
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
                    (specialty_of.get(page_id) if page_id in specialty_of else axes.stored(kind).get("specialty"))
                    or (DEFAULT_SPECIALTY if kind else None)
                ) if with_specialty else None,
            }
            in_document = document_of.get(page_id) or [page_id]
            type_pages = type_document_of.get(page_id) or [page_id]
            dated = (
                None if document and document.get("dos_default")
                else next((pid for pid in in_document if own[pid]["dos_from"] == dates["dos_from"]), page_id)
                if dates["dos_from"] else None
            )
            row["confidences"] = final_confidences(
                page_id,
                page_type=verdict.page_type,
                decider=page_id if verdict.continuation_rule != "agreed" else _anchor(
                    type_pages, verdict.page_type, verdict.page_subtype, classification),
                replaced=verdict.source in ("continuation", "embedded"),
                member_fields=(
                    member_field_confidences(member, staged.page(page["page_name"])) if staged is not None and member
                    else None
                ),
                type_pages=type_pages,
                specialty_page=in_document[0] if kind and with_specialty else None,
                dos_page=dated,
                encounter_page=in_document[0],
                classification=classification,
                dos=dos,
                encounters=encounters,
                members=members,
                provider_confidence=_number(signature.get("provider_confidence")),
                blank_junk=blank_junk,
                quality=quality,
            )
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
            # The content values' confidences are the original's; its own verdict and
            # image values stay its own.
            own_values = {k: v for k, v in (row.get("confidences") or {}).items() if k in ("blank_junk", "handwriting")}
            row["confidences"] = {**(reference.get("confidences") or {}), **own_values}

        # A "Copy of N" page (2026-11-11): a Main page; the values it has none of (a copy the
        # date-of-service block stopped was skipped by the stages after blank / junk) are its
        # original's.
        for page_id, row in rows.items():
            final = blank_junk.get(page_id) or {}
            original = final.get("duplicate_of_page_id")
            if row["blank_junk_flag"] == "duplicate" or original not in rows:
                continue
            if CONTENT_MARK not in str(final.get("reason") or ""):
                continue
            reference = rows[original]
            # A copy the date-of-service block stopped (continuity, 2026-11-11) keeps its own
            # document's DOS, never its original's.
            dos_blocked = DOS_NOTE in str(final.get("reason") or "")
            for key in COPIED_FROM_REFERENCE:
                if dos_blocked and key in ("dos_from", "dos_to"):
                    continue
                if row.get(key) in (None, "", []) and key != "document_seq":
                    row[key] = reference[key]
            row["duplicate_of_page_id"] = original
            # Not a duplicate: the page-type row's duplicate_flag was written before the check.
            row["is_duplicate"] = False

        # Below the review line anywhere: the page needs review (2026-10-25, user).
        for row in rows.values():
            row["review_fields"] = review_fields(row.get("confidences") or {})
            row["needs_review"] = bool(row["needs_review"] or row["review_fields"])

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
                            k: "" if v is None else json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
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
            "imaging_final chart=%s pages=%d page_type_changed_by_continuation=%d "
            "non_codable_credentials=%d → %s",
            ctx.chart_name, len(csv_rows), changed, non_codable_pages, path,
        )
        return {
            "chart_id": chart_id,
            "final_csv": str(path),
            "pages_done": ctx.done,
            "page_type_changed": changed,
            "non_codable_by_credentials": non_codable_pages,
        }
