"""Stage: date-of-service extraction.

Runs ``dos_logic.detect_dos_per_page`` over the chart's combined OCR text with
the key/value extractor's best date per page, and writes one DB row and one CSV
row per page. Per page: the key/value date (its confidence is the model's score),
else a DOS label in the first or last 20 words — see the dos_logic docstring.
``extraction_method`` is ``kv`` for a key/value date, else ``rules``.

With DOS_DEBUG on, every DOS label found at a page's edges, its date and
whether it was chosen go to ``<chart>/debug/<chart>_dos_candidates.csv``.
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Optional

from config import CORE_ROOT, DOS_DEBUG, chart_dir
from db import (
    connect,
    get_blank_junk_flags,
    get_ocr_texts,
    get_quality_map,
    upsert_dos,
)
from db.paths import imaging_csv, write_csv
from stages._support import (
    BJ_EXCLUDE,
    best_page_text,
    unwrap_ocr_content,
    mark_completed,
    mark_skipped,
    stage_run,
)

logger = logging.getLogger(__name__)

STAGE = "dos_extract"

_DOS_LIB = CORE_ROOT / "stages" / "lib" / "dos"
if str(_DOS_LIB) not in sys.path:
    sys.path.insert(0, str(_DOS_LIB))

from dos_logic import LabelledDate, detect_dos_per_page, to_iso_date  # noqa: E402

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
    # Which state gave the page's date: kv, edge, edge_tesseract, admit_discharge, dictation,
    # none.
    "source",
]


CANDIDATE_COLS = ["chart_name", "page_name", *LabelledDate.__dataclass_fields__]


def _page_method(hit: dict[str, Any]) -> str:
    return "kv" if hit.get("kv_backed") or hit.get("page_source") == "kv" else "rules"


def _combined_text(
    conn: Any, chart_id: int, pages: list[dict[str, Any]], eligible: set[int]
) -> tuple[str, dict[str, str]]:
    """Rebuild the chart's text with ``===== page =====`` markers.

    The reference splits on exactly this marker (``UI_PAGE_MARKER_RE``), and the
    document-level carry-forward depends on seeing pages in order — so every
    page gets a block, with an empty body for pages this stage does not read.

    Text preference: final2 → final1 → prelim. Prelim is never used for
    handwritten / uncertain / mixed / low-quality pages.

    Also returns the Tesseract text of each page that has no Final2 text
    (same restriction): the edge rule reads its top and bottom words when the
    Final1 text has no DOS label there, because Docling can drop the page's
    top banner (fixed in ``docling_ocr.export_markdown``, but OCR reused with
    ``skip_ocr`` still has it missing).
    """
    from db import page_blocks_prelim

    prelim = get_ocr_texts(conn, chart_id, "tesseract")
    final1 = get_ocr_texts(conn, chart_id, "docling")
    final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
    quality = get_quality_map(conn, chart_id)

    blocks: list[str] = []
    tesseract: dict[str, str] = {}
    for page in pages:
        page_id = page["id"]
        if page_id in eligible:
            text = best_page_text(
                final2=final2.get(page_id),
                final1=final1.get(page_id),
                prelim=prelim.get(page_id),
                quality_row=quality.get(page_id),
            )
            # Only without Final2: Final1 (Docling) can drop the top banner.
            if (
                not unwrap_ocr_content(final2.get(page_id)).strip()
                and prelim.get(page_id)
                and not page_blocks_prelim(quality.get(page_id))
            ):
                tesseract[page["page_name"]] = prelim[page_id]
        else:
            text = ""
        blocks.append(f"===== {page['page_name']} =====\n{text.rstrip()}\n")
    return "\n".join(blocks), tesseract


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


def _kv_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Staged DOS rows as the dates dos_logic reads; ``confidence`` is the model's score, or
    the rules' score when no model ran."""
    from stages.lib.extraction.util.dates import canonical_date

    found: list[dict[str, Any]] = []
    for row in rows:
        raw = str(row.get("dos_from") or row.get("value") or "")
        iso = canonical_date(raw)
        if len(iso) != 10:
            continue
        # A range ("Date of Service: a - b") keeps its end date.
        iso_to = canonical_date(str(row.get("dos_to") or ""))
        found.append(
            {
                "iso": iso,
                "iso_to": iso_to if len(iso_to) == 10 and iso_to > iso else "",
                "raw": raw,
                "tier": row.get("tier") or "",
                "keyword": row.get("key") or "",
                "confidence": row.get("model_score") if row.get("model_score") is not None else row.get("score"),
            }
        )
    return found


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, force=force) as ctx:
        with connect() as conn:
            bj = get_blank_junk_flags(conn, chart_id, final_only=True)
            drop = [
                pid for pid in ctx.todo
                if bj.get(pid, "not_blank_junk") in BJ_EXCLUDE
            ]
            mark_skipped(conn, ctx, drop, "blank_junk")
            eligible = set(ctx.todo)
            text, tesseract = _combined_text(conn, chart_id, ctx.pages, eligible)

        candidates: Optional[list[dict[str, Any]]] = [] if DOS_DEBUG else None
        from stages.lib.extraction.stage import ensure_staging

        staged = ensure_staging(chart_id, ctx.chart_name)
        # Every key/value date the rules accepted; dos_logic picks by the page's priority.
        kv_dates: dict[str, list[dict[str, Any]]] = {}
        for page in ctx.pages:
            staged_page = staged.page(page["page_name"])
            found = _kv_items(staged_page.candidates("dos")) if staged_page is not None else []
            if found:
                kv_dates[page["page_name"]] = found
        hits = detect_dos_per_page(
            text,
            candidate_log=candidates,
            kv_dates=kv_dates,
            tesseract_texts=tesseract,
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
                    extraction_method=_page_method(hit),
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
                        "extraction_method": _page_method(hit),
                        "source": hit.get("page_source") or "none",
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
                    extraction_method="rules",
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

        return {
            "chart_id": chart_id,
            "dos_csv": str(path),
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            "kv_pages": sum(1 for r in csv_rows if r["source"] == "kv"),
            "edge_pages": sum(1 for r in csv_rows if r["source"] in ("edge", "edge_tesseract")),
            "admit_discharge_pages": sum(1 for r in csv_rows if r["source"] == "admit_discharge"),
        }
