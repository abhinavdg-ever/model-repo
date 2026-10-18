"""Stage: blank / junk / duplicate classification.

Two passes, as the pipeline spec requires:

  pass 1  on preliminary (Tesseract) text, for printed + non-low-quality
          pages only. Handwritten and low-quality pages are skipped —
          prelim text is not trusted to judge them.
  pass 2  on final2 (Azure) text when present, else final1 (Docling /
          RapidOCR), for handwritten + low-quality pages plus any printed
          page pass 1 did not already rule out. A Visual page pass 1 judged
          is not judged again.

The verdict is ``method.decide`` (docs/BLANK_JUNK_LOGIC.md): rules R1-R8, then
DistilBERT and the keyword canon vote (R9). Pass 1 only removes what it
is sure of; every other page stays Main and gets final OCR. The confidence is
the evidence for the verdict (DistilBERT, keywords, the rule), so a contested
call comes out low. Every ballot and the
deciding rule go to ``<chart>/staging/blank_junk.json`` beside the key/value
extraction's ``extraction.json`` (not the database).

Duplicate detection (after the verdict, among Main pages only):

* Compare each comparable page only to neighbors **±5** in page order.
* Match when normalized-text similarity is **≥ 98%** (SequenceMatcher, run only
  when the two lengths are within 10% of each other), or when one page's text
  is wholly contained in the other's.
* Store that ratio as ``confidence`` (UI: 100% → Yes, [98%, 100%) → May Be);
  a contained page scores 1.0.
* On a match, keep the page with the **higher character count** as the
  original; on a tie, keep the **earlier** page. The other is ``duplicate``.
* Blank / junk pages and texts shorter than 300 normalized chars are never
  compared.
* Prior-pass ``main`` pages are included as neighbors so pass 2 can still
  match a handwritten page to a printed one from pass 1.

Also: one final verdict per page (``mark_blank_junk_final``), and the junk CSV
is fully rewritten from the database after each pass.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from config import CORE_ROOT, page_image_path, staging_dir
from db import (
    NO_INK_HW_METHODS,
    connect,
    delete_blank_junk,
    get_blank_junk_flags,
    get_ocr_texts,
    get_quality_map,
    low_quality_page_ids,
    non_printed_page_ids,
    mark_blank_junk_final,
    page_blocks_prelim,
    page_type,
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

_JUNK_LIB = CORE_ROOT / "stages" / "lib" / "blank_junk"
if str(_JUNK_LIB) not in sys.path:
    sys.path.insert(0, str(_JUNK_LIB))

from classify import (  # noqa: E402
    CLASSIFICATION_LABELS,
    CODE_BLANK,
    CODE_DUPLICATE,
    CODE_JUNK,
    CODE_MAIN,
    DUPLICATE_NEIGHBOR_WINDOW,
    DUPLICATE_SIMILARITY_THRESHOLD,
    JUNK_CODES,
    classification_confidence,
    duplicate_char_count,
    text_is_comparable,
    text_is_contained,
    text_similarity,
)
import method  # noqa: E402
from model_bridge import model_scores  # noqa: E402

STAGE = "blank_junk"

# Every rule and the vote live in method.py.
# Two pages' lengths must be within this share of each other for SequenceMatcher.
DUPLICATE_LENGTH_RATIO = 0.10


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
    "duplicate_of_page",
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
    if code == CODE_JUNK:
        return "junk", None
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
    confidence: Optional[float] = None,
    votes: Optional[dict[str, Any]] = None,
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
        "confidence": float(confidence) if confidence is not None else _confidence(code),
        "label": CLASSIFICATION_LABELS.get(code, "Main"),
        "group": _page_group(code),
        "reason": reason,
        "votes": votes,
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


def _docling_page(raw: Optional[str]) -> Optional[dict[str, Any]]:
    """The stored Final OCR 1 page object (``{"markdown", "document", ...}``), or None."""
    text = (raw or "").strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _prior_main_ids(conn: Any, chart_id: int) -> set[int]:
    """Pages already judged main in an earlier pass (eligible duplicate targets)."""
    existing = get_blank_junk_flags(conn, chart_id)
    return {pid for pid, flag in existing.items() if flag == "not_blank_junk"}


def _classify(
    pages: list[dict[str, Any]],
    texts: dict[int, str],
    todo: set[int],
    prior_main_ids: Optional[set[int]] = None,
    *,
    inputs: Optional[dict[int, "method.PageInputs"]] = None,
    docling_pages: Optional[dict[int, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Classify ``todo`` pages, then look for duplicates among Main pages.

    ``inputs`` carries each page's method.PageInputs without the model scores, which
    are added here. Duplicates are checked only after the verdict, and only between
    Main pages within ±5 in page order that are at least ``DUPLICATE_MIN_CHARS``
    long. A pair matches at ≥ 98% similarity, or when one page's text is wholly
    contained in the other's (confidence 1.0).

    ``prior_main_ids`` are pages already ``not_blank_junk`` from an earlier pass;
    they participate as comparison neighbors (and may be demoted to duplicate
    when a longer later page matches). Similarity is stored as ``confidence``
    (1.0 → UI Yes; [0.98, 1.0) → May Be).
    """
    prior = set(prior_main_ids or ())
    by_id: dict[int, dict[str, Any]] = {}
    for page in pages:
        page_id = page["id"]
        if page_id not in todo:
            continue
        text = texts.get(page_id) or ""
        page_inputs = (inputs or {}).get(page_id) or method.PageInputs(text=text, pass_no=1)
        page_inputs.text = text
        scores = model_scores(text, docling=(docling_pages or {}).get(page_id)) if text.strip() else {}
        page_inputs.bert = scores.get("bert")
        verdict = method.decide(page_inputs)
        by_id[page_id] = _row_for(
            page, code=verdict.code, reason=verdict.reason, confidence=verdict.confidence,
            votes=verdict.votes,
        )

    def _comparable(page_id: int) -> bool:
        text = texts.get(page_id) or ""
        if not text_is_comparable(text):
            return False
        if page_id in by_id:
            return by_id[page_id]["code"] == CODE_MAIN
        return page_id in prior

    # Phase 2 — pairwise ±2 window among comparable pages.
    dup_of: dict[int, int] = {}
    dup_sim: dict[int, float] = {}
    dup_contained: dict[int, bool] = {}
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
            a_text, b_text = texts.get(pid) or "", texts.get(nid) or ""
            is_contained = text_is_contained(a_text, b_text)
            if is_contained:
                sim = 1.0
            else:
                a_len, b_len = duplicate_char_count(a_text), duplicate_char_count(b_text)
                if abs(a_len - b_len) > DUPLICATE_LENGTH_RATIO * max(a_len, b_len, 1):
                    continue
                sim = text_similarity(a_text, b_text)
                if sim < DUPLICATE_SIMILARITY_THRESHOLD:
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
                    if other in dup_sim:
                        dup_sim[other] = max(dup_sim[other], sim)
            prev = dup_sim.get(dup, 0.0)
            if sim >= prev:
                dup_of[dup] = orig
                dup_sim[dup] = sim
                dup_contained[dup] = is_contained

    # Apply duplicate marks (including demoting prior-main pages not in todo).
    page_by_id = {p["id"]: p for p in pages}
    for dup_id, orig_id in dup_of.items():
        orig_id = _ultimate_original(dup_of, orig_id)
        if dup_id == orig_id:
            continue
        sim = dup_sim.get(dup_id, DUPLICATE_SIMILARITY_THRESHOLD)
        if dup_contained.get(dup_id):
            match = "text contained in the original"
        else:
            match = f"similarity={sim:.4f} ≥{DUPLICATE_SIMILARITY_THRESHOLD:.0%}"
        reason = (
            f"duplicate_of_page_id:{orig_id} "
            f"({match} within ±{DUPLICATE_NEIGHBOR_WINDOW})"
        )
        page = page_by_id[dup_id]
        earlier = (by_id.get(dup_id) or {}).get("votes")
        by_id[dup_id] = _row_for(
            page,
            code=CODE_DUPLICATE,
            reason=reason,
            duplicate_of=orig_id,
            confidence=sim,
            votes={**earlier, "duplicate": reason} if earlier else None,
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
                "duplicate_of_page": row.get("duplicate_of_page") or "",
            }
        )
    return write_csv(imaging_csv(chart_name, "junk"), JUNK_CSV_COLS, out)


