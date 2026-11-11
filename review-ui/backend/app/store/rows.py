"""Row -> response logic shared by the Postgres and Cosmos repositories.

Each function takes the rows a chart view needs, in the shape the Postgres queries return
them (tuples in SELECT order), and builds the response. The Postgres repository feeds them
query results; the Cosmos repository turns its documents into the same tuples first, so
both backends show a chart the same way.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from fastapi import HTTPException

from app.core.schemas import (
    FolderDetail,
    ImagingDocumentResponse,
    ImagingManifestDetails,
    ImagingPageResult,
    ImagingSectionsProcessed,
    ImagingVerificationDetails,
    OcrSectionHeader,
    OcrTextResponse,
    PageSummary,
    with_service_window,
)
from app.services.chart_run_batch import with_run_batch_default
from app.services.imaging_overlays import (
    compose_page_type,
    continuity_label,
    empty_imaging_pages,
    inherit_duplicate_blank_junk,
    page_type_fields,
    signer_fields,
    subtype_confidence,
)
from app.store.shared import _fmt_dos_display, _normalize_kind

logger = logging.getLogger("review_ui.postgres")

# API kind → ocr_results.ocr_type
KIND_TO_OCR_TYPE: dict[str, str] = {
    "preliminary": "tesseract",
    "final1": "docling",
    "final2": "azuredocintel",
}

# schema v7 lifecycle values.
CHART_STATUS_TO_OCR: dict[str, str] = {
    "received": "QUEUED",
    "downloading": "IN_PROGRESS",
    "processing": "IN_PROGRESS",
    "completed": "IMAGING_COMPLETED",
    "failed": "FAILED",
    "needs_review": "IMAGING_COMPLETED",
    "rejected": "IMAGING_COMPLETED",
    # v6 packed the stage name into status. Kept so a database that has not run
    # migration 002 yet still renders sensibly.
    "ocr_prelim": "IN_PROGRESS",
    "ocr_quality": "IN_PROGRESS",
    "blank_junk": "IMAGING_IN_PROGRESS",
    "ocr_final1": "IN_PROGRESS",
    "ocr_final2": "IN_PROGRESS",
    "section_headers": "IMAGING_IN_PROGRESS",
    "kv_extract": "IMAGING_IN_PROGRESS",
    "member_verify": "IMAGING_IN_PROGRESS",
    "dos_extract": "IMAGING_IN_PROGRESS",
    "page_subtype": "IMAGING_IN_PROGRESS",
    "encounter_type": "IMAGING_IN_PROGRESS",
    "page_sequencing": "IMAGING_IN_PROGRESS",
}

# v7: once status is just "processing", which stage it is in comes from
# current_stage. These stages mean the imaging modules are running.
IMAGING_STAGES = frozenset(
    {
        "blank_junk",
        "section_headers",
        "kv_extract",
        "member_verify",
        "dos_extract",
        "page_subtype",
        "continuity",
        "encounter_type",
        "page_sequencing",
        "imaging_final",
    }
)

# A chart is Imaging Completed only when every stage in pipeline_stage is done
# for every page — chart_list.status 'completed' / 'needs_review' / 'rejected'.
IMAGING_DONE_STATUSES = frozenset({"completed", "needs_review", "rejected"})

# The reason a copied page carries when its encounter is not a copy (core-pipeline's
# modules/continuity/duplicates.py writes it).
DUPLICATE_CONTENT_MARK = "duplicate_content_of_page_id:"

# The imaging overlay's sections, in the order they are applied.
OVERLAY_SECTIONS = (
    "quality",
    "blank_junk",
    "member",
    "dos",
    "page_classification",
    "encounter_type",
    "sequencing",
    "continuity",
    "provider_signature",
    "imaging_final",
)

# Which imaging sections have any result rows (see section_flags).
SECTION_FLAG_KEYS = (
    "member", "dos", "hw", "quality", "rotation", "junk", "codeable", "encounter", "sequencing",
)

_CODEABLE_DISPLAY = {
    "codeable": "Codable",
    "non_codeable": "Non-Codable",
    "discharge_summary": "Discharge",
    "discharge_frequency": "Discharge",
    "not_sure": "Not Sure",
}

_ENCOUNTER_DISPLAY = {
    "outpatient_f2f": "Outpatient (F2F)",
    "outpatient_tele": "Outpatient (Tele)",
    "inpatient": "Inpatient",
    "home": "Home",
    "not_applicable": "",  # 2026-11-02 (user): not a visit document -> blank
}

_BLANK_JUNK_SKIPS = {"blank_junk_pass1", "blank_junk", "blank_junk_pass2"}


def _fmt_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    return _fmt_dos_display(value)


def _blank_junk_ui(flag: str | None, subtype: str | None) -> tuple[str | None, bool | None, str | None]:
    """Map DB blank_junk_flag → UI blankOrJunk / isDuplicate / pageType."""
    if not flag:
        return None, None, None
    f = flag.strip().lower()
    if f == "blank":
        return "Yes (Blank)", False, "Not Available"
    if f == "duplicate":
        # A duplicate is not junk (2026-10-29, user): Blank / Junk No, Duplicate Yes.
        return "No", True, "Not Available"
    if f == "junk":
        return "Yes (Junk)", False, subtype or "Junk"
    if f == "not_blank_junk":
        return "No", False, "Not Available"
    return None, None, None


def json_text(value: Any) -> str | None:
    """A JSON value as Postgres ``->>`` returns it: strings as they are, booleans as
    'true' / 'false', numbers and objects as their JSON text."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value)


