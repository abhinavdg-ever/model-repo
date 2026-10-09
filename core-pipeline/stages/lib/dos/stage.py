"""Stage: date-of-service extraction.

Runs ``dos_logic.detect_dos_per_page`` over the chart's combined OCR text. The
driver scores every date on every page and resolves the chart (see the
dos_logic docstring); this stage feeds it the chart's received date — the
``chart_list`` row's ``created_at`` — and writes one DB row and one CSV row per
page.

The Azure OpenAI pass is a fallback for clinical pages where no candidate
clears DOS_MIN_SCORE. When it is not configured every row records
``extraction_method='rules'``, so a degraded run is visible in the data.

With DOS_DEBUG on, every candidate, its features, its score and whether it was
chosen go to ``<chart>/debug/<chart>_dos_candidates.csv``.
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from typing import Any, Optional

from config import (
    AZURE_OPENAI_DEPLOYMENT,
    CORE_ROOT,
    DOS_DEBUG,
    DOS_LLM_ENABLED,
    chart_dir,
)
from db import (
    connect,
    get_blank_junk_flags,
    get_chart,
    get_ocr_texts,
    get_quality_map,
    upsert_dos,
)
from db.paths import imaging_csv, write_csv
from stages._support import (
    BJ_EXCLUDE,
    best_page_text,
    mark_completed,
    mark_skipped,
    stage_run,
)

logger = logging.getLogger(__name__)

STAGE = "dos_extract"

_DOS_LIB = CORE_ROOT / "stages" / "lib" / "dos"
if str(_DOS_LIB) not in sys.path:
    sys.path.insert(0, str(_DOS_LIB))

from dos_logic import Candidate, detect_dos_per_page, to_iso_date  # noqa: E402

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
    "final_dos",
    "keyword",
    "confidence",
    "extraction_method",
]


CANDIDATE_COLS = ["chart_name", *(f for f in Candidate.__dataclass_fields__ if f != "pair")]


def _page_method(hit: dict[str, Any], method: str) -> str:
    source = hit.get("page_source")
    if source == "llm":
        return "llm"
    if source == "kv":
        return "kv"
    return method


def _received_date(chart: Optional[dict[str, Any]]) -> Optional[date]:
    created = (chart or {}).get("created_at")
    if isinstance(created, datetime):
        return created.date()
    if isinstance(created, date):
        return created
    return None


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
            "DOS: no usable Azure OpenAI credentials; regex only. Set "
            "AZURE_OPENAI_ENDPOINT plus either AZURE_OPENAI_API_KEY or, for "
            "keyless auth, AZURE_OPENAI_AUTH=entra with azure-identity installed"
        )
        return None
    try:
        from azure_llm import resolved_auth

        auth = resolved_auth() or "unknown"
    except Exception:
        auth = "unknown"
    logger.info(
        "DOS: LLM pass enabled (deployment=%s, auth=%s)",
        AZURE_OPENAI_DEPLOYMENT,
        auth,
    )
    return client


def _combined_text(
    conn: Any, chart_id: int, pages: list[dict[str, Any]], eligible: set[int]
) -> str:
    """Rebuild the chart's text with ``===== page =====`` markers.

    The reference splits on exactly this marker (``UI_PAGE_MARKER_RE``), and the
    document-level carry-forward depends on seeing pages in order — so every
    page gets a block, with an empty body for pages this stage does not read.

    Text preference: final2 → final1 → prelim. Prelim is never used for
    handwritten / uncertain / mixed / low-quality pages.
    """
    prelim = get_ocr_texts(conn, chart_id, "tesseract")
    final1 = get_ocr_texts(conn, chart_id, "docling")
    final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
    quality = get_quality_map(conn, chart_id)

    blocks: list[str] = []
    for page in pages:
        page_id = page["id"]
        if page_id in eligible:
            text = best_page_text(
                final2=final2.get(page_id),
                final1=final1.get(page_id),
                prelim=prelim.get(page_id),
                quality_row=quality.get(page_id),
            )
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
            received = _received_date(get_chart(conn, chart_id))

        candidates: Optional[list[dict[str, Any]]] = [] if DOS_DEBUG else None
        from stages.lib.extraction.stage import ensure_staging
        from stages.lib.extraction.util.dates import canonical_date

        staged = ensure_staging(chart_id, ctx.chart_name)
        kv_dates: dict[str, list[dict[str, Any]]] = {}
        for page in ctx.pages:
            staged_page = staged.page(page["page_name"])
            if staged_page is None:
                continue
            found: list[dict[str, Any]] = []
            for row in staged_page.selected("dos"):
                raw = str(row.get("dos_from") or row.get("value") or "")
                iso = canonical_date(raw)
                if len(iso) != 10:
                    continue
                found.append(
                    {
                        "iso": iso,
                        "raw": raw,
                        "tier": row.get("tier") or "",
                        "keyword": row.get("key") or "",
                    }
                )
            if found:
                kv_dates[page["page_name"]] = found
        hits = detect_dos_per_page(
            text,
            client,
            use_llm=client is not None,
            received_date=received,
            candidate_log=candidates,
            kv_dates=kv_dates,
        )

        by_name = {p["page_name"]: p for p in ctx.pages}
        csv_rows: list[dict[str, Any]] = []
        written: set[int] = set()

        with connect() as conn:
            for hit in hits:
                page = by_name.get(str(hit.get("page_name") or ""))
                if page is None or page["id"] not in eligible or page["id"] in written:
                    continue
                page_id = page["id"]
                written.add(page_id)
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
                    extraction_method=_page_method(hit, method),
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
                        "final_dos": hit.get("final_dos") or "",
                        "keyword": hit.get("keyword") or "",
                        "confidence": hit.get("confidence"),
                        "extraction_method": _page_method(hit, method),
                    }
                )

            # Eligible pages with no DOS hit still finish the stage (empty DOS).
            for page in ctx.pages:
                page_id = page["id"]
                if page_id not in eligible or page_id in written:
                    continue
                upsert_dos(
                    conn,
                    chart_id=chart_id,
                    page_id=page_id,
                    date_of_service_from=None,
                    date_of_service_to=None,
                    date_of_service_from_doclevel=None,
                    date_of_service_to_doclevel=None,
                    confidence=None,
                    all_dates=[],
                    extraction_method=method,
                )
                mark_completed(conn, ctx, page_id)

        path = write_csv(imaging_csv(ctx.chart_name, "dos"), DOS_COLS, csv_rows)
        if candidates is not None:
            debug_path = chart_dir(ctx.chart_name) / "debug" / f"{ctx.chart_name}_dos_candidates.csv"
            write_csv(
                debug_path,
                CANDIDATE_COLS,
                ({"chart_name": ctx.chart_name, **row} for row in candidates),
            )

        llm_pages = sum(1 for r in csv_rows if r["extraction_method"] == "llm")
        return {
            "chart_id": chart_id,
            "dos_csv": str(path),
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            "llm_enabled": client is not None,
            "llm_pages": llm_pages,
            "extraction_method": method,
        }
