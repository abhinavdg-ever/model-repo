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

Duplicate detection (after the verdict, among Main pages only -- never blank or junk):

* Compare each comparable page with the pages after it, up to 25 pages or 10% of the
  chart, whichever is more (``classify.duplicate_window``); each pair once.
* Pages are compared on their distinct words, everything on the page included
  (``classify.word_overlap``, 2026-10-22, user): the **overlap** is shared distinct
  words / all distinct words of the two pages; **contained** is the share of the
  smaller page's distinct words found on the other.
* Contained (canon ``duplicates.contained_at``, 100%), overlap **≥ 98%**, or fewer than
  ``duplicate_below_words`` (8) distinct words different, with lengths within 10%:
  ``duplicate`` -- UI Duplicate "Duplicate (To Page N)", Blank / Junk "Yes (Junk)"
  (2026-10-22, user). Overlap **[95%, 98%)** or fewer than ``partial_below_words``
  (15) distinct words different: a partial duplicate -- the page stays Main with
  ``duplicate_of_page_id`` set; UI "Partial (To Page N)".
* The similarity is stored as ``confidence`` on both kinds of row.
* On a match the **later** page (order of appearance) is the duplicate (or partial
  duplicate) -- except a contained pair whose larger page has more than
  ``duplicates.larger_kept_above_words`` (10) distinct words the smaller lacks: the
  larger page stays and the smaller is the duplicate (2026-10-22, user).
* Blank pages and texts shorter than 300 normalized chars are never compared.
* A signature or ending page (kv_extract's Electronic Signature in pass 2, or
  ``duplicates.signature_terms`` on the page) is not a duplicate of a look-alike with a
  Main page between them -- other notes, other DOSs (2026-10-26, user).
* Two pages whose kv_extract DOS share no date are not duplicates or partials, however
  alike their words (2026-10-26, user).
* Pass 1 marks only exact copies: the same distinct words, lengths within 10%. Every
  other rule above (98%, contained, word counts, partials, DOS, signature pages) runs in
  pass 2 only (2026-10-26, user).
* Prior-pass ``main`` pages are included as neighbors so pass 2 can still
  match a handwritten page to a printed one from pass 1.