@contextmanager
def _skip_on_error(name: str, folder_id: str) -> Iterator[None]:
    """One part of a chart's overlay: a failure while applying its rows skips that part
    with a warning; the parts after it still load."""
    try:
        yield
    except Exception as exc:
        logger.warning("imaging overlay %s skipped for %s: %s", name, folder_id, exc)


# ---------------------------------------------------------------------------
# get_folder
# ---------------------------------------------------------------------------

def page_list(page_rows: Sequence[tuple]) -> list[tuple[int, str]]:
    """(page_number, page_name) rows, already ordered → viewer pages (a missing number is
    the page's position)."""
    return [(int(num or idx), str(name)) for idx, (num, name) in enumerate(page_rows, start=1)]


def folder_detail(
    folder_id: str,
    chart_row: tuple | None,
    page_rows: Sequence[tuple],
    flag_rows: Sequence[tuple],
    imaging_done: set[str],
    manifest_row: tuple | None,
) -> FolderDetail | None:
    """FolderDetail from: chart (id, status, current_stage, run_id, batch_id); pages
    (page_number, page_name) ordered; OCR flags (page_name, ocr_type) for non-empty text;
    page names done in every stage; manifest (member_name, member_dob, external_member_id)."""
    if not chart_row or not page_rows:
        return None
    _chart_id, status_raw, stage_raw, run_id, batch_id = chart_row
    pages = page_list(page_rows)

    flags: dict[str, dict[str, bool]] = {}
    for page_name, ocr_type in flag_rows:
        flags.setdefault(str(page_name), {})[str(ocr_type)] = True

    page_summaries = [
        PageSummary(
            page_number=num,
            filename=name,
            image_url=f"/api/folders/{folder_id}/pages/{num}/image",
            has_preliminary_ocr=bool(flags.get(name, {}).get("tesseract")),
            has_final1_ocr=bool(flags.get(name, {}).get("docling")),
            has_final2_ocr=bool(flags.get(name, {}).get("azuredocintel")),
            has_imaging=name in imaging_done
            or str(status_raw or "").lower() in IMAGING_DONE_STATUSES,
        )
        for num, name in pages
    ]
    kinds_present = {k for page_flags in flags.values() for k, ok in page_flags.items() if ok}
    ocr_processed = sum(1 for k in ("tesseract", "docling", "azuredocintel") if k in kinds_present)

    status = str(status_raw or "").lower()
    stage = str(stage_raw or "").lower()
    chart_status = stage if status == "processing" and stage else (status or None)
    ocr_status = CHART_STATUS_TO_OCR.get(str(chart_status or "").lower(), "QUEUED")
    if ocr_status == "IN_PROGRESS" and ocr_processed == 3:
        ocr_status = "IMAGING_IN_PROGRESS"
    elif ocr_status == "QUEUED" and ocr_processed > 0:
        ocr_status = "IN_PROGRESS"

    manifest = None
    if manifest_row:
        name, dob, member_id = manifest_row
        manifest = ImagingManifestDetails(member=name, dob=_fmt_date(dob), memberId=member_id)

    shown_run, shown_batch = with_run_batch_default(
        str(run_id) if run_id else None,
        str(batch_id) if batch_id else None,
    )
    return FolderDetail(
        id=folder_id,
        name=folder_id,
        page_count=len(pages),
        ocr_processed=ocr_processed,
        imaging_processed=sum(1 for p in page_summaries if p.has_imaging),
        ocr_status=ocr_status,  # type: ignore[arg-type]
        last_updated_at=None,
        run_id=shown_run,
        batch_id=shown_batch,
        manifest=manifest,
        pages=page_summaries,
    )


