"""Read page_ground_truth for the landing flag and the imaging comparison."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app.core.schemas import ImagingPageResult, PageGroundTruth
from app.services.chart_run_batch import database_url_usable
from app.services.db import connection

logger = logging.getLogger("review_ui.ground_truth")

_COLUMNS = """
    page_number, source_page_id,
    member_name, member_dob, dos_from, dos_to,
    encounter_type, page_type, codeable,
    blank_page, junk_page, is_invoice, page_sequence, rotation,
    to_jsonb(page_ground_truth) ->> 'is_visible',
    to_jsonb(page_ground_truth) ->> 'rendering_provider',
    to_jsonb(page_ground_truth) ->> 'provider_signature',
    to_jsonb(page_ground_truth) ->> 'member_id'
"""

_EMPTY = {"", "na", "n/a", "not found", "not available"}


def spell_codeable(value: str | None) -> str | None:
    """Ground-truth sheets say Codable. Store and show Codeable."""
    text = clean_label(value)
    if text is None:
        return None
    return re.sub(r"codable", "Codeable", text, flags=re.IGNORECASE)


def clean_label(value: str | None) -> str | None:
    """Blank and NA are an empty ground-truth cell."""
    text = (value or "").strip()
    if text.casefold() in _EMPTY:
        return None
    return text


def yes_no_label(value: str | None) -> str | None:
    """Member name, DOB, member id, and provider signature are only Yes or No."""
    text = clean_label(value)
    if text is None:
        return None
    folded = text.casefold()
    if folded in {"yes", "y", "true"}:
        return "Yes"
    if folded in {"no", "n", "false"}:
        return "No"
    return None


# is_visible, rendering_provider, provider_signature, and member_id are read
# through to_jsonb so a database created before the column existed returns
# NULL instead of failing the whole lookup.


LABEL_COLUMNS = (
    "member_name", "member_dob", "member_id", "dos_from", "dos_to",
    "encounter_type", "page_type", "codeable",
    "blank_page", "junk_page", "is_invoice", "page_sequence", "rotation",
    "is_visible", "rendering_provider", "provider_signature",
)


def _labelled_row_sql() -> str:
    """True when at least one label cell holds a value, so an all-empty save does not count."""
    empty = ", ".join(f"'{value}'" for value in sorted(_EMPTY))
    cells = " OR ".join(
        f"lower(btrim(COALESCE(to_jsonb(g) ->> '{column}', ''))) NOT IN ({empty})"
        for column in LABEL_COLUMNS
    )
    return f"({cells})"


def charts_with_ground_truth(database_url: str | None, db_schema: str = "public") -> set[str]:
    """Chart folder names with at least one page that has a ground-truth value."""
    if not database_url or not database_url_usable(database_url):
        return set()
    try:
        with connection(database_url, db_schema) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT chart_name FROM page_ground_truth g "
                    f"WHERE {_labelled_row_sql()}"
                )
                return {str(row[0]) for row in cur.fetchall() if row[0]}
    except Exception as exc:
        logger.warning("page_ground_truth chart lookup failed: %s", exc)
        return set()


def _page_id(file_name: str) -> int | None:
    stem = Path(file_name).stem
    if stem.isdigit() and int(stem) > 0:
        return int(stem)
    return None


def ground_truth_by_page(
    database_url: str | None,
    chart_name: str,
    db_schema: str = "public",
) -> dict[int, PageGroundTruth]:
    if not database_url or not database_url_usable(database_url) or not chart_name:
        return {}
    try:
        with connection(database_url, db_schema) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT {_COLUMNS}
                      FROM page_ground_truth
                     WHERE chart_name = %s
                    """,
                    (chart_name,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        logger.warning("page_ground_truth page lookup failed: %s", exc)
        return {}

    out: dict[int, PageGroundTruth] = {}
    for row in rows:
        parsed = _ground_truth_from_row(row)
        out[parsed.pageNumber] = parsed
    return out


def _ground_truth_from_row(row: tuple) -> PageGroundTruth:
    return PageGroundTruth(
        pageNumber=int(row[0]),
        sourcePageId=row[1],
        memberName=row[2],
        memberDob=row[3],
        dosFrom=row[4],
        dosTo=row[5],
        encounterType=row[6],
        pageType=row[7],
        codeable=spell_codeable(row[8]),
        blankPage=row[9],
        junkPage=row[10],
        isInvoice=row[11],
        pageSequence=row[12],
        rotation=row[13],
        isVisible=row[14],
        renderingProvider=row[15] if len(row) > 15 else None,
        providerSignature=row[16] if len(row) > 16 else None,
        memberId=row[17] if len(row) > 17 else None,
    )


def _chart_name(folder_id: str) -> str:
    name = (folder_id or "").strip()
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("invalid chart name")
    return name


_UPSERT = """
INSERT INTO page_ground_truth (
    chart_name, page_number, source_page_id,
    member_name, member_dob, member_id, dos_from, dos_to,
    encounter_type, page_type, codeable,
    blank_page, junk_page, is_invoice,
    rotation, is_visible,
    rendering_provider, provider_signature
) VALUES (
    %s, %s, %s,
    %s, %s, %s, %s, %s,
    %s, %s, %s,
    %s, %s, %s,
    %s, %s,
    %s, %s
)
ON CONFLICT (chart_name, page_number) DO UPDATE SET
    source_page_id = COALESCE(EXCLUDED.source_page_id, page_ground_truth.source_page_id),
    member_name = EXCLUDED.member_name,
    member_dob = EXCLUDED.member_dob,
    member_id = EXCLUDED.member_id,
    dos_from = EXCLUDED.dos_from,
    dos_to = EXCLUDED.dos_to,
    encounter_type = EXCLUDED.encounter_type,
    page_type = EXCLUDED.page_type,
    codeable = EXCLUDED.codeable,
    blank_page = EXCLUDED.blank_page,
    junk_page = EXCLUDED.junk_page,
    is_invoice = EXCLUDED.is_invoice,
    rotation = EXCLUDED.rotation,
    is_visible = EXCLUDED.is_visible,
    rendering_provider = EXCLUDED.rendering_provider,
    provider_signature = EXCLUDED.provider_signature,
    updated_at = now()
RETURNING
    page_number, source_page_id,
    member_name, member_dob, dos_from, dos_to,
    encounter_type, page_type, codeable,
    blank_page, junk_page, is_invoice, page_sequence, rotation,
    is_visible, rendering_provider, provider_signature,
    member_id
"""


def upsert_page_ground_truth(
    database_url: str,
    folder_id: str,
    body: PageGroundTruth,
    db_schema: str = "public",
) -> PageGroundTruth:
    """Insert or update the ground-truth row for one chart page.

    A later manual save of the same page hits the same
    ``(chart_name, page_number)`` key. Columns this screen does not edit
    (page sequence, specialty, deleted level, source path) are left as they are.
    """
    chart = _chart_name(folder_id)
    if body.pageNumber < 1:
        raise ValueError("invalid page number")
    params = (
        chart,
        body.pageNumber,
        clean_label(body.sourcePageId),
        yes_no_label(body.memberName),
        yes_no_label(body.memberDob),
        yes_no_label(body.memberId),
        clean_label(body.dosFrom),
        clean_label(body.dosTo),
        clean_label(body.encounterType),
        clean_label(body.pageType),
        spell_codeable(body.codeable),
        clean_label(body.blankPage),
        clean_label(body.junkPage),
        clean_label(body.isInvoice),
        clean_label(body.rotation),
        clean_label(body.isVisible),
        clean_label(body.renderingProvider),
        clean_label(body.providerSignature),
    )
    with connection(database_url, db_schema) as conn:
        with conn.cursor() as cur:
            cur.execute(_UPSERT, params)
            row = cur.fetchone()
    if row is None:
        raise RuntimeError("ground truth upsert returned no row")
    return _ground_truth_from_row(row)


def attach_ground_truth(
    pages: list[ImagingPageResult],
    chart_name: str,
    database_url: str | None,
    db_schema: str = "public",
) -> list[ImagingPageResult]:
    """Match each page file stem (1.jpg / 1.png / 1.tif) to ground-truth Id."""
    by_page = ground_truth_by_page(database_url, chart_name, db_schema)
    if not by_page:
        return pages
    attached: list[ImagingPageResult] = []
    for page in pages:
        number = _page_id(page.fileName)
        hit = by_page.get(number) if number is not None else None
        if hit is None:
            attached.append(page)
        else:
            attached.append(page.model_copy(update={"groundTruth": hit}))
    return attached
