"""Stage: blank / junk / duplicate classification.

Two passes, as the pipeline spec requires:

  pass 1  on preliminary (Tesseract) text, for printed pages only. Handwritten
          pages are skipped — Tesseract's read of handwriting is not good
          enough to judge them on.
  pass 2  on final2 (Azure Document Intelligence) text, for handwritten pages
          plus any printed page pass 1 did not already rule out.

Three things are fixed relative to v6:

1. **Duplicate scope.** v6 rebuilt the fingerprint table inside each pass over
   only that pass's pages, so a printed page duplicating a handwritten page was
   invisible. The fingerprint table is now seeded from every page that already
   has a verdict, in page order, so pass 2 sees pass 1's pages and duplicates
   are found across the whole chart.

2. **One final verdict per page.** Each pass writes its own row (pass_no), then
   ``mark_blank_junk_final`` stamps the winning row. Downstream stages read
   ``v_page_blank_junk_final`` and never re-implement precedence.

3. **Idempotent CSV.** v6 truncated in pass 1 and appended in pass 2, so
   re-running pass 2 alone duplicated every row. The CSV is now rebuilt from the
   database after each pass, so it always matches the stored verdicts.
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
    handwritten_page_ids,
    mark_blank_junk_final,
    upsert_blank_junk,
)
from db.paths import imaging_csv, write_csv
from stages._support import (
    BJ_EXCLUDE,
    mark_completed,
    mark_skipped,
    stage_run,
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
    JUNK_CODES,
    classification_confidence,
    classify_text,
    fingerprint,
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


def _seed_fingerprints(
    conn: Any, chart_id: int, pages: list[dict[str, Any]], texts: dict[int, str]
) -> dict[str, int]:
    """Fingerprints of pages already judged 'main', so a later pass can spot a
    duplicate of a page an earlier pass handled.

    Only 'main' pages seed the table: a page that is itself blank or junk is not
    an original worth marking others as copies of.
    """
    existing = get_blank_junk_flags(conn, chart_id)
    seen: dict[str, int] = {}
    for page in pages:  # page order, so the earliest copy stays the original
        page_id = page["id"]
        if existing.get(page_id) != "not_blank_junk":
            continue
        fp = fingerprint(texts.get(page_id) or "")
        if fp:
            seen.setdefault(fp, page_id)
    return seen


def _classify(
    pages: list[dict[str, Any]],
    texts: dict[int, str],
    todo: set[int],
    seen_fps: dict[str, int],
) -> list[dict[str, Any]]:
    """Classify the pages in `todo`, walking `pages` in page order.

    `seen_fps` is carried in and mutated, so duplicates are detected against
    every page judged so far — this pass and any earlier one.
    """
    rows: list[dict[str, Any]] = []
    for page in pages:
        page_id = page["id"]
        if page_id not in todo:
            continue
        text = texts.get(page_id) or ""
        code, reason = classify_text(text)
        duplicate_of: Optional[int] = None

        fp = fingerprint(text)
        if code == CODE_MAIN and fp:
            if fp in seen_fps and seen_fps[fp] != page_id:
                code = CODE_DUPLICATE
                duplicate_of = seen_fps[fp]
                reason = f"duplicate_of_page_id:{duplicate_of}"
            else:
                seen_fps.setdefault(fp, page_id)

        flag, subtype = _to_db_flag(code)
        rows.append(
            {
                "page_id": page_id,
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
        )
    return rows


def _rewrite_csv(conn: Any, chart_id: int, chart_name: str) -> Path:
    """Rebuild the junk CSV from the stored verdicts.

    Always a full rewrite, so re-running either pass converges instead of
    appending duplicate rows.
    """
    rows = conn.execute(
        """
        SELECT c.chart_name, p.page_name, p.page_number,
               b.blank_junk_flag, b.junk_subtype, b.confidence, b.reason,
               b.ocr_source, b.pass_no, b.is_final
          FROM blank_junk_classification b
          JOIN page_list p  ON p.id = b.page_id
          JOIN chart_list c ON c.id = b.chart_id
         WHERE b.chart_id = %s
         ORDER BY p.page_number NULLS LAST, p.page_name, b.pass_no
        """,
        (chart_id,),
    ).fetchall()

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
            hw_ids = handwritten_page_ids(conn, chart_id)

            if pass_no == 1:
                ocr_source = "tesseract"
                texts = get_ocr_texts(conn, chart_id, "tesseract")
                # Handwritten pages are judged in pass 2, on final2 text.
                mark_skipped(conn, ctx, sorted(hw_ids & ctx.todo), "handwritten")
            else:
                ocr_source = "azuredocintel"
                texts = {
                    pid: _final2_content(raw)
                    for pid, raw in get_ocr_texts(conn, chart_id, "azuredocintel").items()
                }
                pass1 = get_blank_junk_flags(conn, chart_id, pass_no=1)
                # Eligible: handwritten pages (never judged), plus printed pages
                # pass 1 did not already rule out.
                not_eligible = [
                    pid for pid in ctx.todo
                    if pid not in hw_ids and pass1.get(pid, "not_blank_junk") in BJ_EXCLUDE
                ]
                mark_skipped(conn, ctx, not_eligible, "blank_junk_pass1")
                # A page with no final2 text has nothing new to judge on.
                no_text = [
                    pid for pid in ctx.todo if not (texts.get(pid) or "").strip()
                ]
                mark_skipped(conn, ctx, no_text, "no_final2_text")

            seen_fps = _seed_fingerprints(conn, chart_id, ctx.pages, texts)

        classified = _classify(ctx.pages, texts, ctx.todo, seen_fps)

        with connect() as conn:
            for row in classified:
                upsert_blank_junk(
                    conn,
                    chart_id=chart_id,
                    page_id=row["page_id"],
                    pass_no=pass_no,
                    blank_junk_flag=row["flag"],
                    ocr_source=ocr_source,
                    junk_subtype=row["subtype"],
                    duplicate_of_page_id=row["duplicate_of"],
                    confidence=row["confidence"],
                    reason=row["reason"],
                )
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


def _final2_content(raw: Optional[str]) -> str:
    if not raw:
        return ""
    text = raw.strip()
    if not text.startswith("{"):
        return raw
    try:
        import json

        return str(json.loads(text).get("content") or "")
    except (ValueError, AttributeError):
        return raw


def run_pass1(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    """Blank/junk on printed pages using preliminary (Tesseract) text."""
    return _run_pass(chart_id, 1, force=force)


def run_pass2(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    """Blank/junk on final2 text for handwritten + surviving printed pages."""
    return _run_pass(chart_id, 2, force=force)