# ---------------------------------------------------------------------------
# get_ocr_text
# ---------------------------------------------------------------------------

def ocr_text(folder_id: str, kind: str, rows: Sequence[tuple]) -> OcrTextResponse:
    """Assemble OCR rows (page_name, raw_text, skip_reason), in page order, into
    ===== page ===== marker text for the UI. 404 when there are none."""
    from app.store.shared import (
        FINAL2_QUALITY_SKIP_MESSAGE,
        FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE,
        _filter_headers_against_canon,
        _section_headers_from_page,
        clean_ocr_display_text,
    )

    normalized = _normalize_kind(kind)
    ocr_type = KIND_TO_OCR_TYPE[normalized]
    final = ocr_type in {"azuredocintel", "docling"}
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No ocr_results for chart={folder_id!r} ocr_type={ocr_type!r}",
        )

    chunks: list[str] = []
    headers_by_file: dict[str, list[OcrSectionHeader]] = {}
    for page_name, raw_text, skip_reason in rows:
        body = (raw_text or "").strip()
        parsed: dict[str, Any] | None = None
        if final and body.startswith("{"):
            try:
                maybe = json.loads(body)
                if isinstance(maybe, dict) and ("content" in maybe or "markdown" in maybe):
                    parsed = maybe
                    body = str(maybe.get("content") or maybe.get("markdown") or "").strip()
            except Exception:
                pass
        if final and not body and parsed is not None:
            reason = str(parsed.get("skippedReason") or parsed.get("skipped_reason") or "").strip().lower()
            if reason == "high_quality_printed":
                body = FINAL2_QUALITY_SKIP_MESSAGE
            elif reason in _BLANK_JUNK_SKIPS:
                body = FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE
        skip = str(skip_reason or "").strip().lower()
        if ocr_type == "azuredocintel" and not body and skip == "high_quality_printed":
            body = FINAL2_QUALITY_SKIP_MESSAGE
        if final and not body and skip in _BLANK_JUNK_SKIPS:
            body = FINAL_OCR_BLANK_JUNK_SKIP_MESSAGE
        if body and final and not body.startswith("Skipped for "):
            body = clean_ocr_display_text(body)
        if final and parsed is not None:
            headers = _filter_headers_against_canon(_section_headers_from_page(parsed))
            if headers:
                headers_by_file[str(page_name)] = headers
                headers_by_file[str(page_name).lower()] = headers
        chunks.append(f"===== {page_name} =====\n{body}".rstrip())

    if ocr_type == "azuredocintel" and not any(
        (c.split("\n", 1)[-1] if "\n" in c else "").strip() for c in chunks
    ):
        raise HTTPException(
            status_code=404,
            detail=f"No ocr_results for chart={folder_id!r} ocr_type={ocr_type!r}",
        )

    return OcrTextResponse(
        folder_id=folder_id,
        kind=normalized,
        text="\n\n".join(chunks),
        section_headers_by_file=headers_by_file,
    )


