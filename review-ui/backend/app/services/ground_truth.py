"""Read page_ground_truth for the landing flag and the imaging comparison."""

from __future__ import annotations

import logging
from pathlib import Path

from app.core.schemas import ImagingPageResult, PageGroundTruth
from app.services.chart_run_batch import database_url_usable
from app.services.db import connection

logger = logging.getLogger("review_ui.ground_truth")

_COLUMNS = """
    page_number, source_page_id,
    member_name, member_dob, dos_from, dos_to,
    encounter_type, page_type, codeable,
    blank_page, junk_page, is_invoice, page_sequence, rotation
"""


def charts_with_ground_truth(database_url: str | None, db_schema: str = "public") -> set[str]:
    """Chart folder names that have at least one ground-truth row."""
    if not database_url or not database_url_usable(database_url):
        return set()
    try:
        with connection(database_url, db_schema) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT chart_name FROM page_ground_truth")
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
        number = int(row[0])
        out[number] = PageGroundTruth(
            pageNumber=number,
            sourcePageId=row[1],
            memberName=row[2],
            memberDob=row[3],
            dosFrom=row[4],
            dosTo=row[5],
            encounterType=row[6],
            pageType=row[7],
            codeable=row[8],
            blankPage=row[9],
            junkPage=row[10],
            isInvoice=row[11],
            pageSequence=row[12],
            rotation=row[13],
        )
    return out


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
