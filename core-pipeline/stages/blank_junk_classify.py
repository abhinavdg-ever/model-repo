"""Stage: blank / junk / duplicate classification.

Two passes, as the pipeline spec requires:

  pass 1  on preliminary (Tesseract) text, for printed + non-low-quality
          pages only. Handwritten and low-quality pages are skipped —
          prelim text is not trusted to judge them.
  pass 2  on final2 (Azure) text when present, else final1 (Docling /
          RapidOCR), for handwritten + low-quality pages plus any printed
          page pass 1 did not already rule out.

Duplicate detection (after blank/junk rules):

* Compare each comparable page only to neighbors **±2** in page order.
* Match when normalized-text similarity is **> 95%** (SequenceMatcher).
* On a match, keep the page with the **higher character count** as the
  original; on a tie, keep the **earlier** page. The other is ``duplicate``.
* Blank pages and texts shorter than 50 normalized chars are never compared.
* Prior-pass ``main`` pages are included as neighbors so pass 2 can still
  match a handwritten page to a printed one from pass 1.

Also: one final verdict per page (``mark_blank_junk_final``), and the junk CSV
is fully rewritten from the database after each pass.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional

from config import CORE_ROOT
from db import (
    connect,
    get_blank_junk_flags,
    get_ocr_texts,
    get_quality_map,
    low_quality_page_ids,
    non_printed_page_ids,
    mark_blank_junk_final,
    upsert_blank_junk,
)
from db.paths import imaging_csv, write_csv
from stages._support import (
    BJ_EXCLUDE,
    best_page_text,
    mark_completed,
    mark_skipped,
    stage_run,
    unwrap_ocr_content,
)

logger = logging.getLogger(__name__)

_JUNK_LIB = CORE_ROOT / "stages" / "lib" / "junk"
if str(_JUNK_LIB) not in sys.path:
    sys.path.insert(0, str(_JUNK_LIB))

from classify import (  # noqa: E402
    CLASSIFICATION_LABELS,
    CODE_BLANK,
    CODE_DUPLICATE,
    CODE_MAIN,
    DUPLICATE_NEIGHBOR_WINDOW,
    DUPLICATE_SIMILARITY_THRESHOLD,
    JUNK_CODES,
    classification_confidence,
    classify_text,
    duplicate_char_count,
    text_is_comparable,
    text_similarity,
)

STAGE = "blank_junk"

JUNK_CSV_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "page_classification",
    "page_classification_confidence",
    "page_group",
    "reason",
    "ocr_source",
    "pass_no",
    "is_final",
]

# classify.py's label -> the constrained junk_subtype values in schema v7.
SUBTYPE_DB = {
    "invoice": "Invoice",
    "cover": "Cover Page",
    "cover page": "Cover Page",
    "cover_page": "Cover Page",
    "record request": "Record Request/Transmittal",
    "record_request": "Record Request/Transmittal",
    "record request/transmittal": "Record Request/Transmittal",
    "transmittal": "Record Request/Transmittal",
    "instructions": "Instructions",
    "letter": "Letter/Fax",
    "fax": "Letter/Fax",
    "letter/fax": "Letter/Fax",
    "letter_fax": "Letter/Fax",
    "others": "Others",
}


def _to_db_flag(code: int) -> tuple[str, Optional[str]]:
    label = CLASSIFICATION_LABELS.get(code, "Main")
    if code == CODE_BLANK:
        return "blank", None
    if code == CODE_DUPLICATE:
        return "duplicate", None
    if code in JUNK_CODES:
        return "junk", SUBTYPE_DB.get(label.strip().casefold(), "Others")
    return "not_blank_junk", None


def _page_group(code: int) -> str:
    if code == CODE_BLANK:
        return "blank"
    if code == CODE_DUPLICATE:
        return "duplicate"
    if code in JUNK_CODES:
        return "junk"
    return "main"


def _confidence(code: int) -> float:
    raw = classification_confidence(code)
    try:
        if raw is not None:
            return float(raw)
    except (TypeError, ValueError):
        pass
    return 0.85 if code != CODE_MAIN else 0.7


def _row_for(
    page: dict[str, Any],
    *,
    code: int,
    reason: str,
    duplicate_of: Optional[int] = None,
) -> dict[str, Any]:
    flag, subtype = _to_db_flag(code)
    return {
        "page_id": page["id"],
        "page_name": page["page_name"],
        "page_number": page.get("page_number"),
        "code": code,
        "flag": flag,
        "subtype": subtype,
        "duplicate_of": duplicate_of,
        "confidence": _confidence(code),
        "label": CLASSIFICATION_LABELS.get(code, "Main"),
        "group": _page_group(code),
        "reason": reason,
    }


def _prefer_original(
    idx_a: int,
    id_a: int,
    chars_a: int,
    idx_b: int,
    id_b: int,
    chars_b: int,
) -> tuple[int, int]:
    """Return ``(original_id, duplicate_id)`` — higher chars wins; tie → earlier."""
    if chars_a > chars_b:
        return id_a, id_b
    if chars_b > chars_a:
        return id_b, id_a
    if idx_a <= idx_b:
        return id_a, id_b
    return id_b, id_a


def _ultimate_original(dup_of: dict[int, int], page_id: int) -> int:
    seen: set[int] = set()
    cur = page_id
    while cur in dup_of and cur not in seen:
        seen.add(cur)
        cur = dup_of[cur]
    return cur


def _prior_main_ids(conn: Any, chart_id: int) -> set[int]:
    """Pages already judged main in an earlier pass (eligible duplicate targets)."""
    existing = get_blank_junk_flags(conn, chart_id)
    return {pid for pid, flag in existing.items() if flag == "not_blank_junk"}


def _classify(
    pages: list[dict[str, Any]],
    texts: dict[int, str],
    todo: set[int],
    prior_main_ids: Optional[set[int]] = None,
) -> list[dict[str, Any]]:
    """Classify ``todo`` pages; duplicates use ±2 neighbor similarity > 95%.

    ``prior_main_ids`` are pages already ``not_blank_junk`` from an earlier pass;
    they participate as comparison neighbors (and may be demoted to duplicate
    when a longer later page matches).
    """
    prior = set(prior_main_ids or ())
    # Phase 1 — blank / junk / main from text rules (no duplicates yet).
    by_id: dict[int, dict[str, Any]] = {}
    for page in pages:
        page_id = page["id"]
        if page_id not in todo:
            continue
        text = texts.get(page_id) or ""
        code, reason = classify_text(text)
        by_id[page_id] = _row_for(page, code=code, reason=reason)

    def _comparable(page_id: int) -> bool:
        text = texts.get(page_id) or ""
        if not text_is_comparable(text):
            return False
        if page_id in by_id:
            return by_id[page_id]["code"] == CODE_MAIN
        return page_id in prior

    # Phase 2 — pairwise ±2 window among comparable pages.
    dup_of: dict[int, int] = {}
    n = len(pages)
    for i, page in enumerate(pages):
        pid = page["id"]
        if not _comparable(pid):
            continue
        for offset in range(1, DUPLICATE_NEIGHBOR_WINDOW + 1):
            j = i + offset
            if j >= n:
                break
            nid = pages[j]["id"]
            if not _comparable(nid):
                continue
            sim = text_similarity(texts.get(pid) or "", texts.get(nid) or "")
            if sim <= DUPLICATE_SIMILARITY_THRESHOLD:
                continue
            orig, dup = _prefer_original(
                i,
                pid,
                duplicate_char_count(texts.get(pid) or ""),
                j,
                nid,
                duplicate_char_count(texts.get(nid) or ""),
            )
            orig = _ultimate_original(dup_of, orig)
            if dup == orig:
                continue
            for other, pointed in list(dup_of.items()):
                if pointed == dup:
                    dup_of[other] = orig
            dup_of[dup] = orig

    # Apply duplicate marks (including demoting prior-main pages not in todo).
    page_by_id = {p["id"]: p for p in pages}
    for dup_id, orig_id in dup_of.items():
        orig_id = _ultimate_original(dup_of, orig_id)
        if dup_id == orig_id:
            continue
        reason = (
            f"duplicate_of_page_id:{orig_id} "
            f"(similarity>{DUPLICATE_SIMILARITY_THRESHOLD:.0%} within ±"
            f"{DUPLICATE_NEIGHBOR_WINDOW})"
        )
        page = page_by_id[dup_id]
        by_id[dup_id] = _row_for(
            page,
            code=CODE_DUPLICATE,
            reason=reason,
            duplicate_of=orig_id,
        )

    rows: list[dict[str, Any]] = []
    emitted: set[int] = set()
    for page in pages:
        pid = page["id"]
        if pid in todo and pid in by_id:
            rows.append(by_id[pid])
            emitted.add(pid)
    for page in pages:
        pid = page["id"]
        if pid in emitted or pid not in by_id:
            continue
        if pid in dup_of:
            rows.append(by_id[pid])
    return rows


def _rewrite_csv(conn: Any, chart_id: int, chart_name: str) -> Path:
    """Rebuild the junk CSV from the stored verdicts.

    Always a full rewrite, so re-running either pass converges instead of
    appending duplicate rows.
    """
    from db import list_blank_junk_for_csv

    rows = list_blank_junk_for_csv(conn, chart_id)

    out: list[dict[str, Any]] = []
    for row in rows:
        flag = row["blank_junk_flag"]
        label = row["junk_subtype"] or (
            "Blank" if flag == "blank"
            else "Duplicate" if flag == "duplicate"
            else "Main"
        )
        out.append(
            {
                "chart_name": row["chart_name"],
                "page_name": row["page_name"],
                "page_number": row["page_number"],
                "page_classification": label,
                "page_classification_confidence": row["confidence"],
                "page_group": "main" if flag == "not_blank_junk" else flag,
                "reason": row["reason"] or "",
                "ocr_source": row["ocr_source"],
                "pass_no": row["pass_no"],
                "is_final": row["is_final"],
            }
        )
    return write_csv(imaging_csv(chart_name, "junk"), JUNK_CSV_COLS, out)


def _run_pass(
    chart_id: int,
    pass_no: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, pass_no=pass_no, force=force) as ctx:
        with connect() as conn:
            non_printed = non_printed_page_ids(conn, chart_id)
            low_ids = low_quality_page_ids(conn, chart_id)
            quality = get_quality_map(conn, chart_id)

            if pass_no == 1:
                ocr_source = "tesseract"
                texts = get_ocr_texts(conn, chart_id, "tesseract")
                sources: dict[int, str] = {}
                # Handwritten/uncertain/mixed + low-quality pages are judged
                # in pass 2 on final OCR text — prelim is not trusted.
                mark_skipped(
                    conn, ctx, sorted(non_printed & ctx.todo), "handwritten"
                )
                mark_skipped(
                    conn,
                    ctx,
                    sorted((low_ids - non_printed) & ctx.todo),
                    "low_quality",
                )
            else:
                # Prefer final2; if Azure was skipped or failed, use final1.
                final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
                final1 = get_ocr_texts(conn, chart_id, "docling")
                texts = {}
                sources = {}
                for page in ctx.pages:
                    pid = page["id"]
                    body = best_page_text(
                        final2=final2.get(pid),
                        final1=final1.get(pid),
                        prelim=None,
                        quality_row=quality.get(pid),
                    )
                    texts[pid] = body
                    if unwrap_ocr_content(final2.get(pid)).strip():
                        sources[pid] = "azuredocintel"
                    elif unwrap_ocr_content(final1.get(pid)).strip():
                        sources[pid] = "docling"
                    else:
                        sources[pid] = "azuredocintel"
                ocr_source = "azuredocintel"  # default; per-row override below
                pass1 = get_blank_junk_flags(conn, chart_id, pass_no=1)
                # Eligible: non-printed + low-quality (never judged in pass 1),
                # plus printed pages pass 1 did not already rule out.
                pass1_skippers = non_printed | low_ids
                not_eligible = [
                    pid for pid in ctx.todo
                    if pid not in pass1_skippers
                    and pass1.get(pid, "not_blank_junk") in BJ_EXCLUDE
                ]
                mark_skipped(conn, ctx, not_eligible, "blank_junk_pass1")
                no_text = [
                    pid for pid in ctx.todo if not (texts.get(pid) or "").strip()
                ]
                mark_skipped(conn, ctx, no_text, "no_final_ocr_text")

            prior_mains = _prior_main_ids(conn, chart_id)

        classified = _classify(
            ctx.pages, texts, ctx.todo, prior_main_ids=prior_mains
        )

        with connect() as conn:
            for row in classified:
                row_source = ocr_source
                if pass_no == 2:
                    row_source = sources.get(row["page_id"], ocr_source)
                upsert_blank_junk(
                    conn,
                    chart_id=chart_id,
                    page_id=row["page_id"],
                    pass_no=pass_no,
                    blank_junk_flag=row["flag"],
                    ocr_source=row_source,
                    junk_subtype=row["subtype"],
                    duplicate_of_page_id=row["duplicate_of"],
                    confidence=row["confidence"],
                    reason=row["reason"],
                )
                # Demoted prior-main pages are upserted but were not in todo.
                if row["page_id"] in ctx.todo:
                    mark_completed(conn, ctx, row["page_id"])
            mark_blank_junk_final(conn, chart_id)
            path = _rewrite_csv(conn, chart_id, ctx.chart_name)

        counts: dict[str, int] = {}
        for row in classified:
            counts[row["group"]] = counts.get(row["group"], 0) + 1

        return {
            "chart_id": chart_id,
            "pass_no": pass_no,
            "junk_csv": str(path),
            "classified": len(classified),
            "skipped": ctx.skipped,
            "counts": counts,
        }


def run_pass1(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    """Blank/junk on printed, non-low-quality pages using preliminary text."""
    return _run_pass(chart_id, 1, force=force)


def run_pass2(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    """Blank/junk on final2→final1 text for HW + low-quality + surviving printed."""
    return _run_pass(chart_id, 2, force=force)