# ---------------------------------------------------------------------------
# get_imaging
# ---------------------------------------------------------------------------

def manifest_details(row: tuple | None) -> ImagingManifestDetails | None:
    """(member_name, member_dob, external_member_id, measure, service_note) → details."""
    if not row:
        return None
    name, dob, member_id, measure, service_note = row
    return with_service_window(ImagingManifestDetails(
        member=name,
        dob=_fmt_date(dob),
        memberId=member_id,
        measure=measure,
        serviceNote=service_note,
    ))


def verification_details(row: tuple | None) -> ImagingVerificationDetails | None:
    """(final_status, matched_name, external_member_id, confidence, pages_matched,
    pages_checked, decision_reason) → details."""
    if not row:
        return None
    return ImagingVerificationDetails(
        finalStatus=row[0],
        matchedName=row[1],
        matchedMemberId=row[2],
        matchedConfidence=float(row[3]) if row[3] is not None else None,
        pagesMatched=row[4],
        pagesChecked=row[5],
        decisionReason=row[6],
    )


def page_imaging_fields(folder_id: str, rows: dict[str, list[tuple] | None]) -> dict[str, dict[str, Any]]:
    """page_name → field dict from the overlay sections' rows (each section's tuple is its
    _OVERLAY_SQL SELECT list; None or [] = no rows)."""
    by_page: dict[str, dict[str, Any]] = {}

    def _ensure(name: str) -> dict[str, Any]:
        return by_page.setdefault(name, {})

    try:
        with _skip_on_error("quality", folder_id):
            for (
                page_name, hw, conf, qscore, qtag, orient, tilt, mirrored,
                doc_type, visible, hw_area, measured,
            ) in rows.get("quality") or []:
                fields = _ensure(str(page_name))
                if hw:
                    label = str(hw).strip().lower()
                    if "hand" in label:
                        fields["handwrittenOrPrinted"] = "Handwritten"
                    elif "uncertain" in label:
                        fields["handwrittenOrPrinted"] = "Uncertain"
                    elif "mix" in label:
                        fields["handwrittenOrPrinted"] = "Mixed"
                    else:
                        fields["handwrittenOrPrinted"] = "Printed"
                if conf is not None:
                    fields["handwrittenOrPrintedConfidence"] = float(conf)
                if qscore is not None:
                    fields["pageQualityConfidence"] = float(qscore)
                if qtag:
                    fields["pageQualityTag"] = str(qtag)
                if orient is not None:
                    fields["orientationAngle"] = float(orient)
                if tilt is not None:
                    fields["tiltAngle"] = float(tilt)
                if mirrored is not None:
                    fields["mirrored"] = bool(mirrored)
                fields.update(page_type_fields(doc_type, visible, hw_area))
                if isinstance(measured, dict) and measured:
                    fields["measuredConfidences"] = {
                        k: float(v) for k, v in measured.items() if isinstance(v, (int, float))
                    }

        # 2026-11-02 (user): a blank / junk page shows only what blank / junk reports, even
        # when a later stage wrote a row for it (continuity junks a short page contained in a
        # nearby page after page type, DOS and member ran on it).
        junk_pages: set[str] = set()
        with _skip_on_error("blank_junk", folder_id):
            # Blank/junk — v7 stamps exactly one final row per page,
            # so precedence is not re-derived here any more.
            for page_name, flag, subtype, conf, duplicate_of, best, reason in rows.get("blank_junk") or []:
                fields = _ensure(str(page_name))
                if (flag or "").strip().lower() in ("blank", "junk"):
                    junk_pages.add(str(page_name))
                blank, dup, page_type = _blank_junk_ui(flag, subtype)
                fields["blankOrJunk"] = blank
                fields["isDuplicate"] = dup
                pointing = (flag or "").strip().lower() in ("not_blank_junk", "junk") and bool(duplicate_of)
                # "Copy of N" (2026-11-11, user): a 100% text match a hard block kept from
                # being a duplicate; a Main page. There are no Partials any more.
                fields["isDuplicateContent"] = pointing and DUPLICATE_CONTENT_MARK in (reason or "")
                fields["isPartialDuplicate"] = False
                fields["pageType"] = page_type
                if duplicate_of:
                    fields["duplicateOf"] = str(duplicate_of)
                if conf is not None:
                    fields["pageTypeConfidence"] = float(conf)
                    fields["blankJunkConfidence"] = float(conf)
                if best is not None:
                    fields["duplicateConfidence"] = float(best)

        with _skip_on_error("member", folder_id):
            for page_name, name, dob, mid, conf, member_status, other_names in rows.get("member") or []:
                # Blank / junk pages are read too since 2026-11-04 (user): shown, not verified.
                if not page_name:
                    continue
                fields = _ensure(str(page_name))
                fields["memberName"] = name
                fields["memberDob"] = _fmt_date(dob)
                fields["memberId"] = mid
                if conf is not None:
                    fields["memberConfidence"] = float(conf)
                fields["memberPageStatus"] = member_status
                if isinstance(other_names, list):
                    fields["memberOtherNames"] = [str(n) for n in other_names if str(n).strip()]

        with _skip_on_error("dos", folder_id):
            # DOS (Extracted): the DOS stage's own row per page, the same values it
            # writes to <chart>_dos.csv.
            for page_name, d_from, d_to, doc_from, doc_to, conf in rows.get("dos") or []:
                if str(page_name) in junk_pages:
                    continue
                fields = _ensure(str(page_name))
                fields["dosFrom"] = _fmt_date(d_from)
                fields["dosTo"] = _fmt_date(d_to)
                fields["docDosFrom"] = _fmt_date(doc_from)
                fields["docDosTo"] = _fmt_date(doc_to)
                if conf is not None:
                    fields["dosConfidence"] = float(conf)

        with _skip_on_error("page_classification", folder_id):
            # Codable / non-codable / discharge (page_classification)
            for page_name, subtype, cat, conf, page_type, specialty, specialty_conf, axes_json in (
                rows.get("page_classification") or []
            ):
                if str(page_name) in junk_pages:
                    continue
                fields = _ensure(str(page_name))
                if isinstance(axes_json, dict):
                    sub = subtype_confidence(axes_json.get("l2"), axes_json.get("l2_confidence"),
                                             axes_json.get("l3"), axes_json.get("l3_confidence"))
                    if sub is not None:
                        fields["subtypeConfidence"] = sub
                fields["specialty"] = specialty or "Unknown"
                if specialty_conf:
                    fields["specialtyConfidence"] = float(specialty_conf)
                key = str(cat or "").strip()
                shown = compose_page_type(page_type, subtype)
                if shown:
                    fields["pageType"] = shown
                fields["isCodeable"] = _CODEABLE_DISPLAY.get(key, key or "Not Sure")
                if conf is not None:
                    fields["pageTypeConfidence"] = float(conf)

        # Main pages with no page_classification row keep junk's
        # pageType="Not Available" — stamp Not Sure for codability.
        for fields in by_page.values():
            pt = str(fields.get("pageType") or "").strip()
            if pt == "Not Available" and not fields.get("isCodeable"):
                fields["isCodeable"] = "Not Sure"

        with _skip_on_error("encounter_type", folder_id):
            for page_name, et, conf in rows.get("encounter_type") or []:
                if str(page_name) in junk_pages:
                    continue
                fields = _ensure(str(page_name))
                key = str(et or "").strip()
                fields["encounterType"] = _ENCOUNTER_DISPLAY.get(key, key)
                if conf is not None:
                    fields["encounterConfidence"] = float(conf)

        with _skip_on_error("sequencing", folder_id):
            for page_name, page_num, orig, seq, conf in rows.get("sequencing") or []:
                fields = _ensure(str(page_name))
                current = orig if orig is not None else page_num
                if current is not None:
                    fields["currentSequence"] = int(current)
                if seq is not None:
                    fields["actualSequence"] = int(seq)
                if conf is not None:
                    fields["sequenceConfidence"] = float(conf)

        with _skip_on_error("continuity", folder_id):
            # Document continuity, then Final values. A database without
            # these tables yet skips them, not the page.
            if rows.get("continuity"):
                # The gap scorer's columns (2026-10-20) may not exist yet: read the
                # row as JSON so an older database still shows its documents.
                members: dict[int, list[dict[str, Any]]] = {}
                for page_name, k in rows.get("continuity") or []:
                    k = k or {}
                    document = k.get("document_seq")
                    position = k.get("position")
                    fields = _ensure(str(page_name))
                    fields["documentSeq"] = int(document) if document is not None else None
                    fields["documentPosition"] = position
                    # The engine's own label (2026-10-25), else rebuilt from position.
                    fields["documentLabel"] = k.get("label") or continuity_label(
                        position, int(document) if document is not None else None,
                        k.get("confidence_level"),
                    )
                    fields["continuityRelation"] = k.get("relation")
                    fields["continuityDecidedBy"] = k.get("decided_by")
                    fields["continuityEvidence"] = k.get("evidence")
                    fields["continuityConfidence"] = (
                        float(k["confidence"]) if k.get("confidence") is not None else None
                    )
                    if document is not None:
                        members.setdefault(int(document), []).append(fields)
                # Documents and junk groups share the numbering: a group whose every
                # page is junk reads 'New (Junk 4)'.
                for document, group in members.items():
                    if all(str(f.get("blankOrJunk") or "").lower().startswith("yes (junk)") for f in group):
                        for f in group:
                            f["documentLabel"] = (f.get("documentLabel") or "").replace(
                                f"(Doc {document})", f"(Junk {document})"
                            )

        with _skip_on_error("provider_signature", folder_id):
            # The extraction stage's signature rows; imaging_final, below, replaces them
            # with the document's values once it ran.
            for (page_name, provider, signed, credentials, signers, version,
                 provider_confidence) in rows.get("provider_signature") or []:
                fields = _ensure(str(page_name))
                people = signer_fields(signers, provider, credentials, version)
                fields.update({key: value for key, value in people.items() if value})
                if provider_confidence is not None:
                    fields["providerSignatureConfidence"] = float(provider_confidence)
                if signed is not None:
                    fields["providerSignature"] = "Yes" if signed else "No"

        with _skip_on_error("imaging_final", folder_id):
            for (page_name, final_type, category, final_from, final_to, final_sub,
                 final_specialty, provider, signed, credentials, signers, version,
                 provider_confidence, confidences, review) in rows.get("imaging_final") or []:
                fields = _ensure(str(page_name))
                if isinstance(confidences, dict):
                    fields["finalConfidences"] = {
                        k: float(v) for k, v in confidences.items() if isinstance(v, (int, float))
                    }
                if isinstance(review, list):
                    fields["reviewFields"] = [str(k) for k in review]
                # Provider Extraction: the name cell carries the credentials
                # ('Ramsey Luce, R.N.').
                people = signer_fields(signers, provider, credentials, version)
                fields.update({key: value for key, value in people.items() if value})
                if provider_confidence not in (None, ""):
                    fields["providerSignatureConfidence"] = float(provider_confidence)
                if signed is not None:
                    fields["providerSignature"] = "Yes" if signed == "true" else "No"
                fields["finalSpecialty"] = final_specialty or None
                fields["finalPageType"] = compose_page_type(final_type, final_sub) or None
                fields["finalCodeable"] = _CODEABLE_DISPLAY.get(category or "")
                fields["finalDosFrom"] = _fmt_date(final_from)
                fields["finalDosTo"] = _fmt_date(final_to)
    except Exception as exc:
        logger.warning("imaging overlay unavailable for %s: %s", folder_id, exc)
        return by_page

    return by_page


