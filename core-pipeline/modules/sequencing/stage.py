"""Stage: page sequencing (suggested order; does not reorder files).

Ported from advantmed-document-processing ``adapters/sequencing`` (explicit page
markers, multi-stream split, header groups, optional cross-encoder continuation), with
the order fenced (2026-10-25, user; ``detect.fenced_order``): Main pages stay in scan
order, except pages that confidently say "page n of m" (same m), which are put in
page-number order -- across continuity documents too -- at most 5 places from their scan
position, with no limit when the whole document is numbered n of m.

Writes DB + ``imaging/<chart>_sequencing.csv``.
``currentSequence`` = file/page order; ``actualSequence`` = suggested ``seq``.

Two series: Main pages are ordered 1, 2, 3 …; blank / junk pages are left out of the
ordering and numbered J1, J2 … in page order (``seq`` NULL, ``sequence_method``
``blank_junk``; the CSV says ``J1``). A complete duplicate is in the J series too
(2026-10-25, user). Accuracy compares Main pages with Main pages only.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from common.db import (
    connect,
    get_additional_page_details_map,
    get_blank_junk_flags,
    get_continuity_map,
    get_ocr_texts,
    get_quality_map,
    upsert_sequencing,
)
from common.db.paths import imaging_csv, write_csv
from apps.worker.stages._support import (
    BJ_EXCLUDE,
    best_page_text,
    mark_completed,
    mark_skipped,
    stage_run,
)
from modules.sequencing import compute_sequence_assignments

logger = logging.getLogger(__name__)

STAGE = "page_sequencing"

SEQ_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "current_sequence",
    "actual_sequence",
    "sequence_method",
    "confidence",
    "review_flag",
    "stream_id",
    "ocr_source",
]


def _ocr_source_label(
    *,
    final2: Optional[str],
    final1: Optional[str],
    prelim: Optional[str],
) -> str:
    if final2 and str(final2).strip():
        return "final2"
    if final1 and str(final1).strip():
        return "final1"
    if prelim and str(prelim).strip():
        return "prelim"
    return ""


def _printed_marker(details: Optional[dict[str, Any]]) -> Optional[Any]:
    """The printed page number the key/value stage stored (additional_page_details), as a
    sequencing marker."""
    if not details:
        return None
    from modules.sequencing.engine.types import ExplicitMarker

    try:
        number = int(details.get("printed_page_no") or 0)
        total = int(details["printed_page_total"]) if details.get("printed_page_total") else None
    except (TypeError, ValueError):
        return None
    if number < 1 or (total is not None and (total < 1 or number > total)):
        return None
    return ExplicitMarker(
        page_num=number,
        total_pages=total,
        pattern="page_x_of_y" if total else "page_x",
        confidence=1.0 if total else 0.85,
    )


def junk_sequences(pages: list[dict[str, Any]], verdicts: dict[int, str]) -> dict[int, int]:
    """Blank / junk / duplicate page id → its J number (1, 2, 3 … in page order). Main
    pages are not in it."""
    out: dict[int, int] = {}
    for page in sorted(pages, key=lambda p: int(p.get("page_number") or 0)):
        if verdicts.get(page["id"], "not_blank_junk") in BJ_EXCLUDE:
            out[page["id"]] = len(out) + 1
    return out


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, force=force) as ctx:
        with connect() as conn:
            bj = get_blank_junk_flags(conn, chart_id, final_only=True)
            drop = [
                pid
                for pid in ctx.todo
                if bj.get(pid, "not_blank_junk") in BJ_EXCLUDE
            ]
            mark_skipped(conn, ctx, drop, "blank_junk")
            todo = set(ctx.todo)

            prelim = get_ocr_texts(conn, chart_id, "tesseract")
            final1 = get_ocr_texts(conn, chart_id, "docling")
            final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
            quality = get_quality_map(conn, chart_id)
            details = get_additional_page_details_map(conn, chart_id)
            continuity = get_continuity_map(conn, chart_id)

        # Main pages are ordered together, fenced by marker family, distance and
        # continuity document; blank / junk / duplicate pages get J1, J2 … in page order.
        seq_inputs: list[dict[str, Any]] = []
        sources: dict[int, str] = {}
        junk_seq = junk_sequences(ctx.pages, bj)
        for page in ctx.pages:
            page_id = page["id"]
            f2 = final2.get(page_id)
            f1 = final1.get(page_id)
            pr = prelim.get(page_id)
            sources[page_id] = _ocr_source_label(final2=f2, final1=f1, prelim=pr)
            if bj.get(page_id, "not_blank_junk") in BJ_EXCLUDE:
                continue
            seq_inputs.append(
                {
                    "page_id": page_id,
                    "page_number": int(page.get("page_number") or 0),
                    "text": best_page_text(
                        final2=f2,
                        final1=f1,
                        prelim=pr,
                        quality_row=quality.get(page_id),
                    ),
                    "is_classified": False,
                    "page_marker": _printed_marker(details.get(page_id)),
                    "document": (continuity.get(page_id) or {}).get("document_seq"),
                }
            )

        assignments = compute_sequence_assignments(seq_inputs)
        by_str_id = {str(a["page_id"]): a for a in assignments}
        ml_summary = _sequencing_shadow(ctx.chart_name, seq_inputs, {p["id"]: p["page_name"] for p in ctx.pages})

        csv_rows: list[dict[str, Any]] = []
        with connect() as conn:
            for page in ctx.pages:
                page_id = page["id"]
                a = by_str_id.get(str(page_id)) or {}
                current = int(page.get("page_number") or 0) or None
                actual = a.get("sequence_position")
                conf = a.get("sequence_confidence")
                method = a.get("sequence_method") or ""
                review = bool(a.get("sequence_review_flag"))
                shown: Any = actual if actual is not None else ""
                if page_id in junk_seq:
                    # J1..Jn is scan order among the set-aside pages: nothing is decided.
                    method, shown, conf = "blank_junk", f"J{junk_seq[page_id]}", None

                if page_id in todo or force:
                    upsert_sequencing(
                        conn,
                        chart_id=chart_id,
                        page_id=page_id,
                        original_page_number=current,
                        seq=int(actual) if actual is not None else None,
                        confidence=float(conf) if conf is not None else None,
                        sequence_method=method or None,
                        review_flag=review,
                    )
                if page_id in todo:
                    mark_completed(conn, ctx, page_id)

                csv_rows.append(
                    {
                        "chart_name": ctx.chart_name,
                        "page_name": page["page_name"],
                        "page_number": page.get("page_number"),
                        "current_sequence": current or "",
                        "actual_sequence": shown,
                        "sequence_method": method,
                        "confidence": conf if conf is not None else "",
                        "review_flag": "y" if review else "n",
                        "stream_id": a.get("stream_id") or "",
                        "ocr_source": sources.get(page_id, ""),
                    }
                )

        path = write_csv(
            imaging_csv(ctx.chart_name, "sequencing"), SEQ_COLS, csv_rows
        )
        logger.info(
            "page_sequencing chart=%s pages=%d → %s",
            ctx.chart_name,
            len(csv_rows),
            path,
        )
        return {
            "chart_id": chart_id,
            "sequencing_ml": ml_summary,
            "sequencing_csv": str(path),
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
        }


SHADOW_FILE = "sequencing_shadow.json"


def _encounter_dates(texts: list[str], docs: list[Any]) -> list[str]:
    """Each page's encounter date (Brief 2 Step 1a): the encounter's first dated page's date of
    service; an undated encounter takes the one before."""
    from modules.continuity.ml.clues import dos_dates

    first: dict[Any, str] = {}
    for text, doc in zip(texts, docs):
        if doc not in first:
            found = dos_dates(text)
            if found:
                first[doc] = found[0]
    out, last = [], ""
    seen: dict[Any, str] = {}
    for doc in docs:
        if doc not in seen:
            seen[doc] = first.get(doc) or last
            last = seen[doc]
        out.append(seen[doc])
    return out


def _sequencing_shadow(chart_name: str, mains: list[dict[str, Any]], names: dict[Any, str]) -> dict[str, Any]:
    """SEQUENCING_MODE shadow (default; Brief 2, 2026-11-22): the +-5 local repair runs beside today's
    rules on the same Main pages and continuity documents, and the moves it would make go to
    staging/sequencing_shadow.json (page names, how far each moved, held pages; no text). The written
    order stays the rules'. "ml" waits for the text model (Brief 2 Q3): the clue model alone fixes too
    few shuffles at a safe gain bar, so it runs as shadow and says so."""
    import json

    from common.config import SEQUENCING_MODE, staging_dir
    from modules.continuity.ml import clues as cclues
    from modules.continuity.ml import model as cmodel
    from modules.sequencing.ml import model as smodel
    from modules.sequencing.ml import repair as rp

    if SEQUENCING_MODE not in ("shadow", "ml") or len(mains) < 2:
        return {"mode": SEQUENCING_MODE if SEQUENCING_MODE in ("shadow", "ml") else "rules"}
    if smodel.load() is None:
        return {"mode": SEQUENCING_MODE, "ran": False, "reason": smodel.status().get("reason")}
    texts = [m["text"] or "" for m in mains]
    docs = [m.get("document") for m in mains]
    recs = [{"text": t, "L1": "", "L2": "", "visual": None} for t in texts]
    rows = cclues.chart_rows(recs)
    decisions = cmodel.decide(recs) or [None] * len(rows)
    dos = _encounter_dates(texts, docs)
    hard = {i: rp.hard_boundary(rows[i], (decisions[i] or {}).get("p_break"))
            for i in range(len(rows)) if docs[i] != docs[i + 1]}
    doc_index, k = [], -1
    for i, d in enumerate(docs):
        k += int(i == 0 or d != docs[i - 1])
        doc_index.append(k)
    groups = rp.repair_groups(doc_index, hard, dos)
    settings = _repair_settings()
    res = rp.repair(len(mains), smodel.scorer(texts), [smodel.page_info(t, d) for t, d in zip(texts, dos)],
                    groups, min_gain=settings["min_gain"], cut=settings["cut"])
    moves = [{"page": names.get(mains[p]["page_id"], str(mains[p]["page_id"])), "moved_by": k - p}
             for k, p in enumerate(res.order) if p != k]
    held = [names.get(mains[p]["page_id"], "") for p in res.held]
    summary = {"mode": SEQUENCING_MODE, "ran": True, "pages": len(mains), "groups": len(groups),
               "hard_boundaries": sum(hard.values()), "soft_boundaries": len(hard) - sum(hard.values()),
               "moved": len(moves), "held": len(held)}
    if SEQUENCING_MODE == "ml":
        summary["note"] = "ml waits for the text model (Brief 2 Q3): ran as shadow, the rules order is written"
    try:
        path = staging_dir(chart_name) / SHADOW_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "chart": chart_name, **summary, "settings": settings,
                                    "moves": moves, "held": held, "windows": res.windows}, indent=1), encoding="utf-8")
    except OSError as exc:
        logger.warning("page_sequencing chart=%s shadow file not written: %s", chart_name, exc)
    return summary


def _repair_settings() -> dict[str, float]:
    """min_gain and the mismatch cut, tuned on the val shuffle cases (modules.sequencing.ml.evaluate_synth),
    in the pair model's settings.json (``repair``); a safe default without it."""
    from modules.sequencing.ml import model as smodel

    loaded = smodel.load() or {}
    repair = (loaded.get("settings") or {}).get("repair") or {}
    return {"min_gain": float(repair.get("min_gain", 16.0)), "cut": float(repair.get("cut", 0.01))}