Also: one final verdict per page (``mark_blank_junk_final``), and the junk CSV
is fully rewritten from the database after each pass.
"""
from __future__ import annotations

import json
import logging
import re
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
    duplicate_window,
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


def _duplicate_rules() -> dict[str, float]:
    """The canon's ``duplicates`` block (reloads on change), with its defaults:
    ``contained_at`` (share of the smaller page's distinct words that must be on the
    other), ``duplicate_below_words`` / ``partial_below_words`` (fewer distinct words
    different than this is a duplicate / partial whatever the overlap), and
    ``larger_kept_above_words`` (a contained pair whose larger page has more distinct
    words than this beyond the smaller keeps the larger page; otherwise the later page
    is the duplicate)."""
    cfg = method.canon().raw.get("duplicates", {})
    return {
        "contained_at": float(cfg.get("contained_at", 1.0)),
        "duplicate_below_words": int(cfg.get("duplicate_below_words", 8)),
        "partial_below_words": int(cfg.get("partial_below_words", 15)),
        "count_rule_min_overlap": float(cfg.get("count_rule_min_overlap", 0.80)),
        "duplicate_min_length_ratio": float(cfg.get("duplicate_min_length_ratio", 0.90)),
        "larger_kept_above_words": int(cfg.get("larger_kept_above_words", 10)),
    }


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
    signed_ids: Optional[set[int]] = None,
    dos_by_id: Optional[dict[int, frozenset[str]]] = None,
    exact_only: bool = False,
) -> list[dict[str, Any]]:
    """Classify ``todo`` pages, then look for duplicates among Main pages.

    ``inputs`` carries each page's method.PageInputs without the model scores, which
    are added here. Duplicates are checked only after the verdict, between Main pages
    within ``duplicate_window`` pages of each other that are at least
    ``DUPLICATE_MIN_CHARS`` long, on their distinct words (see the module docstring for
    the duplicate / partial rules).

    ``prior_main_ids`` are pages already ``not_blank_junk`` from an earlier pass;
    they participate as comparison neighbors (and may be demoted to duplicate, or
    marked partial, when a longer page matches). Similarity is stored as
    ``confidence`` on duplicate and partial rows.

    ``best_matches`` (page name -> its highest match, see _write_votes) is filled
    for every page compared, including earlier-pass pages that get no row here.

    A signature or ending page (``signed_ids``: kv_extract's Electronic Signature, or the
    canon's ``duplicates.signature_terms`` on the page) standing apart from its look-alike
    is not a duplicate: a pair with one is skipped when a Main page lies between them
    (another note, another DOS; 2026-10-26, user). Next to each other they still compare.

    ``dos_by_id`` (pass 2: kv_extract's selected DOS per page): two pages that both have
    a DOS and share none are never duplicates or partials (2026-10-26, user).

    ``exact_only`` (pass 1): only exact copies -- the same distinct words, lengths within
    10% -- are duplicates; no partials. The rest waits for pass 2 (2026-10-26, user).
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
        verdict = method.post_process(method.decide(page_inputs), page_inputs)
        by_id[page_id] = _row_for(
            page, code=verdict.code, reason=verdict.reason, confidence=verdict.confidence,
            votes=verdict.votes,
        )

    signed = set(signed_ids or ())
    terms = method.canon().raw.get("duplicates", {}).get("signature_terms") or []
    signature_terms = (
        re.compile("|".join(rf"\b{re.escape(term)}\b" for term in terms), re.IGNORECASE) if terms else None
    )

    def _signature_page(page_id: int) -> bool:
        return page_id in signed or bool(signature_terms and signature_terms.search(texts.get(page_id) or ""))

    def _main(page_id: int) -> bool:
        if page_id in by_id:
            return by_id[page_id]["code"] == CODE_MAIN
        return page_id in prior

    def _comparable(page_id: int) -> bool:
        """Main pages only (2026-10-22, user): blank and junk pages, and short texts,
        are never compared."""
        text = texts.get(page_id) or ""
        if not text_is_comparable(text):
            return False
        if page_id in by_id:
            return by_id[page_id]["code"] == CODE_MAIN
        return page_id in prior

    # Phase 2 — each comparable page against the comparable pages up to ``window``
    # after it: each pair once. Comparability and lengths are worked out once per page.
    dup_of: dict[int, int] = {}
    dup_sim: dict[int, float] = {}
    dup_contained: dict[int, bool] = {}
    # Distinct words different, for the reason a duplicate / partial row carries.
    pair_words: dict[int, int] = {}
    # [PARTIAL, THRESHOLD): the page's closest near-copy, applied after duplicates.
    partial_of: dict[int, int] = {}
    partial_sim: dict[int, float] = {}
    # Each page's highest match among the neighbors it was compared with, any score,
    # for blank_junk.json and duplicate_confidence: which page, its overlap and how
    # much of the smaller page is on the other.
    best: dict[int, dict[str, Any]] = {}
    rules = _duplicate_rules()

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
                "different_words": words.different,
            }

    n = len(pages)
    window = duplicate_window(n)
    comparable = [_comparable(page["id"]) for page in pages]
    length = {page["id"]: duplicate_char_count(texts.get(page["id"]) or "")
              for page, ok in zip(pages, comparable) if ok}
    for i, page in enumerate(pages):
        pid = page["id"]
        if not comparable[i]:
            continue
        for j in range(i + 1, min(n, i + window + 1)):
            if not comparable[j]:
                continue
            nid = pages[j]["id"]
            if (_signature_page(pid) or _signature_page(nid)) and any(
                _main(pages[k]["id"]) for k in range(i + 1, j)
            ):
                continue  # a signature / ending page apart from its look-alike
            dates_a, dates_b = (dos_by_id or {}).get(pid), (dos_by_id or {}).get(nid)
            if dates_a and dates_b and not dates_a & dates_b:
                continue  # different dates of service: different visits
            words = word_overlap(texts.get(pid) or "", texts.get(nid) or "")
            is_contained = words.contained >= rules["contained_at"]
            # A contained pair scores 1.0; otherwise its overlap.
            sim = 1.0 if is_contained else words.overlap
            _note_best(pid, nid, words, sim, is_contained)
            _note_best(nid, pid, words, sim, is_contained)
            # 2026-10-22 (user): contained, >= 98% overlap or fewer than 8 distinct words
            # different -> duplicate; 95-98% or fewer than 15 -> partial; else neither.
            # The word counts count only between pages that are mostly the same: two short
            # unrelated pages also differ in few words.
            counts = words.overlap >= rules["count_rule_min_overlap"]
            # A duplicate is about as long as its original (2026-10-22, user): a page
            # inside a much longer one is a partial at most.
            len_a, len_b = length[pid], length[nid]
            similar_length = min(len_a, len_b) >= rules["duplicate_min_length_ratio"] * max(len_a, len_b)
            if exact_only:
                if not (similar_length and words.different == 0):
                    continue
                kind = "duplicate"
            elif similar_length and (
                is_contained
                or sim >= DUPLICATE_SIMILARITY_THRESHOLD
                or (counts and words.different < rules["duplicate_below_words"])
            ):
                kind = "duplicate"
            elif sim >= DUPLICATE_PARTIAL_THRESHOLD or (
                counts and words.different < rules["partial_below_words"]
            ):
                kind = "partial"
            else:
                continue
            # 2026-10-22 (user): the later page, in order of appearance, is the duplicate
            # -- unless one page is contained in the other and the larger adds more than
            # larger_kept_above_words distinct words: the larger page stays.
            if is_contained and words.different > rules["larger_kept_above_words"]:
                larger_first = words.words_a > words.words_b
                orig, dup = (pid, nid) if larger_first else (nid, pid)
            else:
                orig, dup = pid, nid
            if kind == "partial":
                if sim > partial_sim.get(dup, 0.0) or dup not in partial_of:
                    partial_of[dup] = orig
                    partial_sim[dup] = sim
                    pair_words[dup] = words.different
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
                pair_words[dup] = words.different

    # Apply duplicate marks (including demoting prior-main pages not in todo).
    page_by_id = {p["id"]: p for p in pages}
    for dup_id, orig_id in dup_of.items():
        orig_id = _ultimate_original(dup_of, orig_id)
        if dup_id == orig_id:
            continue
        sim = dup_sim.get(dup_id, DUPLICATE_SIMILARITY_THRESHOLD)
        match = (
            "its distinct words all on the original" if dup_contained.get(dup_id)
            else f"overlap={sim:.4f}"
        ) + f", {pair_words.get(dup_id, 0)} distinct words different"
        reason = (
            f"duplicate_of_page_id:{orig_id} "
            f"({match} within {window} pages)"
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

    # Partial duplicates stay Main (their own reason first, so pass 2 still reads a
    # pass-1 keep) and point at their near-copy.
    partial: set[int] = set()
    for page_id, orig_id in partial_of.items():
        if page_id in dup_of:
            continue  # a full duplicate of another page
        orig_id = _ultimate_original(dup_of, orig_id)
        if orig_id == page_id:
            continue
        sim = partial_sim[page_id]
        note = (
            f"partial_duplicate_of_page_id:{orig_id} (overlap={sim:.4f}, "
            f"{pair_words.get(page_id, 0)} distinct words different "
            f"within {window} pages)"
        )
        own = by_id.get(page_id) or {}
        earlier = own.get("votes")
        by_id[page_id] = _row_for(
            page_by_id[page_id],
            code=own.get("code", CODE_MAIN),
            reason=f"{own['reason']}; {note}" if own.get("reason") else note,
            duplicate_of=orig_id,
            # Its verdict is still its own (Main): so is its confidence. The similarity
            # is the duplicate_confidence.
            confidence=own.get("confidence", sim),
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


def extracted_page_names(chart_name: str, *fields: str) -> set[str]:
    """Page names kv_extract selected any of ``fields`` on (staging/extraction.json)."""
    from stages.lib.extraction import staging

    staged = staging.read(chart_name)
    if staged is None:
        return set()
    return {page.name for page in staged if any(page.selected(field) for field in fields)}


def extracted_dos(chart_name: str) -> dict[str, frozenset[str]]:
    """Page name -> the dates of kv_extract's selected DOS on it (from and to, ISO form)."""
    from stages.lib.extraction import staging
    from stages.lib.extraction.util.dates import canonical_date

    staged = staging.read(chart_name)
    if staged is None:
        return {}
    out: dict[str, frozenset[str]] = {}
    for page in staged:
        dates = {canonical_date(value) for row in page.selected("dos")
                 for value in (row.get("dos_from"), row.get("dos_to")) if value}
        if dates:
            out[page.name] = frozenset(dates)
    return out


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
    dated: set[str] = set()
    with_provider: set[str] = set()
    signature_known = False
    if pass_no == 2:
        from db.paths import imaging_csv
        from stages.lib.continuity.signature import signed_page_names

        signed = signed_page_names(ctx.chart_name)
        signature_known = imaging_csv(ctx.chart_name, "provider_signature").is_file()
        dated = extracted_page_names(ctx.chart_name, "dos")
        with_provider = extracted_page_names(ctx.chart_name, "provider_name", "provider_credentials")
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
            dated=page["page_name"] in dated,
            provider=page["page_name"] in with_provider,
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


# Every pass's full ballots (DistilBERT, rules, the best duplicate match), for debugging.
DETAIL_FILE = "blank_junk_detail.json"
# blank_junk.json lists a page's closest neighbor only from this similarity up.
CLOSEST_FROM = 0.70


def _duplicate_view(entry: dict[str, Any]) -> dict[str, Any]:
    """Duplicate / Partial / No, and the closest page when it is at least 70% alike."""
    reason = str(entry.get("reason") or "")
    status = (
        "Duplicate" if entry.get("verdict") == "Duplicate"
        else "Partial" if "partial_duplicate_of_page_id:" in reason
        else "No"
    )
    out: dict[str, Any] = {"status": status}
    match = entry.get("best_match") or {}
    similarity = match.get("similarity")
    if similarity is not None and similarity >= CLOSEST_FROM:
        out["closest_page"] = match.get("page_name")
        out["similarity"] = similarity
    return out


def _pass_view(entry: dict[str, Any]) -> dict[str, Any]:
    """One pass of one page, in the fields a reviewer reads (2026-10-23, user)."""
    if "verdict" not in entry:
        # Only compared against this pass's pages (judged in an earlier pass).
        return {"judged": False, "duplicate": _duplicate_view(entry)}
    bert = entry.get("bert") or {}
    short = entry.get("short_page")
    return {
        "p_junk": bert.get("p_junk"),
        "p_keep": bert.get("p_keep"),
        "empty": entry.get("empty"),
        "text_ends_at": short.get("content_bottom") if isinstance(short, dict) else None,
        "card_keywords": entry.get("card_keywords") or [],
        "result": entry.get("verdict"),
        "rule": entry.get("rule"),
        "why": entry.get("reason"),
        "duplicate": _duplicate_view(entry),
    }


def _simple(detail_pages: dict[str, Any], passes_run: list[int]) -> dict[str, Any]:
    """blank_junk.json's pages from the detail: pass_1, pass_2 and the final decision."""
    out: dict[str, Any] = {}
    for page_name, passes in detail_pages.items():
        page: dict[str, Any] = {}
        for pass_no in (1, 2):
            entry = passes.get(f"pass_{pass_no}")
            if entry is not None:
                page[f"pass_{pass_no}"] = _pass_view(entry)
            elif pass_no == 2 and 2 in passes_run and "pass_1" in passes:
                page["pass_2"] = "not judged: settled in pass 1"
        judged = [
            (pass_no, passes[f"pass_{pass_no}"]) for pass_no in (2, 1)
            if "verdict" in (passes.get(f"pass_{pass_no}") or {})
        ]
        if judged:
            pass_no, entry = judged[0]
            page["final"] = {
                "decision": entry.get("verdict"),
                "rule": entry.get("rule"),
                "why": entry.get("reason"),
                "pass": pass_no,
            }
        out[page_name] = page
    return out


def _write_votes(
    chart_name: str,
    pass_no: int,
    rows: list[dict[str, Any]],
    best_matches: Optional[dict[str, dict[str, Any]]] = None,
) -> Optional[Path]:
    """Stage this pass beside the chart, in two files.

    ``blank_junk.json`` (2026-10-23, user: simple): per page, ``pass_1`` / ``pass_2`` --
    p_junk, p_keep, empty, text_ends_at (where the text stops, pass 2), card_keywords,
    result, rule, why, and duplicate (Duplicate / Partial / No; the closest page and its
    similarity when at least 70% alike) -- and ``final`` (decision, rule, why, pass). A
    page pass 1 settled reads "not judged: settled in pass 1" for pass 2.

    ``blank_junk_detail.json``: every ballot of every pass as decided, with each page's
    ``best_match`` (the neighbor, similarity, overlap, contained_share, contained and the
    word counts). A page only compared against this pass gets ``best_match`` alone.

    This pass's entries are replaced and the other pass's kept, so after a full run both
    files hold both passes.
    """
    from datetime import datetime, timezone

    path = votes_path(chart_name)
    detail_path = path.with_name(DETAIL_FILE)
    try:
        existing = json.loads(detail_path.read_text(encoding="utf-8")) if detail_path.is_file() else {}
    except (OSError, ValueError):
        existing = {}
    if existing.get("version") != VOTES_VERSION:
        existing = {}
    pages: dict[str, Any] = existing.get("pages") or {}
    passes_run = sorted({*existing.get("passes_run", []), pass_no})
    key = f"pass_{pass_no}"
    for page in pages.values():
        page.pop(key, None)
    for row in rows:
        pages.setdefault(row["page_name"], {})[key] = {
            **(row.get("votes") or {}),
            "rule": (row.get("votes") or {}).get("rule") or ("duplicate" if row["flag"] == "duplicate" else None),
            "verdict": row["label"],
            "confidence": round(float(row["confidence"]), 4),
            "reason": row["reason"],
            "best_match": row.get("best_match"),
        }
    for page_name, match in (best_matches or {}).items():
        pages.setdefault(page_name, {}).setdefault(key, {})["best_match"] = match
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    detail = {
        "version": VOTES_VERSION,
        "chart_name": chart_name,
        "updated_at": updated_at,
        "passes_run": passes_run,
        "pages": pages,
    }
    simple = {
        "version": 2,
        "chart_name": chart_name,
        "updated_at": updated_at,
        "pages": _simple(pages, passes_run),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        for target, body in ((detail_path, detail), (path, simple)):
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(body, indent=1, default=str), encoding="utf-8")
            tmp.replace(target)
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
        signed_ids: set[int] = set()
        dos_by_id: dict[int, frozenset[str]] = {}
        if pass_no == 2:
            from stages.lib.continuity.signature import signed_page_names

            signed_names = signed_page_names(ctx.chart_name)
            signed_ids = {page["id"] for page in ctx.pages if page["page_name"] in signed_names}
            staged_dos = extracted_dos(ctx.chart_name)
            dos_by_id = {page["id"]: staged_dos[page["page_name"]]
                         for page in ctx.pages if staged_dos.get(page["page_name"])}
        classified = _classify(
            ctx.pages, texts, ctx.todo, prior_main_ids=prior_mains,
            inputs=inputs, docling_pages=docling_pages, best_matches=best_matches,
            signed_ids=signed_ids, dos_by_id=dos_by_id,
            exact_only=pass_no == 1,
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