# HW-stage methods that mean "no ink on the page" (image_preprocess/hw_printed.py).
NO_INK_METHODS = NO_INK_HW_METHODS
PAGE_TAG_METHOD = "convnext_page_tags"


def _ocr_json(raw: Optional[str]) -> Optional[dict[str, Any]]:
    text = (raw or "").strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _page_lines(
    final2: Optional[str], final1: Optional[str], *, page: dict[str, Any], chart_name: str
) -> Optional[list["method.Line"]]:
    """The page's text lines with their height on the page (final2 word boxes, else final1)."""
    from stages.lib.extraction.ocr_input import page_for_extraction

    found = page_for_extraction(
        _ocr_json(final2), _ocr_json(final1),
        page_name=page["page_name"], page_number=page.get("page_number"),
        image_path=page_image_path(chart_name, page["page_name"]),
    )
    if not found or not found.get("height"):
        return None
    return method.lines_from_words(found["words"], float(found["height"])) or None


def _body_ink(chart_name: str, page_name: str) -> tuple[Optional[bool], Optional[float]]:
    """(ink below the short-page band, the share of that band that is ink): a picture,
    handwriting or stamp OCR did not read. (None, None) when the image cannot be read."""
    try:
        from PIL import Image

        from stages.lib.image_preprocess.hw_printed import _image_bgr, page_marks

        short = method.canon().raw["short_page"]
        top, bottom = (float(v) for v in short["body_ink_band"])
        with Image.open(page_image_path(chart_name, page_name)) as image:
            width, height = image.size
            band = image.convert("RGB").crop((0, int(top * height), width, int(bottom * height)))
        marks = page_marks(_image_bgr(band))
        fraction = float(marks["strong_fraction"])
        return fraction >= float(short["body_ink_max_fraction"]), round(fraction, 6)
    except Exception as exc:  # noqa: BLE001 - a missing image only switches the rule off
        logger.warning("short-page ink check failed for %s/%s: %s", chart_name, page_name, exc)
        return None, None


