"""Algo output for Manual Review read straight from Postgres, many charts per
query.

Reading a chart through repo.get_imaging costs a dozen round trips (manifest,
verification, sections, ground truth, …) -- over a second a chart against a
remote database, minutes for a few hundred. Manual Review needs only the page
values, so one query fetches every page of a batch of charts. Values are
mapped as PostgresFolderRepository.get_imaging maps them (page type, "Not
Sure" codability, sequencing), so they match what the chart view shows.
"""

from __future__ import annotations

from datetime import datetime

from app.core.schemas import ImagingDocumentResponse, ImagingPageResult
from app.services.db import connection

# Charts fetched per query: big enough to cut round trips, small enough that
# the first rows reach the screen quickly.
BATCH = 25

_PAGES = r"""
SELECT c.chart_name,
       COALESCE(p.page_number,
                NULLIF(regexp_replace(split_part(p.page_name, '.', 1), '\D', '', 'g'), '')::int)
           AS page_number,
       p.page_name,
       m.extracted_name, m.extracted_dob, m.extracted_member_id,
       d.date_of_service_from, d.date_of_service_to,
       b.blank_junk_flag, b.junk_subtype,
       pc.id IS NOT NULL, pc.page_type, pc.page_subtype, pc.classification_category,
       f.page_type, f.page_subtype, f.classification_category, f.dos_from, f.dos_to,
       s.id IS NOT NULL, COALESCE(s.original_page_number, p.page_number), s.seq,
       f.provider_name, f.signature_present, to_jsonb(f) ->> 'provider_credentials',
       to_jsonb(f) -> 'signers', to_jsonb(f) ->> 'provider_version',
       to_jsonb(q) ->> 'document_type', q.printed_or_handwritten,
       b.duplicate_of_page_id IS NOT NULL
  FROM chart_list c
  JOIN page_list p ON p.chart_id = c.id
  LEFT JOIN member_extraction_results m ON m.page_id = p.id
  LEFT JOIN dos_extraction_results d ON d.page_id = p.id
  LEFT JOIN v_page_blank_junk_final b ON b.page_id = p.id
  LEFT JOIN page_classification pc ON pc.page_id = p.id
  LEFT JOIN imaging_final f ON f.page_id = p.id
  LEFT JOIN page_sequencing_results s ON s.page_id = p.id
  LEFT JOIN ocr_quality_results q ON q.page_id = p.id
 WHERE c.chart_name = ANY(%s)
 ORDER BY c.chart_name, 2
"""

_LAST_RUN = "SELECT chart_name, updated_at FROM chart_list WHERE chart_name = ANY(%s)"


def _page(row: tuple) -> ImagingPageResult:
    from app.adapters.postgres.repository import _blank_junk_ui, _fmt_date
    from app.services.accuracy_cache import _CODEABLE, _text
    from app.services.imaging_overlays import compose_page_type, page_type_fields, signer_fields

    (
        _chart, page_number, page_name,
        name, dob, member_id, dos_from, dos_to,
        flag, junk_subtype,
        classified, page_type, subtype, category,
        final_type, final_subtype, final_category, final_from, final_to,
        sequenced, current_seq, actual_seq,
        provider, signed, credentials, signers, version,
        document_type, printed_or_handwritten,
        names_original,
    ) = row
    # The signature rows' provider(s) (imaging_final), as the chart view shows them.
    people = signer_fields(signers, _text(provider), credentials, version)
    provider_name, provider_credentials = people.get("providerName"), people.get("providerCredentials")
    blank, duplicate, shown_type = _blank_junk_ui(_text(flag), _text(junk_subtype))
    codeable = None
    if classified:
        # A classified page shows its own type; blank category reads Not Sure.
        shown_type = compose_page_type(_text(page_type), _text(subtype)) or shown_type
        key = _text(category) or ""
        codeable = _CODEABLE.get(key, key or "Not Sure")
    if (shown_type or "").strip() == "Not Available" and not codeable:
        codeable = "Not Sure"  # a main page with no classification row
    final_key = _text(final_category) or ""
    return ImagingPageResult(
        pageNumber=int(page_number),
        fileName=_text(page_name) or f"{page_number}.jpg",
        memberName=_text(name),
        memberDob=_fmt_date(dob),
        memberId=_text(member_id),
        dosFrom=_fmt_date(dos_from),
        dosTo=_fmt_date(dos_to),
        blankOrJunk=blank,
        isDuplicate=duplicate,
        # A Main or junk page naming the page it nearly copies: a partial (95-98%) duplicate.
        isPartialDuplicate=_text(flag) in ("not_blank_junk", "junk") and bool(names_original),
        pageType=shown_type,
        isCodeable=codeable,
        currentSequence=int(current_seq) if sequenced and current_seq is not None else None,
        actualSequence=int(actual_seq) if sequenced and actual_seq is not None else None,
        finalPageType=compose_page_type(_text(final_type), _text(final_subtype)) or None,
        finalCodeable=_CODEABLE.get(final_key, final_key or None) if final_key else None,
        finalDosFrom=_fmt_date(final_from),
        finalDosTo=_fmt_date(final_to),
        providerName=provider_name or None,
        providerCredentials=provider_credentials or None,
        providerSignature=None if signed is None else ("Yes" if signed else "No"),
        # The chart view's Type: document_type (Printed / Handwritten / Form /
        # Visual / Blank), else the older printed_or_handwritten label.
        documentType=page_type_fields(document_type, None, None).get("documentType"),
        handwrittenOrPrinted=_hw_label(printed_or_handwritten),
    )


def _hw_label(value: object) -> str | None:
    """printed_or_handwritten as PostgresFolderRepository.get_imaging shows it."""
    label = str(value or "").strip().lower()
    if not label:
        return None
    if "hand" in label:
        return "Handwritten"
    if "uncertain" in label:
        return "Uncertain"
    if "mix" in label:
        return "Mixed"
    return "Printed"


def fetch(
    database_url: str, db_schema: str, chart_names: list[str]
) -> tuple[dict[str, ImagingDocumentResponse], dict[str, datetime | None]]:
    """(chart -> its pages as a slim document, chart -> last run) for charts in
    chart_list. A chart with no pages there is absent from the documents."""
    from app.services.imaging_overlays import inherit_duplicate_blank_junk

    pages: dict[str, list[ImagingPageResult]] = {}
    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.execute(_PAGES, (chart_names,))
        for row in cur.fetchall():
            if row[1] is None:
                continue  # no page number to match on
            pages.setdefault(str(row[0]), []).append(_page(row))
        cur.execute(_LAST_RUN, (chart_names,))
        last_run = {str(name): updated for name, updated in cur.fetchall()}
    documents = {
        chart: ImagingDocumentResponse.model_construct(
            folder_id=chart,
            pages=inherit_duplicate_blank_junk(chart_pages),
            verifications=[],
            verification=None,
        )
        for chart, chart_pages in pages.items()
    }
    return documents, last_run