def imaging_document(
    folder_id: str,
    pages: Sequence[tuple[int, str]],
    db_fields: dict[str, dict[str, Any]],
    manifest: ImagingManifestDetails | None,
    verification: ImagingVerificationDetails | None,
    stage_flags: dict[str, bool],
    attach_truth: Callable[[list[ImagingPageResult]], list[ImagingPageResult]],
) -> ImagingDocumentResponse:
    """The imaging panel: the page skeleton with each page's overlay fields, ground truth
    (``attach_truth``), manifest, verification and which sections have run."""
    page_files = [(num, Path(name)) for num, name in pages]
    merged: list[ImagingPageResult] = []
    for page in empty_imaging_pages(page_files):
        hit = db_fields.get(page.fileName) or db_fields.get(page.fileName.lower())
        if not hit:
            stem = Path(page.fileName).stem
            for key, val in db_fields.items():
                if Path(key).stem == stem:
                    hit = val
                    break
        merged.append(page.model_copy(update=hit) if hit else page)
    merged = inherit_duplicate_blank_junk(merged)
    merged = attach_truth(merged)

    # "Processed" means the stage wrote rows — not that it found a value.
    # Using non-empty memberName/dosFrom here made empty-but-finished charts
    # show "Yet to Process" instead of "Not Found".
    sections = ImagingSectionsProcessed(
        member=stage_flags["member"] or verification is not None,
        dos=stage_flags["dos"],
        hw=stage_flags["hw"] or any(p.handwrittenOrPrinted for p in merged),
        quality=stage_flags["quality"]
        or any(p.pageQualityTag or p.pageQualityConfidence is not None for p in merged),
        rotation=stage_flags["rotation"]
        or any(p.orientationAngle is not None or p.tiltAngle is not None for p in merged),
        junk=stage_flags["junk"]
        or any(p.blankOrJunk is not None or p.isDuplicate is not None for p in merged),
        codeable=stage_flags["codeable"] or any(p.isCodeable is not None for p in merged),
        encounter=stage_flags["encounter"] or any(p.encounterType is not None for p in merged),
        sequencing=stage_flags["sequencing"] or any(p.actualSequence is not None for p in merged),
        verification=verification is not None,
    )

    return ImagingDocumentResponse(
        folder_id=folder_id,
        manifest=manifest or ImagingManifestDetails(),
        verification=verification,
        verifications=[verification] if verification else [],
        pages=merged,
        sectionsProcessed=sections,
    )


def empty_section_flags() -> dict[str, bool]:
    return {key: False for key in SECTION_FLAG_KEYS}


# ---------------------------------------------------------------------------
# page images
# ---------------------------------------------------------------------------

def page_locations(rows: Sequence[tuple]) -> dict[int, tuple]:
    """Location rows (blob_container, blob_path, output_path, page_name, page_number,
    use_corrected, image_path), in page order → page_number → (container, blob_path,
    output_path, page_name, use_corrected, image_path, ingest_index)."""
    by_num: dict[int, tuple] = {}
    for idx, (container, blob_path, output_path, page_name, db_num, use_corrected, image_path) in enumerate(
        rows, start=1
    ):
        num = int(db_num or idx)
        by_num.setdefault(
            num,
            (
                (container or "").strip(),
                blob_path,
                output_path,
                str(page_name),
                bool(use_corrected),
                str(image_path or ""),
                idx,
            ),
        )
    return by_num