def _page_inputs(
    ctx: Any,
    pass_no: int,
    quality: dict[int, dict[str, Any]],
    *,
    final2: Optional[dict[int, str]] = None,
    final1: Optional[dict[int, str]] = None,
    prelim: Optional[dict[int, str]] = None,
) -> dict[int, "method.PageInputs"]:
    """Everything method.decide needs per page except the text and the model scores."""
    signed: set[str] = set()
    signature_known = False
    if pass_no == 2:
        from db.paths import imaging_csv
        from stages.lib.continuity.signature import signed_page_names

        signed = signed_page_names(ctx.chart_name)
        signature_known = imaging_csv(ctx.chart_name, "provider_signature").is_file()
    top = float(method.canon().raw["short_page"]["top_fraction"])
    out: dict[int, method.PageInputs] = {}
    for page in ctx.pages:
        pid = page["id"]
        if pid not in ctx.todo:
            continue
        q = quality.get(pid) or {}
        area = q.get("handwritten_area_pct")
        inputs = method.PageInputs(
            text="",
            pass_no=pass_no,
            document_type=page_type(q),
            page_tags=str(q.get("hw_method") or "") == PAGE_TAG_METHOD,
            hw_area_pct=float(area) if area is not None else None,
            signed=page["page_name"] in signed,
            signature_known=signature_known,
            no_ink=str(q.get("hw_method") or "") in NO_INK_METHODS,
        )
        if pass_no == 2:
            inputs.lines = _page_lines(
                (final2 or {}).get(pid), (final1 or {}).get(pid), page=page, chart_name=ctx.chart_name
            )
            bottom = method.content_bottom(inputs.lines) if inputs.lines else None
            if bottom is not None and bottom <= top:
                inputs.body_ink, inputs.body_ink_fraction = _body_ink(ctx.chart_name, page["page_name"])
            # Tesseract's text for the junk terms the final OCR missed; only where prelim is
            # trusted (printed, not low quality).
            if not page_blocks_prelim(q):
                inputs.prelim_text = unwrap_ocr_content((prelim or {}).get(pid))
        out[pid] = inputs
    return out


