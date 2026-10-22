"""Stage: blank / junk / duplicate classification.

Two passes, as the pipeline spec requires:

  pass 1  on preliminary (Tesseract) text, for printed + non-low-quality
          pages only. Handwritten and low-quality pages are skipped —
          prelim text is not trusted to judge them.
  pass 2  on final2 (Azure) text when present, else final1 (Docling /
          RapidOCR), for handwritten + low-quality pages plus any printed
          page pass 1 did not already rule out. A Visual page pass 1 judged
          is not judged again.

The verdict is ``method.decide`` (docs/BLANK_JUNK_LOGIC.md): rules R1-R4, then DistilBERT
alone (no keyword vote). Pass 1 removes blank pages, R1-R3 junk and the pages DistilBERT is
sure are junk (p(junk) > 0.95); every page it settles still gets Final OCR 1, which changes
nothing (no quality re-grade, no Final OCR 2, no later stage reads it). It also settles the
pages it is sure to keep (p(keep) > 0.95: pass 2 skips them);
every other page stays Main, gets final OCR, and DistilBERT decides it in pass 2. DistilBERT's
probabilities and the deciding rule go to ``<chart>/staging/blank_junk.json`` beside the
key/value extraction's ``extraction.json`` (not the database).

Duplicate detection (after the verdict, among Main and junk pages -- never blank):

* Compare each comparable page only to neighbors **±25** in page order.
* Pages are compared on their distinct words, everything on the page included
  (``classify.word_overlap``, 2026-10-22, user): the **overlap** is shared distinct
  words / all distinct words of the two pages; **contained** is the share of the
  smaller page's distinct words found on the other.
* Overlap **≥ 98%**, or contained ≥ ``duplicates.contained_at`` (canon, 100%):
  ``duplicate`` -- UI Duplicate "Yes", Blank / Junk
  "Yes (Duplicate)" -- whether the page was judged Main or junk (2026-10-22: junk
  pages were never compared, so a 98%-alike junk page read "Yes (Junk)", Duplicate
  "No"). **[95%, 98%)**: a partial duplicate -- the page keeps its verdict (Main or
  junk) with ``duplicate_of_page_id`` set; UI "Partial".
  2026-10-22 (user); before, only ≥ 98% counted and the UI split it 100% Yes /
  below May Be.
* The similarity is stored as ``confidence`` on both kinds of row.
* On a match, keep the page with the **higher character count** as the
  original; on a tie, keep the **earlier** page. The other is the duplicate
  (or partial duplicate). A Main page is never the copy of a junk page: when one
  of the pair is Main, it is the original.
* Blank pages and texts shorter than 300 normalized chars are never compared.
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
    get_blank_junk_reasons,
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
    pass1_ruled_out,
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
    DUPLICATE_PARTIAL_THRESHOLD,
    DUPLICATE_SIMILARITY_THRESHOLD,
    JUNK_CODES,
    classification_confidence,
    duplicate_char_count,
    text_is_comparable,
    word_overlap,
)
import method  # noqa: E402
from model_bridge import model_scores  # noqa: E402

STAGE = "blank_junk"

# Every rule and the vote live in method.py.


def _contained_at() -> float:
    """The canon's ``duplicates.contained_at`` (reloads on change): the share of the
    smaller page's distinct words that must be on the other page for a duplicate."""
    return float(method.canon().raw.get("duplicates", {}).get("contained_at", 1.0))


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
    best_matches: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Classify ``todo`` pages, then look for duplicates among Main and junk pages.

    ``inputs`` carries each page's method.PageInputs without the model scores, which
    are added here. Duplicates are checked only after the verdict, between Main and junk
    pages (never blank) within ±``DUPLICATE_NEIGHBOR_WINDOW`` in page order that are at
    least ``DUPLICATE_MIN_CHARS`` long; a Main page is never the copy of a junk page. A pair is a duplicate at ≥ 98% similarity, or when
    one page's text is wholly contained in the other's (confidence 1.0); a partial
    duplicate at [95%, 98%), which stays Main.

    ``prior_main_ids`` are pages already ``not_blank_junk`` from an earlier pass;
    they participate as comparison neighbors (and may be demoted to duplicate, or
    marked partial, when a longer page matches). Similarity is stored as
    ``confidence`` on duplicate and partial rows.

    ``best_matches`` (page name -> its highest match, see _write_votes) is filled
    for every page compared, including earlier-pass pages that get no row here.
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
        """Main and junk pages alike (2026-10-22: a 98%-alike page is a duplicate
        whatever its verdict); blank pages and short texts never."""
        text = texts.get(page_id) or ""
        if not text_is_comparable(text):
            return False
        if page_id in by_id:
            return by_id[page_id]["code"] != CODE_BLANK
        return page_id in prior

    def _is_main(page_id: int) -> bool:
        return by_id[page_id]["code"] == CODE_MAIN if page_id in by_id else page_id in prior

    # Phase 2 — each comparable page against the next ±window comparable pages.
    dup_of: dict[int, int] = {}
    dup_sim: dict[int, float] = {}
    dup_contained: dict[int, bool] = {}
    # [PARTIAL, THRESHOLD): the page's closest near-copy, applied after duplicates.
    partial_of: dict[int, int] = {}
    partial_sim: dict[int, float] = {}
    # Each page's highest match among the neighbors it was compared with, any score,
    # for blank_junk.json and duplicate_confidence: which page, its overlap and how
    # much of the smaller page is on the other.
    best: dict[int, dict[str, Any]] = {}
    contained_at = _contained_at()

    def _note_best(page_id: int, other_id: int, words: Any, score: float, contained: bool) -> None:
        if score > best.get(page_id, {}).get("similarity", -1.0):
            mine, theirs = (words.words_a, words.words_b) if page_id == pid else (words.words_b, words.words_a)
            best[page_id] = {
                "page_id": other_id,
                "similarity": round(score, 4),
                "overlap": round(words.overlap, 4),
                "contained_share": round(words.contained, 4),
                "contained": contained,
                "distinct_words": mine,
                "other_distinct_words": theirs,
                "shared_words": words.shared,
            }

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
            words = word_overlap(texts.get(pid) or "", texts.get(nid) or "")
            is_contained = words.contained >= contained_at
            # A contained pair scores 1.0; otherwise its overlap.
            sim = 1.0 if is_contained else words.overlap
            _note_best(pid, nid, words, sim, is_contained)
            _note_best(nid, pid, words, sim, is_contained)
            if sim < DUPLICATE_PARTIAL_THRESHOLD:
                continue
            kind = "duplicate" if sim >= DUPLICATE_SIMILARITY_THRESHOLD else "partial"
            orig, dup = _prefer_original(
                i,
                pid,
                duplicate_char_count(texts.get(pid) or ""),
                j,
                nid,
                duplicate_char_count(texts.get(nid) or ""),
            )
            # A Main page is never made the copy of a junk page: the Main one is the
            # original, and the junk one its duplicate.
            if _is_main(dup) and not _is_main(orig):
                orig, dup = dup, orig
            if kind == "partial":
                if sim > partial_sim.get(dup, 0.0):
                    partial_of[dup] = orig
                    partial_sim[dup] = sim
                continue
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
            match = "its distinct words all on the original"
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

    # Partial duplicates keep their own verdict -- Main or junk -- (its reason first,
    # so pass 2 still reads a pass-1 keep) and point at their near-copy.
    partial: set[int] = set()
    for page_id, orig_id in partial_of.items():
        if page_id in dup_of:
            continue  # a full duplicate of another page
        orig_id = _ultimate_original(dup_of, orig_id)
        if orig_id == page_id:
            continue
        sim = partial_sim[page_id]
        note = (
            f"partial_duplicate_of_page_id:{orig_id} (similarity={sim:.4f}, "
            f"{DUPLICATE_PARTIAL_THRESHOLD:.0%}-{DUPLICATE_SIMILARITY_THRESHOLD:.0%} "
            f"within ±{DUPLICATE_NEIGHBOR_WINDOW})"
        )
        own = by_id.get(page_id) or {}
        earlier = own.get("votes")
        by_id[page_id] = _row_for(
            page_by_id[page_id],
            code=own.get("code", CODE_MAIN),
            reason=f"{own['reason']}; {note}" if own.get("reason") else note,
            duplicate_of=orig_id,
            confidence=sim,
            votes={**earlier, "partial_duplicate": note} if earlier else None,
        )
        partial.add(page_id)

    named: dict[int, dict[str, Any]] = {}
    for page_id, match in best.items():
        other = page_by_id.get(match["page_id"]) or {}
        match = {key: value for key, value in match.items() if key != "_rank"}
        named[page_id] = {"page_name": other.get("page_name"), "page_number": other.get("page_number"), **match}
    for page_id, row in by_id.items():
        row["best_match"] = named.get(page_id)
        # The score review-ui's Duplicate confidence is built from: its best match.
        match = best.get(page_id)
        row["duplicate_confidence"] = None if match is None else match["similarity"]
    if best_matches is not None:
        best_matches.update({page_by_id[pid]["page_name"]: match for pid, match in named.items()})

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
        if pid in dup_of or pid in partial:
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
            inputs.content_bottom = bottom
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


