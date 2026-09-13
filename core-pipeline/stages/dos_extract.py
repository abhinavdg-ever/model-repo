"""Stage: date-of-service extraction.

Runs the reference driver ``dos_logic.detect_dos_per_page`` over the chart's
combined OCR text — the same entry point ``Reference`` /
``02-imaging-pipeline/dos-extraction/extract_dos.py`` uses. That driver owns:

  * the regex pass over the whole page (``extract_dos_from_page_text``),
  * escalation to the Azure OpenAI pass (``extract_dos_range_with_llm``) for
    pages the regex could not read and that ``page_allows_llm`` permits,
  * the document-level carry-forward (a page with no DOS of its own inherits the
    previous encounter's; before the first encounter it gets the reference's
    ``DEFAULT_DOC_DOS``),
  * ISO normalisation of both page-level and document-level dates, including
    comma-separated multi-date lists.

v6 called only ``extract_dos_from_page_text`` per page. That dropped the LLM
pass, the carry-forward and the real ISO conversion — ``doc_dos_from_iso`` was
written as a copy of the un-normalised ``doc_dos_from``. All three are restored
here by going through the reference driver.

When Azure OpenAI is not configured the stage runs regex-only and records
``extraction_method='rules'`` on every row, so a degraded run is visible in the
data rather than silent.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any, Optional

from config import (
    AZURE_OPENAI_DEPLOYMENT,
    CORE_ROOT,
    DOS_LLM_ENABLED,
)
from db import connect, get_blank_junk_flags, get_ocr_texts, upsert_dos
from db.paths import imaging_csv, write_csv
from stages._support import BJ_EXCLUDE, mark_completed, mark_skipped, stage_run

logger = logging.getLogger(__name__)

STAGE = "dos_extract"

_DOS_LIB = CORE_ROOT / "stages" / "lib" / "dos"
if str(_DOS_LIB) not in sys.path:
    sys.path.insert(0, str(_DOS_LIB))

from dos_logic import detect_dos_per_page, to_iso_date  # noqa: E402

DOS_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "dos_from",
    "dos_to",
    "dos_from_iso",
    "dos_to_iso",
    "doc_dos_from",
    "doc_dos_to",
    "doc_dos_from_iso",
    "doc_dos_to_iso",
    "match_type",
    "keyword",
    "confidence",
    "extraction_method",
]


def _llm_client() -> Optional[Any]:
    """Azure OpenAI client, or None when the LLM pass is off/unconfigured."""
    if not DOS_LLM_ENABLED:
        logger.info("DOS: LLM pass disabled — regex only")
        return None
    try:
        from azure_llm import get_azure_openai_client

        client = get_azure_openai_client()
    except Exception as exc:
        logger.warning("DOS: Azure OpenAI client unavailable (%s); regex only", exc)
        return None
    if client is None:
        logger.warning(
            "DOS: AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT not set; regex only"
        )
        return None
    logger.info("DOS: LLM pass enabled (deployment=%s)", AZURE_OPENAI_DEPLOYMENT)
    return client


def _final2_content(raw: Optional[str]) -> str:
    if not raw:
        return ""
    text = raw.strip()
    if not text.startswith("{"):
        return raw
    try:
        return str(json.loads(text).get("content") or "")
    except (ValueError, AttributeError):
        return raw


def _combined_text(
    conn: Any, chart_id: int, pages: list[dict[str, Any]], eligible: set[int]
) -> str:
    """Rebuild the chart's text with ``===== page =====`` markers.

    The reference splits on exactly this marker (``UI_PAGE_MARKER_RE``), and the
    document-level carry-forward depends on seeing pages in order — so every
    page gets a block, with an empty body for pages this stage does not read.
    """
    prelim = get_ocr_texts(conn, chart_id, "tesseract")
    final1 = get_ocr_texts(conn, chart_id, "docling")
    final2 = get_ocr_texts(conn, chart_id, "azuredocintel")

    blocks: list[str] = []
    for page in pages:
        page_id = page["id"]
        if page_id in eligible:
            text = _final2_content(final2.get(page_id))
            if not text.strip():
                text = final1.get(page_id) or ""
            if not text.strip():
                text = prelim.get(page_id) or ""
        else:
            text = ""
        blocks.append(f"===== {page['page_name']} =====\n{text.rstrip()}\n")
    return "\n".join(blocks)


def _split_dates(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _date_rows(hit: dict[str, Any]) -> list[dict[str, Any]]:
    """Every date this page carries, for the `dates` array on the DOS row.

    The reference emits comma-separated lists when a page names several dates;
    a single from/to column pair can only keep one.
    """
    froms = _split_dates(hit.get("dos_from"))
    tos = _split_dates(hit.get("dos_to"))
    rows: list[dict[str, Any]] = []
    for index, value in enumerate(froms):
        to_value = tos[index] if index < len(tos) else (tos[-1] if tos else value)
        rows.append(
            {
                "dos_from": to_iso_date(value) or None,
                "dos_to": to_iso_date(to_value) or None,
                "source_keyword": (hit.get("keyword") or hit.get("match_type") or "")[:100],
                "confidence": hit.get("confidence"),
            }
        )
    return rows


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    client = _llm_client()
    method = "rules+llm" if client is not None else "rules"

    with stage_run(chart_id, STAGE, force=force) as ctx:
        with connect() as conn:
            bj = get_blank_junk_flags(conn, chart_id, final_only=True)
            drop = [
                pid for pid in ctx.todo
                if bj.get(pid, "not_blank_junk") in BJ_EXCLUDE
            ]
            mark_skipped(conn, ctx, drop, "blank_junk")
            eligible = set(ctx.todo)
            text = _combined_text(conn, chart_id, ctx.pages, eligible)

        hits = detect_dos_per_page(text, client, use_llm=client is not None)

        by_name = {p["page_name"]: p for p in ctx.pages}
        csv_rows: list[dict[str, Any]] = []

        with connect() as conn:
            for hit in hits:
                page = by_name.get(str(hit.get("page_name") or ""))
                if page is None or page["id"] not in eligible:
                    continue
                page_id = page["id"]
                date_rows = _date_rows(hit)
                upsert_dos(
                    conn,
                    chart_id=chart_id,
                    page_id=page_id,
                    date_of_service_from=hit.get("dos_from_iso") or None,
                    date_of_service_to=hit.get("dos_to_iso") or None,
                    date_of_service_from_doclevel=hit.get("doc_dos_from_iso") or None,
                    date_of_service_to_doclevel=hit.get("doc_dos_to_iso") or None,
                    confidence=hit.get("confidence"),
                    all_dates=date_rows,
                    extraction_method=(
                        "llm" if hit.get("match_type") == "llm" else method
                    ),
                )
                mark_completed(conn, ctx, page_id)

                csv_rows.append(
                    {
                        "chart_name": ctx.chart_name,
                        "page_name": page["page_name"],
                        "page_number": page.get("page_number"),
                        "dos_from": hit.get("dos_from") or "",
                        "dos_to": hit.get("dos_to") or "",
                        "dos_from_iso": hit.get("dos_from_iso") or "",
                        "dos_to_iso": hit.get("dos_to_iso") or "",
                        "doc_dos_from": hit.get("doc_dos_from") or "",
                        "doc_dos_to": hit.get("doc_dos_to") or "",
                        "doc_dos_from_iso": hit.get("doc_dos_from_iso") or "",
                        "doc_dos_to_iso": hit.get("doc_dos_to_iso") or "",
                        "match_type": hit.get("match_type") or "",
                        "keyword": hit.get("keyword") or "",
                        "confidence": hit.get("confidence"),
                        "extraction_method": (
                            "llm" if hit.get("match_type") == "llm" else method
                        ),
                    }
                )

        path = write_csv(imaging_csv(ctx.chart_name, "dos"), DOS_COLS, csv_rows)

        llm_pages = sum(1 for r in csv_rows if r["match_type"] == "llm")
        return {
            "chart_id": chart_id,
            "dos_csv": str(path),
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            "llm_enabled": client is not None,
            "llm_pages": llm_pages,
            "extraction_method": method,
        }