VOTES_FILE = "blank_junk.json"
VOTES_VERSION = 1


def votes_path(chart_name: str) -> Path:
    return staging_dir(chart_name) / VOTES_FILE


def _write_votes(chart_name: str, pass_no: int, rows: list[dict[str, Any]]) -> Optional[Path]:
    """Every page's ballots and deciding rule for this pass, staged beside the chart::

        {"version": 1, "chart_name": …, "updated_at": …,
         "pages": {"<page name>": {"pass_1": {...}, "pass_2": {...}}}}

    This pass's entries are replaced and the other pass's kept, so after a full run the
    file holds both passes.
    """
    from datetime import datetime, timezone

    path = votes_path(chart_name)
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        existing = {}
    if existing.get("version") != VOTES_VERSION:
        existing = {}
    pages: dict[str, Any] = existing.get("pages") or {}
    key = f"pass_{pass_no}"
    for page in pages.values():
        page.pop(key, None)
    for row in rows:
        if row.get("votes") is None:
            continue
        pages.setdefault(row["page_name"], {})[key] = {
            **row["votes"],
            "verdict": row["label"],
            "confidence": round(float(row["confidence"]), 4),
            "reason": row["reason"],
        }
    payload = {
        "version": VOTES_VERSION,
        "chart_name": chart_name,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pages": pages,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("could not write %s: %s", path, exc)
        return None
    return path


def _run_pass(
    chart_id: int,
    pass_no: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, pass_no=pass_no, force=force) as ctx:
        with connect() as conn:
            todo_before = set(ctx.todo)
            non_printed = non_printed_page_ids(conn, chart_id)
            low_ids = low_quality_page_ids(conn, chart_id)
            quality = get_quality_map(conn, chart_id)
            # Final OCR 1 page objects, so the model reads them as b_jnk v3 does.
            docling_pages: dict[int, dict[str, Any]] = {}

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
                # Read for junk terms only (method.score_text also_junk).
                prelim_texts = get_ocr_texts(conn, chart_id, "tesseract")
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
                        page_obj = _docling_page(final1.get(pid))
                        if page_obj is not None:
                            docling_pages[pid] = page_obj
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
                # A Visual page is decided in pass 1 (R8); pass 2 does not judge it again.
                visual_done = [
                    pid for pid in ctx.todo
                    if pid in pass1 and page_type(quality.get(pid)) == "visual"
                ]
                mark_skipped(conn, ctx, visual_done, "visual_pass1")
                no_text = [
                    pid for pid in ctx.todo if not (texts.get(pid) or "").strip()
                ]
                mark_skipped(conn, ctx, no_text, "no_final_ocr_text")

            # A page this pass skips must not keep this pass's verdict from an
            # earlier run — as the highest pass it would override the real one.
            delete_blank_junk(
                conn, chart_id, pass_no, sorted(todo_before - ctx.todo)
            )
            prior_mains = _prior_main_ids(conn, chart_id)

        if pass_no == 2:
            inputs = _page_inputs(
                ctx, pass_no, quality, final2=final2, final1=final1,
                prelim=prelim_texts,
            )
        else:
            inputs = _page_inputs(ctx, pass_no, quality)
        classified = _classify(
            ctx.pages, texts, ctx.todo, prior_main_ids=prior_mains,
            inputs=inputs, docling_pages=docling_pages,
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
        votes_file = _write_votes(ctx.chart_name, pass_no, classified)

        counts: dict[str, int] = {}
        for row in classified:
            counts[row["group"]] = counts.get(row["group"], 0) + 1

        return {
            "chart_id": chart_id,
            "pass_no": pass_no,
            "junk_csv": str(path),
            "votes_json": str(votes_file) if votes_file else None,
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