def _write_votes(
    chart_name: str,
    pass_no: int,
    rows: list[dict[str, Any]],
    best_matches: Optional[dict[str, dict[str, Any]]] = None,
) -> Optional[Path]:
    """Every page's ballots and deciding rule for this pass, staged beside the chart::

        {"version": 1, "chart_name": …, "updated_at": …,
         "pages": {"<page name>": {"pass_1": {...}, "pass_2": {...}}}}

    Each pass entry also has ``best_match``: the page's highest duplicate score among
    the neighbors it was compared with, at any score -- ``page_name`` /
    ``page_number`` of that neighbor, ``similarity`` (the score that decides: 1.0 when
    contained, else the overlap), ``overlap`` (shared distinct words / all distinct
    words of the two), ``contained_share`` (the smaller page's distinct words found on
    the other), ``contained``, and the counts ``distinct_words`` /
    ``other_distinct_words`` / ``shared_words``. It is null when the page was never
    compared (too short, or blank). A page only compared against this pass (an
    earlier pass's Main page) gets an entry with ``best_match`` alone.

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
            "best_match": row.get("best_match"),
        }
    for page_name, match in (best_matches or {}).items():
        pages.setdefault(page_name, {}).setdefault(key, {})["best_match"] = match
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
                not_eligible = sorted(pass1_ruled_out(conn, chart_id) & ctx.todo)
                mark_skipped(conn, ctx, not_eligible, "blank_junk_pass1")
                # A Visual page (visual, < 20 words in the text pass 1 read) is decided in
                # pass 1 (R3); pass 2 does not judge it again. A visual page with more text
                # was judged like any other and goes on to pass 2.
                visual_done = [
                    pid for pid in ctx.todo
                    if pid in pass1
                    and method.text_is_visual(page_type(quality.get(pid)), prelim_texts.get(pid))
                ]
                mark_skipped(conn, ctx, visual_done, "visual_pass1")
                # DistilBERT was sure in pass 1 that the page is a keep (R5 keep, final):
                # pass 2 does not judge it again.
                reasons = get_blank_junk_reasons(conn, chart_id, pass_no=1)
                kept = [pid for pid in ctx.todo if method.is_final_keep(reasons.get(pid))]
                mark_skipped(conn, ctx, kept, "final_keep_pass1")
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
        best_matches: dict[str, dict[str, Any]] = {}
        classified = _classify(
            ctx.pages, texts, ctx.todo, prior_main_ids=prior_mains,
            inputs=inputs, docling_pages=docling_pages, best_matches=best_matches,
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
                    duplicate_confidence=row.get("duplicate_confidence"),
                )
                # Demoted prior-main pages are upserted but were not in todo.
                if row["page_id"] in ctx.todo:
                    mark_completed(conn, ctx, row["page_id"])
            mark_blank_junk_final(conn, chart_id)
            path = _rewrite_csv(conn, chart_id, ctx.chart_name)
        votes_file = _write_votes(ctx.chart_name, pass_no, classified, best_matches)

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

