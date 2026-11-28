"""Stage: blank / junk / duplicate classification.

Two passes, as the pipeline spec requires:

  pass 1  on preliminary (Tesseract) text, for printed + non-low-quality
          pages only. Handwritten and low-quality pages are skipped —
          prelim text is not trusted to judge them.
  pass 2  on final2 (Azure) text when present, else final1 (Docling /
          RapidOCR), for handwritten + low-quality pages plus any printed
          page pass 1 did not already rule out. A Visual page pass 1 judged
          is not judged again.

The verdict is ``method.decide``: rules R1-R4, then DistilBERT
alone (no keyword vote). Pass 1 removes blank pages, R1-R3 junk (R3: strong cards) and the pages DistilBERT is
sure are junk (p(junk) > 0.95); every page it settles still gets Final OCR 1, which changes
nothing (no quality re-grade, no Final OCR 2, no later stage reads it). It also settles the
pages it is sure to keep (p(keep) > 0.95: pass 2 skips them);
every other page stays Main, gets final OCR, and DistilBERT decides it in pass 2. DistilBERT's
probabilities and the deciding rule go to ``<chart>/staging/blank_junk.json`` beside the
key/value extraction's ``extraction.json`` (not the database).

Duplicate detection (after the verdict, among Main pages only -- never blank or junk):
``dup_rules.find`` (2026-11-11, user: Duplicate_Logic.md). Body text only (header / footer
lines out), each page against its 3 most similar earlier pages; hard blocks (length, dates and
numbers, signature, short signed page, image); exact at any distance, only exact within 3
pages, runs (Rule A / Rule B) further apart, conflicts flagged. Pass 1 marks exact copies
within 3 pages; pass 2 applies every rule. A 100% text match a block stops is "Copy of N"
(Main, ``duplicate_content_of_page_id:``). Continuity applies the date-of-service block.

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

from common.config import CORE_ROOT, page_image_path, staging_dir
from common.db import (
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
from common.db.paths import imaging_csv, write_csv
from modules import confidence as conf
from apps.worker.stages._support import (
    BJ_EXCLUDE,
    best_page_text,
    mark_completed,
    mark_failed,
    mark_skipped as _mark_skipped,
    pass1_ruled_out,
    stage_run,
    unwrap_ocr_content,
)

logger = logging.getLogger(__name__)

_JUNK_LIB = CORE_ROOT / "modules" / "blank_junk"
if str(_JUNK_LIB) not in sys.path:
    sys.path.insert(0, str(_JUNK_LIB))

from classify import (  # noqa: E402
    CLASSIFICATION_LABELS,
    CODE_BLANK,
    CODE_DUPLICATE,
    CODE_JUNK,
    CODE_MAIN,
    CODE_OTHERS,
    JUNK_CODES,
    classification_confidence,
    unique_words,
)
import dup_rules  # noqa: E402
import method  # noqa: E402
from model_bridge import model_scores  # noqa: E402

STAGE = "blank_junk"

# Every rule and the vote live in method.py.


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
    "cards": "Cards",
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
    """A row given no confidence: nothing judged it with a model (2026-11-14): backup."""
    return conf.backup()


def _strength(code: int) -> float:
    """The pre-2026-11-14 stand-in (classify.classification_confidence), kept as the weight."""
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
    strength: Optional[float] = None,
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
        # The pre-2026-11-14 confidence: continuity's junk-run weight (decision_confidence in
        # blank_junk.json), so the new confidences change no document break.
        "strength": float(strength) if strength is not None else (
            float(confidence) if confidence is not None else _strength(code)),
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


# The reason a short page carries (2026-11-08, user; continuity keeps its junk inside a note).
# Was "short_page_contained_in_page_id:" (2026-11-03), which continuity still reads on old rows.
SHORT_MARK = "short_page:"
# A page that is a Copy of an earlier one: Main, pointing at it (2026-11-11, user: "Copy of N",
# a 100% text match a hard block kept from being a duplicate). review-ui reads the mark.
SAME_CONTENT_MARK = "duplicate_content_of_page_id:"
# Two partial matches to one original (2026-11-11, user): neither a duplicate, both for review.
REVIEW_MARK = "duplicate_review: "

# The short page's pre-2026-11-14 confidence, now only its weight; its confidence is a backup.
CONTAINED_CONFIDENCE = 0.90

def _signature_key(signature: Optional[dict[str, str]]) -> tuple[str, str]:
    """kv_extract's signature block, compared between two pages: (provider name, signature date)."""
    if not signature:
        return ("", "")
    name = re.sub(r"[^a-z0-9]+", " ", str(signature.get("provider_name") or "").lower()).strip()
    return (name, str(signature.get("signature_date") or "").strip())


def duplicate_record(out: "dup_rules.Outcome", page_by_id: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """The fields recorded for each page (Duplicate_Logic.md section 8)."""
    number = (page_by_id.get(out.original) or {}).get("page_number") if out.original is not None else None
    label = (f"Duplicate of {number}" if out.is_duplicate
             else f"Copy of {number}" if out.label == "copy" else "")
    return {
        "label": label,
        "duplicate": out.is_duplicate,
        "copy_of": number if out.label in ("duplicate", "copy") else None,
        "rule": out.rule if out.is_duplicate else "",
        "score": round(out.score, 4) if out.label else (out.closest or {}).get("similarity"),
        "blocked_by": out.blocked_by or ("" if out.label else (out.closest or {}).get("blocked_by", "")),
        "review": out.review,
    }


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
    signatures: Optional[dict[int, dict[str, str]]] = None,
    needs_image: Optional[set[int]] = None,
    image_paths: Optional[dict[int, Path]] = None,
    find_duplicates: bool = True,
    exact_near: bool = False,
    headers_by_id: Optional[dict[int, tuple[str, ...]]] = None,
    engine_texts: Optional[dict[int, dict[str, str]]] = None,
    chart_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Classify ``todo`` pages, then look for duplicates among Main pages (dup_rules.find).
    ``chart_id``: for the LLM judge's token usage (BLANK_JUNK_LLM, llm_judge.py).
    ``engine_texts``: each page's text per OCR engine, for the short page's comparisons.

    ``inputs`` carries each page's method.PageInputs without the model scores, which
    are added here. ``prior_main_ids`` are pages already ``not_blank_junk`` from an earlier
    pass: they are compared too, and may become a duplicate or a Copy of an earlier page.

    ``signed_ids`` / ``signatures``: kv_extract's Electronic Signature per page (blocks 4 and 5);
    ``needs_image``: form and handwritten pages, ``image_paths``: the page images (block 6).
    ``best_matches`` (page name -> its closest page and duplicate record, see _write_votes) is
    filled for every page compared.

    ``exact_near`` (pass 1): exact copies within 3 pages only. ``find_duplicates`` (pass 2):
    every rule. ``headers_by_id``: each page's section headers (stage 6; texts and canon names,
    in page order) for the short page.
    """
    prior = set(prior_main_ids or ())
    signed = set(signed_ids or ())
    # The previous / next non-blank page of each page (2026-11-08, user): a page with no text,
    # or one stage 1 found no ink on, is skipped over.
    order = [page["id"] for page in pages]

    def _blank(page_id: int) -> bool:
        return (not re.search(r"[A-Za-z0-9]", texts.get(page_id) or "")
                or bool(getattr((inputs or {}).get(page_id), "no_ink", False)))

    around: dict[int, tuple[Optional[int], Optional[int]]] = {}
    last: Optional[int] = None
    for page_id in order:
        around[page_id] = (last, None)
        if not _blank(page_id):
            last = page_id
    following: Optional[int] = None
    for page_id in reversed(order):
        around[page_id] = (around[page_id][0], following)
        if not _blank(page_id):
            following = page_id

    # A signature in the same spot on the previous or next non-blank page (2026-11-15, user): the same
    # region (header / mid / footer) and the same sentence or signer -- a stamp the record repeats,
    # not a page's own signing. It replaces "the previous / next page is signed too" (2026-11-08):
    # only a repeated signature stops the signature keep, on every page.
    sigs = signatures or {}
    near = [pid for pid in order if not _blank(pid)]
    place = {pid: i for i, pid in enumerate(near)}

    def _stamp(page_id: Optional[int]) -> str:
        """The signature sentence with its date and time, letters and digits only ("electronically
        signed by peter hale md on 2 17 2026 10 11 42"); "" without one or without a digit."""
        sig = sigs.get(page_id) if page_id is not None and page_id in signed else None
        found = " ".join(re.findall(r"[a-z0-9]+", ((sig or {}).get("sentence") or "").lower()))
        return found if re.search(r"\d", found) else ""

    def _spot(page_id: Optional[int]) -> Optional[tuple[str, frozenset[str], str]]:
        sig = sigs.get(page_id) if page_id is not None and page_id in signed else None
        if not sig:
            return None
        words = frozenset(w for w in re.findall(r"[a-z]+", (sig.get("sentence") or "").lower()) if len(w) > 1)
        return ((sig.get("region") or "").strip().casefold(), words,
                " ".join(re.findall(r"[a-z]+", (sig.get("provider_name") or "").lower())))

    def _repeated_signature(page_id: Optional[int]) -> Optional[int]:
        """The previous / next non-blank page whose signature sits in the same spot, or None."""
        mine = _spot(page_id)
        if mine is None or page_id not in place:
            return None
        i = place[page_id]
        stamp = _stamp(page_id)
        for other in near[max(0, i - 1): i] + near[i + 1: i + 2]:
            # 2026-11-23 (user): the same signature sentence with the same date and time on the
            # previous / next page is that page's signing repeated, whatever region each was filed in.
            if stamp and stamp == _stamp(other):
                return other
            theirs = _spot(other)
            if theirs is None or theirs[0] != mine[0]:
                continue
            same_sentence = bool(mine[1]) and len(mine[1] & theirs[1]) >= 0.8 * max(len(mine[1]), len(theirs[1]))
            if same_sentence or (mine[2] and mine[2] == theirs[2]):
                return other
        return None

    page_by_number = {page["id"]: page.get("page_number") for page in pages}
    words_of: dict[int, frozenset[str]] = {}

    def _words(page_id: Optional[int]) -> frozenset[str]:
        if page_id is None:
            return frozenset()
        if page_id not in words_of:
            words_of[page_id] = frozenset(unique_words(texts.get(page_id) or ""))
        return words_of[page_id]

    def _short_verdict(pid: int, logic: str = "v1") -> Optional[dict[str, Any]]:
        return short_verdict(pid, texts=texts, inputs=inputs or {}, around=around,
                             headers=headers_by_id or {}, signed=signed,
                             repeated=_repeated_signature(pid) is not None, words=_words,
                             engine_texts=engine_texts, logic=logic)

    logic_mode = blank_junk_logic()

    from modules.blank_junk import llm_judge

    llm_on = llm_judge.ready()

    def _llm_review(page_id: int, verdict: "method.Verdict", page_inputs: "method.PageInputs") -> "method.Verdict":
        """2026-11-23 (user, BLANK_JUNK_LLM): a short page with some content of its own after a full
        page with Progress Note headers -- the LLM judges it from the previous page and this page's
        positioned lines, over the short rules' soft verdicts and the classifier."""
        if not llm_on or verdict.rule not in llm_judge.REVIEWABLE:
            return verdict
        from modules.blank_junk import pn_headers

        prev_id = around.get(page_id, (None, None))[0]
        text = texts.get(page_id) or ""
        if prev_id is None or len(re.findall(r"[A-Za-z]{2,}", text)) < 2:
            return verdict
        prev_inputs = (inputs or {}).get(prev_id)
        prev_text = texts.get(prev_id) or ""
        if prev_inputs is not None and is_short(prev_inputs):
            return verdict
        if not pn_headers.clinical_sections_on(prev_text, (headers_by_id or {}).get(prev_id) or ()):
            return verdict
        answer = llm_judge.judge(chart_id, llm_judge.prompt(
            prev_text, getattr(prev_inputs, "lines", None), text, page_inputs.lines))
        if "error" in answer:
            verdict.votes["llm"] = {"error": answer["error"], "kept": verdict.rule}
            return verdict
        code = CODE_MAIN if answer["verdict"] == "main" else method._junk_code("Others")
        votes = {**verdict.votes, "llm": {**answer, "rules_rule": verdict.rule, "rules_reason": verdict.reason}}
        votes["rule"] = method.LLM_ID
        return method.Verdict(code, float(answer["confidence"]), method.LLM_ID,
                              f"{method.LLM_ID}: LLM {answer['verdict']}: {answer['reason']} (rules: {verdict.rule})",
                              votes, strength=float(answer["confidence"]))

    by_id: dict[int, dict[str, Any]] = {}
    for page in pages:
        page_id = page["id"]
        if page_id not in todo:
            continue
        text = texts.get(page_id) or ""
        page_inputs = (inputs or {}).get(page_id) or method.PageInputs(text=text, pass_no=1)
        page_inputs.text = text
        repeated_sig = _repeated_signature(page_id)
        # neighbour_signed = the signature repeats in the same spot nearby: no signature keep.
        page_inputs.neighbour_signed = repeated_sig is not None
        # 2026-11-04 (user): pass 1 is rules only; the model runs in pass 2.
        scores = (model_scores(text, docling=(docling_pages or {}).get(page_id))
                  if text.strip() and page_inputs.pass_no == 2 else {})
        page_inputs.bert = scores.get("bert")
        if page_inputs.pass_no == 2:
            # 2026-11-20 (user): a Progress Note page on each side (2+ note sections on each);
            # the fax header rule leaves such a page to the model.
            from modules.blank_junk import pn_headers

            prev_id, next_id = around.get(page_id, (None, None))
            page_inputs.flanked_by_notes = prev_id is not None and next_id is not None and all(
                len(pn_headers.clinical_sections_on(texts.get(n) or "", (headers_by_id or {}).get(n) or ())) >= 2
                for n in (prev_id, next_id))
        if page_inputs.pass_no == 2 and find_duplicates:
            page_inputs.short_verdict = _short_verdict(page_id)
            if (page_inputs.document_type in method.CARD_DOCUMENT_TYPES
                    and len(_words(page_id)) <= CARD_BARCODE_MAX_WORDS and (image_paths or {}).get(page_id)):
                page_inputs.barcode = read_barcode((image_paths or {})[page_id])
        # Blank / Junk v2 (2026-11-28, user): BLANK_JUNK_LOGIC v1 (default) | v2 | shadow -- in shadow
        # v1 decides and v2's verdict is recorded beside it (blank_junk.json "v2"; differences listed).
        deciding = "v2" if logic_mode == "v2" else "v1"
        if deciding == "v2" and page_inputs.pass_no == 2 and find_duplicates:
            page_inputs.short_verdict = _short_verdict(page_id, "v2")
        page_inputs.logic = deciding
        verdict = method.post_process(method.decide(page_inputs), page_inputs)
        if logic_mode == "shadow":
            from dataclasses import replace as _dc_replace

            other = _dc_replace(page_inputs, logic="v2",
                                short_verdict=_short_verdict(page_id, "v2") if (
                                    page_inputs.pass_no == 2 and find_duplicates) else page_inputs.short_verdict)
            try:
                shadow = method.decide(other)
                verdict.votes["v2"] = {
                    "verdict": CLASSIFICATION_LABELS.get(shadow.code, "Main"), "rule": shadow.rule,
                    "why": shadow.reason, "route": shadow.votes.get("route"), "tier": shadow.votes.get("tier"),
                    "same": shadow.code == verdict.code,
                }
            except Exception as exc:  # noqa: BLE001 - the shadow must never stop the stage
                verdict.votes["v2"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        if page_inputs.pass_no == 2 and page_inputs.short_verdict:
            verdict = _llm_review(page_id, verdict, page_inputs)
        if page_inputs.short_verdict:
            # 2026-11-23 (user): a short page's verdict reads at most short_page.confidence_cap (90%).
            cap = float(method.canon().raw["short_page"].get("confidence_cap", 0.9))
            if verdict.confidence is not None and verdict.confidence > cap:
                verdict.votes["short_page_confidence_cap"] = f"{verdict.confidence:.4f} -> {cap}"
                verdict.confidence = cap
        if repeated_sig is not None:
            verdict.votes["signature_repeated"] = (
                f"same signature spot as page {page_by_number.get(repeated_sig, repeated_sig)}")
        by_id[page_id] = _row_for(
            page, code=verdict.code, reason=verdict.reason, confidence=verdict.confidence,
            votes=verdict.votes, strength=verdict.strength,
        )

    def _main(page_id: int) -> bool:
        if page_id in by_id:
            return by_id[page_id]["code"] == CODE_MAIN
        return page_id in prior

    # Duplicates (2026-11-11, user: Duplicate_Logic.md; dup_rules.find): Main pages only.
    # Pass 1 marks exact copies within 3 pages; pass 2 applies every rule. The date-of-service
    # block needs the documents: continuity applies it.
    dup_of: dict[int, int] = {}
    # Main rows this check changed (Copy of / Conflict), written even for earlier-pass pages.
    partial: set[int] = set()
    best: dict[int, dict[str, Any]] = {}
    page_by_id = {p["id"]: p for p in pages}
    if find_duplicates or exact_near:
        cfg = dup_rules.settings(method.canon().raw.get("duplicate_logic"))

        def dup_text(page_id: int) -> str:
            """2026-11-28 (user): every page on one OCR engine -- Docling (it reads every page) in
            pass 2, else the page's text (pass 1: Tesseract for all)."""
            by = (engine_texts or {}).get(page_id) or {}
            return by.get("docling") or texts.get(page_id) or ""

        candidates = [
            dup_rules.Page(
                page_id=page["id"], position=i, text=dup_text(page["id"]),
                signed=page["id"] in signed,
                signature=_signature_key((signatures or {}).get(page["id"])),
                needs_image=page["id"] in (needs_image or set()),
                image=(image_paths or {}).get(page["id"]),
            )
            for i, page in enumerate(pages)
            if _main(page["id"]) and not _blank(page["id"])
        ]
        outcomes = dup_rules.find(
            candidates, all_texts=[dup_text(page["id"]) for page in pages], cfg=cfg,
            near_only=exact_near,
        )
        for page_id, out in outcomes.items():
            record = duplicate_record(out, page_by_id)
            if out.closest:
                other = page_by_id.get(out.closest["page_id"]) or {}
                best[page_id] = {**out.closest, "page_name": other.get("page_name"),
                                 "page_number": other.get("page_number"), "record": record}
            own = by_id.get(page_id) or {}
            earlier = own.get("votes")
            if out.is_duplicate and out.original is not None:
                dup_of[page_id] = out.original
                reason = (f"duplicate_of_page_id:{out.original} ({out.rule}, score={out.score:.4f}, "
                          f"{out.distance} pages apart)")
                by_id[page_id] = _row_for(
                    page_by_id[page_id], code=CODE_DUPLICATE, reason=reason, duplicate_of=out.original,
                    # 2026-11-14 (user): Yes = max(0.30, 1 - 10 x (1 - match)); the match is the weight.
                    confidence=conf.duplicate_match(out.score), strength=out.score,
                    votes={**earlier, "duplicate": reason, "duplicate_check": record} if earlier else None,
                )
                continue
            if out.label == "copy" and out.original is not None:
                note = f"{SAME_CONTENT_MARK}{out.original} (Copy of: 100% text match, blocked by {out.blocked_by})"
                original = out.original
            elif out.review:
                note = (f"{REVIEW_MARK}{out.review} (a partial match to page id {out.original}, "
                        f"as is page id {out.conflict_with})")
                original = None
            else:
                if earlier is not None:
                    own["votes"] = {**earlier, "duplicate_check": record}
                continue
            by_id[page_id] = _row_for(
                page_by_id[page_id],
                code=own.get("code", CODE_MAIN),
                reason=f"{own['reason']}; {note}" if own.get("reason") else note,
                duplicate_of=original,
                confidence=own.get("confidence", out.score),
                strength=own.get("strength", own.get("confidence", out.score)),
                votes={**earlier, "duplicate_check": record} if earlier else None,
            )
            partial.add(page_id)

    # A contents page's next page carrying on its list (2026-11-15, user): the page before is a
    # contents page (INDEX) and this one is 2+ entry lines (dates / page numbers), at most
    # 120 distinct words, with nothing that kept it earlier -> Junk / Others.
    contained: set[int] = set()
    if find_duplicates:
        for pid in order:
            own = by_id.get(pid) or {}
            if pid in dup_of or not (own.get("votes") or {}).get("reached_classifier"):
                continue
            prev_id = around.get(pid, (None, None))[0]
            prev_row = by_id.get(prev_id) if prev_id is not None else None
            # R4d contents page (2026-11-28 ID; "INDEX" on rows written before).
            if not prev_row or not str(prev_row.get("reason") or "").startswith(("R4d", "INDEX")):
                continue
            body = texts.get(pid) or ""
            if method.index_entries(body) >= 2 and len(_words(pid)) <= 120:
                note = (f"R4d contents page continued: page {page_by_id[prev_id].get('page_number')} is a contents page and "
                        f"this page carries its list on ({method.index_entries(body)} entry lines) -> Junk / Others")
                earlier = own.get("votes")
                p_keep, p_junk, p_blank = method._model_probs((inputs or {}).get(pid) or method.PageInputs(text=""))
                by_id[pid] = _row_for(page_by_id[pid], code=CODE_OTHERS, reason=note,
                                      confidence=method.verdict_confidence(CODE_OTHERS, p_keep, p_junk, p_blank),
                                      strength=CONTAINED_CONFIDENCE,
                                      votes={**earlier, "short_page": note} if earlier else None)
                partial.discard(pid)
                contained.add(pid)

    named = best
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
        if pid in dup_of or pid in partial or pid in contained:
            rows.append(by_id[pid])
    return rows


CARD_BARCODE_MAX_WORDS = 150  # a card's page is short; longer pages are never read for a barcode
SAME_ORDER_SHARE = 0.98  # S5: this share of a short page's words, in order, on a neighbour
FEW_NEW_WORDS = 5  # S6
NOTE_HEADER_EXTRA_WORDS = 3  # S2
MEASUREMENT = re.compile(
    # 2026-11-20 (user): lab units too -- a lab table carried onto a short page ("WBC 6.2
    # x10(3)/mcL 4.5-11.0x10(3)/mcL") is measurements.
    r"(?<![\w.])\d+(?:[.,]\d+)?\s*(?:mg/dl|mmol/l|ng/ml|pg/ml|iu/l|u/l|g/dl|meq/l|mm\s*hg|mmhg|bpm|%|kg|lbs?|°f|°c"
    r"|x\s*10\s*\(?\d+\)?\s*/\s*(?:mcl|ul|µl|l)|(?:cells\s*)?/\s*(?:mcl|ul|µl|hpf|lpf)|fl|pg|ml/min(?:/1\.73\s*m2)?|mosm/kg|mg/l|ug/l|mcg/l|ng/dl)(?![A-Za-z])"
    r"|(?<![A-Za-z])(?:glucose|a1c|hba1c|vitamin\s*[a-z0-9]{1,3}|bmi|blood\s+pressure|bp|pulse|temp(?:erature)?|weight|spo2|o2\s+sat"
    r"|wbc|rbc|hgb|hct|mcv|mch|mchc|rdw|platelets?|sodium|potassium|chloride|creatinine|bun|egfr|calcium)"
    r"\W{0,3}\d", re.IGNORECASE)


def is_short(inputs: "method.PageInputs") -> bool:
    """A short page (2026-11-19, user): text lines, none in the middle of the page
    (contained_page.top_fraction..bottom_fraction, 25%-75%), and no ink there when measured --
    its text sits at the top, at the bottom, or both. No line positions: never short."""
    lines = inputs.lines or []
    cfg = method.canon().raw.get("contained_page") or {}
    top, bottom = float(cfg.get("top_fraction", 0.25)), float(cfg.get("bottom_fraction", 0.75))
    if not lines:
        # Only where the content ends is known: short when it ends in the top band.
        if inputs.content_bottom is None:
            return False
        return inputs.content_bottom <= top and inputs.body_ink is not True
    if any(line.bottom > top and line.top < bottom for line in lines):
        return False
    return inputs.body_ink is not True


def _in_order_share(mine: list[str], theirs: list[str]) -> float:
    """The share of ``mine`` (a page's words) found in the same order in ``theirs``."""
    from difflib import SequenceMatcher

    if not mine or not theirs:
        return 0.0
    matcher = SequenceMatcher(None, mine, theirs, autojunk=False)
    return sum(block.size for block in matcher.get_matching_blocks()) / len(mine)


def blank_junk_logic() -> str:
    """BLANK_JUNK_LOGIC (2026-11-28, user): v1 (today, the default) | v2 | shadow (v1 decides, v2 recorded)."""
    import os

    value = (os.environ.get("BLANK_JUNK_LOGIC") or "v1").strip().lower()
    return value if value in ("v1", "v2", "shadow") else "v1"


# 2026-11-28 (user, Blank / Junk v2): the short checks' order. v1: today's first-fire order. v2
# (docs/specials/BLANK_JUNK_V2_LOGIC.md): tier 1 strong junk (S-A1 S4, S-A2 S5, S-A3 S6, S-A4 S1,
# S-A5 S2, S-A6 S3), tier 2 keeps (S-K1 S11, S-K2 S12, S-K3 S7, S-K4 S8, S-K5 S9), tier 3 weak junk
# (S-W1 S10, S-W2 S13, S-W3 S16 tiny page, S-W4 S14), then the model.
SHORT_V1_ORDER = ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12", "S13", "S14")
SHORT_V2_ORDER = ("S4", "S5", "S6", "S1", "S2", "S3", "S11", "S12", "S7", "S8", "S9", "S10", "S13", "S16", "S14")
SHORT_V2_TIER = {"S4": 1, "S5": 1, "S6": 1, "S1": 1, "S2": 1, "S3": 1, "S11": 2, "S12": 2, "S7": 2, "S8": 2,
                 "S9": 2, "S10": 3, "S13": 3, "S16": 3, "S14": 3}

# 2026-11-28 (user): a page is compared with its neighbour on one OCR engine, in this order.
SAME_ENGINE_ORDER = ("docling", "azuredocintel", "tesseract")


def short_verdict(
    pid: int,
    *,
    texts: dict[int, str],
    inputs: dict[int, "method.PageInputs"],
    around: dict[int, tuple[Optional[int], Optional[int]]],
    headers: dict[int, tuple[str, ...]],
    signed: set[int],
    repeated: bool,
    words: Any,
    engine_texts: Optional[dict[int, dict[str, str]]] = None,
    logic: str = "v1",
) -> Optional[dict[str, Any]]:
    """The short page's outcome (2026-11-19, user), or None when the page is not short:
    {"code": a CODE_* (or None: on to the classifier), "rule": "S1".., "why": "..."}.

    The page's own lines (2026-11-21, user): a line is the neighbour's when it is part of a run of
    2+ lines in the same order there, or a single line matching one there in the same band of the
    page (top / middle / bottom; repeated_lines); placeholder values ("None recorded.") are no
    content either.

      SC a document cover sheet: its own lines only label / value fields (Date, Type, Subject,
         Approved By, Comments ...), 3+, one naming the document -> Junk / Cover Page
      SH (2026-11-23, user) nothing but a running header / footer: every OCR line in the header or
         footer band, each a running line (page number, fax header, Patient / DOB / Visit labels,
         dates, times, numbers, at most 2 other words), no signature / provider / credentials ->
         Junk / Others
      S1 the previous page ends in a Progress Note header: in its bottom third, one of its last
         3 headers -> Main (this page carries that section on) -- only with 2+ words of its own
      S2 a Progress Note header and 3+ other words on the page -> Main
      SF a fax routing block (a fax number and a To / Attn / From line), no note content ->
         Junk / Letter/Fax, even with a provider on it (2026-11-19, user)
      S3 an Electronic Signature, or a provider, not repeated on the previous / next page -> Main
      S4 its content is measurements, doses or ICD codes -> Main
      S5 98% of its words, in order, on the previous or next page -> Junk / Others
      S6 (2026-11-20 / 21, user) each line is removed when it matches a line of the previous or
         next page (each pair on the same OCR engine: Doc Intel vs Doc Intel, else Docling vs
         Docling), and so are the page's own section headers; the unique words left, dates and
         numbers left out: 1 or none -> Junk / Others; only a title or header words -> Junk /
         Others; 2-4 -> Main; 5+ -> the classifier
      otherwise the classifier decides."""
    from modules.blank_junk import evidence, pn_headers

    own = inputs.get(pid)
    if own is None or not is_short(own):
        return None
    # A handwritten (> 30%) or visual page keeps its own keeps (HW, R3V): OCR words of handwriting
    # or a picture say little about what the page adds.
    hw_limit = float(method.canon().raw["handwritten_veto"]["min_handwritten_area_pct"])
    if (own.hw_area_pct is not None and own.hw_area_pct > hw_limit) or own.document_type == "visual":
        return None
    text = texts.get(pid) or ""
    prev_id, next_id = around.get(pid, (None, None))
    # 2026-11-23 (user): a "Financials" / "Financial Summary" page is Junk: the method's RF decides
    # it, not the short page's keeps.
    if method.financial_heading(text, own.headings):
        return None

    neighbours = [n for n in (prev_id, next_id) if n is not None]

    def pair(other_id: int) -> tuple[str, str]:
        """This page's and the neighbour's text from the same OCR engine: Docling vs Docling
        (2026-11-23, user: Docling first), else Doc Intel vs Doc Intel, else the best text of each."""
        mine_by, theirs_by = (engine_texts or {}).get(pid) or {}, (engine_texts or {}).get(other_id) or {}
        for engine in SAME_ENGINE_ORDER:
            if (mine_by.get(engine) or "").strip() and (theirs_by.get(engine) or "").strip():
                return mine_by[engine], theirs_by[engine]
        return text, texts.get(other_id) or ""

    def same_engine(other_id: int) -> bool:
        mine_by, theirs_by = (engine_texts or {}).get(pid) or {}, (engine_texts or {}).get(other_id) or {}
        return any((mine_by.get(e) or "").strip() and (theirs_by.get(e) or "").strip()
                   for e in SAME_ENGINE_ORDER)

    checks: list[dict[str, Any]] = []

    def step(name: str) -> None:
        """2026-11-28 (user): each short-page check in order, for blank_junk.json."""
        if checks and checks[-1]["result"] == "checking":
            checks[-1]["result"] = "no"
        checks.append({"check": name, "result": "checking"})

    def out(code: Optional[int], rule: str, why: str, margin: Optional[float] = None) -> dict[str, Any]:
        """The outcome, with its evidence (2026-11-21, user): ``margin`` how far past the rule's cut
        (evidence.py), and the support -- both neighbours compared or one, the same OCR engine
        on both sides, and for a Junk verdict the share of its lines that are the neighbours'
        (for Main, the share that are its own)."""
        support: dict[str, Optional[float]] = {
            "neighbours_compared": len(neighbours) / 2 if neighbours else None,
            "same_ocr_engine": (sum(1 for n in neighbours if same_engine(n)) / len(neighbours))
                               if neighbours and engine_texts else None,
        }
        if page_lines and code is not None:
            theirs_share = sum(theirs_flags) / len(page_lines)
            support["lines_theirs" if code != CODE_MAIN else "lines_own"] = (
                theirs_share if code != CODE_MAIN else 1.0 - theirs_share)
        rule = method.SHORT_IDS.get(rule, rule)  # 2026-11-28 (user): S1 ... S15
        if code is None:
            step("S15")
        if checks and checks[-1]["result"] == "checking":
            checks[-1]["result"] = f"fired: {why}" if code is not None else f"to the model: {why}"
        return {"code": code, "rule": rule, "why": why, "checks": checks,
                "evidence": {"margin": None if margin is None else round(float(margin), 4), "support": support}}
    # 2026-11-20 (user): the page's own stage 6 section headers ("EKG/Stress Test") are not
    # content: a line that is one of them (or only its words) is removed too.
    heading_texts = [str(h) for h in (own.headings or ())] + [str(h) for h in headers.get(pid) or ()]
    heading_keys = {_line_key(h) for h in heading_texts if _line_key(h)}
    heading_words = set().union(*(unique_words(h) for h in heading_texts)) if heading_texts else set()

    def a_heading(line: str) -> bool:
        key = _line_key(line)
        return key in heading_keys or (bool(heading_words) and set(unique_words(line)) <= heading_words)

    # The page's own lines (2026-11-21, user): not the previous / next page's (repeated_lines:
    # a running header / footer in the same band, or a block carried over), not a placeholder
    # value ("None recorded."). Measured on every text of the neighbours (each OCR engine).
    # 2026-11-23 (user): compared on the same OCR engine -- the page's Docling lines against the
    # neighbour's Docling text, else Doc Intel against Doc Intel (``pair``); only when the two have
    # no engine in common, the page's best text against every text of the neighbour.
    mine_by = (engine_texts or {}).get(pid) or {}
    # 2026-11-28 (user): one engine for the page and both neighbours -- the first in
    # SAME_ENGINE_ORDER they all have (Tesseract always), else the page's first.
    have = [e for e in SAME_ENGINE_ORDER if (mine_by.get(e) or "").strip()]
    shared = [e for e in have if all(((engine_texts or {}).get(n) or {}).get(e, "").strip() for n in neighbours)]
    base_engine = (shared or have or [None])[0]
    base_text = mine_by[base_engine] if base_engine else text
    page_lines = [line for line in base_text.splitlines() if _line_key(line)]
    theirs_flags = [False] * len(page_lines)
    for other_id in neighbours:
        theirs_by = (engine_texts or {}).get(other_id) or {}
        if base_engine and (theirs_by.get(base_engine) or "").strip():
            others = [theirs_by[base_engine]]
        else:
            others = [texts.get(other_id) or "", *theirs_by.values()]
        for other_text in others:
            flags = repeated_lines(base_text, other_text, own, inputs.get(other_id))
            theirs_flags = [a or b for a, b in zip(theirs_flags, flags)]
    own_lines = [line for line, theirs in zip(page_lines, theirs_flags)
                 if not theirs and not EMPTY_VALUE_LINE.match(line.strip())]
    repeated_keys = [_line_key(line) for line, theirs in zip(page_lines, theirs_flags) if theirs]
    own_words = set().union(*(_plain_words(line) for line in own_lines if not a_heading(line))) if own_lines else set()

    def added_over_previous() -> set[str]:
        """2026-11-23 (user): the words this page adds of its own -- its lines that are not the
        previous page's (repeated_lines, Docling vs Docling by ``pair``), not its section headings and
        not a running header / footer line; their words without dates and numbers."""
        if prev_id is None:
            return set()
        mine_text, prev_text = pair(prev_id)
        mine_lines = [line for line in mine_text.splitlines() if _line_key(line)]
        flags = repeated_lines(mine_text, prev_text, own, inputs.get(prev_id))
        kept = [line for line, theirs in zip(mine_lines, flags)
                if not theirs and not EMPTY_VALUE_LINE.match(line.strip()) and not a_heading(line)
                and not running_marker_line(line)]
        return set().union(*(_plain_words(line) for line in kept)) if kept else set()

    added = added_over_previous()
    unsigned = pid not in signed and not own.provider and not method.CREDENTIAL.search(text)
    own_text = "\n".join(own_lines)
    repeated_joined = _joined(repeated_keys)
    own_headings = tuple(h for h in own.headings or ()
                         if not _line_on(_line_key(str(h)), set(repeated_keys), repeated_joined))

    # 2026-11-28 (user, Blank / Junk v2): each check is its own function; v1 runs them in today's
    # order (first that fires decides), v2 in tiers -- strong junk, keeps, weak junk, then the model.
    def check_S1() -> Optional[dict[str, Any]]:
        # SC (2026-11-21, user): a document cover sheet -- "Document Details / Date: / Type: EKG /
        # Subject: EKG / Approved By: / Comments: No", nothing else of its own -> Junk / Cover Page.
        fields = cover_sheet_fields(own_lines)
        if fields:
            field_lines = sum(1 for line in own_lines if META_FIELD.match(line.strip().strip("#*|").strip()))
            margin = evidence.past_cut(len(fields), META_MIN_FIELDS - 1, META_MIN_FIELDS + 3) * field_lines / max(1, len(own_lines))
            return out(method.CODE_COVER_PAGE, "SC", f"document cover sheet ({', '.join(fields)}) -> Junk / Cover Page", margin)
        # SE (2026-11-21, user): "End of Report" is all the page has of its own (the running header and
        # footer aside) -> Junk / Cover Page, as pass 1's R1E on a page of few words.
        return None

    def check_S2() -> Optional[dict[str, Any]]:
        report_end = [line for line in own_lines if method.END_OF_REPORT.search(line)]
        if report_end and all(method.END_OF_REPORT.search(line) or a_heading(line) for line in own_lines):
            return out(method.CODE_COVER_PAGE, "SE", f"'{report_end[0].strip()}' is all of its own -> Junk / Cover Page", 1.0)

        # SN (2026-11-23, user): its only content of its own is a pointer to another page ("Content on
        # next page", "Continued on the following page", "See next page") -> Junk / Others.
        return None

    def check_S3() -> Optional[dict[str, Any]]:
        pointers = [line for line in own_lines if NEXT_PAGE_NOTE.search(line)]
        if pointers and all(NEXT_PAGE_NOTE.search(line) or a_heading(line) or running_marker_line(line) for line in own_lines):
            return out(CODE_OTHERS, "SN", f"'{pointers[0].strip()[:60]}' is all of its own -> Junk / Others", 1.0)
        # SH (2026-11-23, user): nothing but a running header and footer -- every text line at the
        # page's top or bottom edge, each a running line (a page number, a fax header, "Patient: /
        # DOB: / Visit:" labels, a date / time, a number) -- and no signature, provider or credentials:
        # Junk / Others, before the keeps that a date on it would otherwise trigger (S1S, S6).
        # SB (2026-11-28, user): a blank page with only its header / footer -- every OCR line in the
        # header or footer band, at most EDGE_BLANK_MAX_LINES at the top and at the bottom, each a
        # running line or repeated on the previous / next page -- is Blank, not Junk.
        return None

    def check_S4() -> Optional[dict[str, Any]]:
        blank_edge = edge_blank(own, page_lines, theirs_flags)
        if blank_edge and unsigned:
            return out(CODE_BLANK, "SB", f"only a header / footer ({blank_edge}) -> Blank", 1.0)
        return None

    def check_S5() -> Optional[dict[str, Any]]:
        edge = edge_only(own, text)
        if edge and unsigned:
            return out(CODE_OTHERS, "SH", f"only a running header / footer at the page edges ({edge}) -> Junk / Others", 1.0)
        # S1: the previous page's last headers, one of its last 3 in its bottom third -- for a page
        # with content of its own (2026-11-21, user: a page with only the running header and the
        # page number carries nothing on).
        # 2026-11-23 (user): carrying a section on needs 2+ words the previous page does not have.
        return None

    def check_S6() -> Optional[dict[str, Any]]:
        # SR (2026-11-28, user): every line of the page is the previous / next page's (its running
        # header and footer, however many lines) -- nothing of its own -> Junk / Others. A provider or
        # date in that header / footer does not keep it; its own Electronic Signature does.
        if page_lines and all(theirs_flags) and (pid not in signed or repeated):
            return out(CODE_OTHERS, "SR", f"all {len(page_lines)} lines are on the previous / next page "
                                          "(running header / footer only) -> Junk / Others", 1.0)
        return None

    def check_S7() -> Optional[dict[str, Any]]:
        if prev_id is not None and len(added) >= ADDED_WORDS_TO_CARRY:
            prev_text = texts.get(prev_id) or ""
            prev_lines = [line for line in prev_text.splitlines() if line.strip()]
            found = pn_headers.header_lines(prev_text, headers.get(prev_id) or ())
            positioned = [(i, line, section) for i, line, section in found if i >= 0][-3:]
            prev_inputs = inputs.get(prev_id)
            for i, line, section in positioned:
                low = _line_position(prev_inputs, line)
                if low is None and prev_lines:
                    low = (i + 1) / max(1, len((prev_text or "").splitlines()))
                if low is not None and low >= 2 / 3:
                    margin = (evidence.past_cut(low, 2 / 3, 1.0) + evidence.past_cut(len(own_words), 1, 10)) / 2
                    return out(CODE_MAIN, "S1", f"the previous page ends in its '{section}' section "
                                                f"({low:.0%} down the page): this page carries it on -> Main", margin)
        # S1S (2026-11-21, user): the previous page ends in a signature ("Signed By ...", "Signed",
        # "Electronically signed") and this page's own text is its signer and date ("Rajinder Grover
        # on 7/30/2025 at 1:12 PM"): the signature carries on -> Main.
        # 2026-11-23 (user): the date must be on a line of its own content, not in a running header /
        # footer ("Patient: ... DOB: ... Visit: 06/02/2025 Page: 3" is no signer carried over).
        return None

    def check_S8() -> Optional[dict[str, Any]]:
        if prev_id is not None and len(added) >= ADDED_WORDS_TO_CARRY and any(
                method.ANY_DATE.search(line) and not running_marker_line(line) for line in own_lines):
            prev_tail = [line for line in (texts.get(prev_id) or "").splitlines() if line.strip()][-SIGNATURE_TAIL_LINES:]
            signing = next((line for line in prev_tail if SIGNATURE_LINE.search(line)), None)
            if signing is not None:
                at = prev_tail.index(signing)
                return out(CODE_MAIN, "S1S", "the previous page ends in a signature and this page carries its signer / "
                                             "date on -> Main", evidence.past_cut(at, 0, len(prev_tail) - 1))
        # S2: its own Progress Note header and 3+ other words -- on its own lines only (2026-11-20,
        # user: a running "Labs (continued)" header repeated from the previous page is not the page's).
        return None

    def check_S9() -> Optional[dict[str, Any]]:
        sections = pn_headers.sections_on(own_text, own_headings)
        if sections:
            other = [w for w in re.findall(r"[a-z]+", own_text.lower())
                     if len(w) > 1 and w not in pn_headers.header_words(own_text, own_headings)]
            if len(other) >= NOTE_HEADER_EXTRA_WORDS:
                return out(CODE_MAIN, "S2", f"'{sections[0]}' header and {len(other)} other words -> Main",
                           evidence.past_cut(len(other), NOTE_HEADER_EXTRA_WORDS - 1, 12))
        # SF (2026-11-19, user): "To: Dr ... Fax: (555) ..." on a short page is fax routing, Junk even
        # with the provider it names; a page with note content (header, ICD codes, doses) is not.
        return None

    def check_S10() -> Optional[dict[str, Any]]:
        routing = method.fax_routing(text)
        if routing and not method.note_content(own):
            labels = len(method.FAX_ROUTING.findall(text)) + len(method.FAX_NUMBER.findall(text))
            return out(method.CODE_LETTER_FAX, "SF", f"fax routing ({routing}) -> Junk / Letter/Fax",
                       evidence.past_cut(labels, 1, 4))
        # S3: an Electronic Signature (not in the same spot nearby) or a provider not on the neighbours.
        return None

    def check_S11() -> Optional[dict[str, Any]]:
        if pid in signed and not repeated:
            # The signature was compared on both neighbours or one.
            return out(CODE_MAIN, "S3", "Electronic Signature, not repeated on the previous / next page -> Main",
                       len(neighbours) / 2 if neighbours else None)
        # The provider: 2026-11-19 (user), like the signature -- a provider printed in the same spot
        # (region and name) on the previous / next page is a running line, not this page's own.
        # Without a provider position, a provider the neighbours do not have at all.
        neighbour_inputs = [inputs.get(n) for n in (prev_id, next_id) if n is not None]
        if own.provider:
            if own.provider_spot is not None:
                repeated_provider = any(same_spot(own.provider_spot, getattr(n, "provider_spot", None)) for n in neighbour_inputs)
            else:
                repeated_provider = any(getattr(n, "provider", False) for n in neighbour_inputs)
            if not repeated_provider:
                # Compared by position (the box) or by name only.
                return out(CODE_MAIN, "S3", "provider name / credentials, not in the same spot on the previous / "
                                            "next page -> Main", 1.0 if own.provider_spot is not None else 0.5)
        # S4: measurements, doses or ICD codes -- on its own lines (2026-11-20, user).
        return None

    def check_S12() -> Optional[dict[str, Any]]:
        from dataclasses import replace as _replace

        found = (method.medication_doses(own_text)
                 or (" / ".join(method.lab_result_words(_replace(own, text=own_text)) or []) or None)
                 or ("ICD-10 codes" if method.ICD10_CODE.search(own_text) else None)
                 or ("measurements" if len(MEASUREMENT.findall(own_text)) >= 2 else None))
        if found:
            hits = (len(MEASUREMENT.findall(own_text)) + len(method.ICD10_CODE.findall(own_text))
                    + len(re.findall(r"\d\s*(?:mg|mcg|ml|units?)\b", own_text, re.IGNORECASE)))
            return out(CODE_MAIN, "S4", f"its content is {found} -> Main", evidence.past_cut(hits, 1, 6))
        return None

    def check_S13() -> Optional[dict[str, Any]]:
        # S5: the same words in the same order on a neighbour.
        for other_id in neighbours:
            mine_text, other_text = pair(other_id)
            share = _in_order_share(re.findall(r"[a-z0-9]+", mine_text.lower()),
                                    re.findall(r"[a-z0-9]+", other_text.lower()))
            if share >= SAME_ORDER_SHARE:
                return out(CODE_OTHERS, "S5", f"{share:.0%} of its words, in order, are on the page "
                                              f"{'before' if other_id == prev_id else 'after'} -> Junk / Others",
                           evidence.past_cut(share, SAME_ORDER_SHARE, 1.0))
        # S6 (2026-11-20, user): what it adds. Each line of the page is removed only when the whole
        # line matches a line of the previous or next page exactly (each pair on one OCR engine); the
        # words of the lines left are counted.
        return None

    def check_S14() -> Optional[dict[str, Any]]:
        left: Optional[set[str]] = None
        titles_only: list[bool] = []
        removed_headings = False
        for other_id in neighbours:
            mine_text, other_text = pair(other_id)
            mine_lines = [line for line in mine_text.splitlines() if _line_key(line)]
            flags = repeated_lines(mine_text, other_text, own, inputs.get(other_id))
            kept = [line for line, theirs in zip(mine_lines, flags)
                    if not theirs and not EMPTY_VALUE_LINE.match(line.strip())]
            if any(a_heading(line) for line in kept):
                removed_headings = True
                kept = [line for line in kept if not a_heading(line)]
            found = set().union(*(_plain_words(line) for line in kept)) if kept else set()
            left = found if left is None else left & found
            titles_only.append(bool(kept) and all(_title_like(line) for line in kept))
        if left is None:
            lines = [line for line in text.splitlines() if _line_key(line)]
            left = set().union(*(_plain_words(line) for line in lines)) if lines else set()
        if len(left) >= FEW_NEW_WORDS:
            return out(None, "S7", f"{len(left)} words on lines of its own, no short-page rule: the classifier decides")
        header_vocab = pn_headers.header_words(text, own.headings) | set(
            w for h in headers.get(pid) or () for w in re.findall(r"[a-z0-9]+", str(h).lower()))
        # 2026-11-21 (user): dates and numbers are left out of the words counted (above); more than
        # one unique word of its own keeps the page, one or none is junk.
        # The margin: how far the word count is from the cut between 1 (Junk) and 2 (Main); a Junk by
        # header / title words, how far from the classifier's 5.
        junk_margin = evidence.past_cut(len(left), 1.5, 0)
        if not left and removed_headings:
            return out(CODE_OTHERS, "S6", "only section header(s) of its own -> Junk / Others", junk_margin)
        if len(left) <= 1:
            return out(CODE_OTHERS, "S6", f"{len(left)} word(s) of its own (dates and numbers left out) -> Junk / Others",
                       junk_margin)
        if left <= header_vocab:
            return out(CODE_OTHERS, "S6", f"{len(left)} word(s) on lines of its own, all header words -> Junk / Others",
                       evidence.past_cut(len(left), FEW_NEW_WORDS, 0))
        if titles_only and all(titles_only):
            return out(CODE_OTHERS, "S6", f"only a title of its own ({', '.join(sorted(left)[:5])}) -> Junk / Others",
                       evidence.past_cut(len(left), FEW_NEW_WORDS, 0))
        if logic == "v2":
            # v2 S-M1: 2-4 words of its own no longer default to Main -- the model decides.
            return out(None, "S7", f"{len(left)} word(s) on lines of its own: the model decides")
        return out(CODE_MAIN, "S6", f"{len(left)} word(s) on lines of its own ({', '.join(sorted(left)[:8])}) -> Main",
                   evidence.past_cut(len(left), 1.5, FEW_NEW_WORDS))
        return None

    def check_S16() -> Optional[dict[str, Any]]:
        # v2 S-W3 (was pass 1's R2c): a tiny page -- under MIN_TEXT_CHARS characters of text, no
        # provider and no date -> Junk / Others.
        chars = len(re.sub(r"\s", "", text))
        if chars < method.MIN_TEXT_CHARS and not own.provider and not method.ANY_DATE.search(text):
            return out(CODE_OTHERS, "S16", f"tiny page: {chars} characters (< {method.MIN_TEXT_CHARS}) -> Junk / Others",
                       evidence.past_cut(chars, method.MIN_TEXT_CHARS, 0))
        return None

    if logic == "v2":
        sequence = SHORT_V2_ORDER
    else:
        sequence = SHORT_V1_ORDER
    table = {name: fn for name, fn in locals().items() if name.startswith("check_S")}
    for name in sequence:
        step(name)
        result = table[f"check_{name}"]()
        if result is not None:
            return result
    return out(None, "S15", "no short-page rule fired: the model decides")


# A page number in a line ("Page 13", "Page 2 of 4", "pg. 3", "13 of 20"): it changes on every
# page, so it is left out when lines are compared (2026-11-20, user: a footer "Printed on ...
# Page 13" is page 12's "... Page 12").
PAGE_NUMBER_IN_LINE = re.compile(r"\b(?:page|pg)\.?\s*[:#]?\s*\d+(?:\s*(?:of|/)\s*\d+)?\b|\b\d+\s+of\s+\d+\b", re.IGNORECASE)


# SN (2026-11-23, user): a line pointing to another page, with at most a few words around it.
NEXT_PAGE_NOTE = re.compile(
    r"(?i)^\W*(?:\(?\s*)?(?:(?:the\s+)?(?:content|contents|text|report|note|results?|document|form|information)s?\s+)?"
    r"(?:is\s+|are\s+)?(?:continued|continues|continue|cont'?d?\.?|see|please\s+see|refer\s+to|turn\s+to|"
    r"(?:is\s+)?on|(?:printed\s+)?on)\s+(?:on\s+|to\s+)?(?:the\s+)?(?:next|following|reverse|other|back)\s+"
    r"(?:page|side|sheet)s?\W*$"
    r"|^\W*(?:please\s+)?turn\s+over\W*$|^\W*p\.?t\.?o\.?\W*$"
    r"|^\W*\(?\s*continued\s*\)?\W*$"
    r"|^\W*(?:content|contents|text)\s+(?:on|in)\s+(?:the\s+)?(?:next|following)\s+page\W*$")


# SH (2026-11-23): a running header / footer line -- page numbers, fax headers, patient labels.
RUNNING_LABEL = re.compile(
    r"(?i)\b(?:patient(?:\s+name)?|pt|name|dob|d\.o\.b|birth\s*date|mrn|med\s*rec|acct|account|fin|visit|"
    r"encounter|enc|from|to|fax|printed|print\s+date|received|sent|page|pg)\b\s*[:#.]?")
TIME_OF_DAY = re.compile(r"(?i)\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b")
RUNNING_FREE_WORDS = 2  # words a running line may carry besides its labels (a name, a time zone)
STAMP_FREE_WORDS = 6  # a fax / print stamp (page number + date or time) also carries its sender


def running_line(line: str) -> bool:
    """A line of a running header / footer: its labels, dates, times and numbers out, leave at
    most RUNNING_FREE_WORDS (a patient's name, "PDT"); a fax / print stamp -- a page number with a
    date or time -- at most STAMP_FREE_WORDS (the sender's name)."""
    stamp = bool(PAGE_NUMBER_IN_LINE.search(line or "")) and bool(
        method.ANY_DATE.search(line or "") or TIME_OF_DAY.search(line or ""))
    text = PAGE_NUMBER_IN_LINE.sub(" ", line or "")
    text = method.ANY_DATE.sub(" ", text)
    text = TIME_OF_DAY.sub(" ", text)
    text = RUNNING_LABEL.sub(" ", text)
    words = [w for w in re.findall(r"[A-Za-z]+", text) if len(w) > 1]
    return len(words) <= (STAMP_FREE_WORDS if stamp else RUNNING_FREE_WORDS)


def running_marker_line(line: str) -> bool:
    """A running header / footer line by its content alone (no position): a running line that
    carries a page number or a running label (Patient / DOB / Visit / Printed / Fax ...). A short
    content line ("Hypertension", "Dr Grover 7/30/2025") is not one."""
    return running_line(line) and bool(PAGE_NUMBER_IN_LINE.search(line or "") or RUNNING_LABEL.search(line or ""))


EDGE_BLANK_MAX_LINES = 2  # SB: lines at the top, and at the bottom, of a header / footer-only blank page


def edge_blank(page: Optional["method.PageInputs"], lines: list[str], theirs: list[bool]) -> Optional[str]:
    """SB (2026-11-28, user): every OCR line of the page sits in its header band (top) or footer
    band (bottom), at most EDGE_BLANK_MAX_LINES in each, and every text line is a running line with
    a page number or running label (running_marker_line) or one the previous / next page repeats
    (``theirs``). The measured line positions decide;
    without them, no verdict. Returns a short description."""
    if page is None or not page.lines or not lines:
        return None
    zones = method.canon().raw["zones"]
    header, footer = float(zones["header_band"]), float(zones["footer_band"])
    if not all(line.top < header or line.bottom > 1.0 - footer for line in page.lines):
        return None
    top = sum(1 for line in page.lines if line.top < header)
    bottom = len(page.lines) - top
    if top > EDGE_BLANK_MAX_LINES or bottom > EDGE_BLANK_MAX_LINES:
        return None
    # A running line needs its page number or running label (Patient / DOB / Page / Printed / Fax):
    # "DME Orders / None recorded." is a section with nothing in it, not a header.
    if not all(flag or running_marker_line(line) for line, flag in zip(lines, theirs)):
        return None
    return f"{top} line(s) at the top, {bottom} at the bottom"


def edge_only(page: Optional["method.PageInputs"], text: str) -> Optional[str]:
    """SH: every OCR line of the page sits in its header band (top) or footer band (bottom) and
    every text line is a running line, with at least one page number / label among them. The
    measured line positions decide; without them, no verdict. Returns a short description."""
    if page is None or not page.lines:
        return None
    zones = method.canon().raw["zones"]
    header, footer = float(zones["header_band"]), float(zones["footer_band"])
    if not all(line.top < header or line.bottom > 1.0 - footer for line in page.lines):
        return None
    lines = [line for line in (text or "").splitlines() if _line_key(line)]
    if not lines or not all(running_line(line) for line in lines):
        return None
    if not any(PAGE_NUMBER_IN_LINE.search(line) or RUNNING_LABEL.search(line) for line in lines):
        return None
    top = sum(1 for line in page.lines if line.top < header)
    return f"{top} line(s) at the top, {len(page.lines) - top} at the bottom"


def _plain_words(line: str) -> set[str]:
    """A line's unique words without its dates and numbers (2026-11-21, user)."""
    text = method.ANY_DATE.sub(" ", line or "")
    return {w for w in unique_words(text) if not any(ch.isdigit() for ch in w)}


def _title_like(line: str) -> bool:
    """A section title rather than content (2026-11-20, user: "EKG/Stress Test" alone under the
    running header is junk): 1-5 words, no digits, every word capitalised, no sentence end."""
    text = re.sub(r"[#*|]", " ", line or "").strip()
    words = re.findall(r"[A-Za-z0-9']+", text)
    if not 1 <= len(words) <= 5 or re.search(r"\d", text) or text.endswith((".", "?", "!")):
        return False
    return all(word[0].isupper() for word in words)


NUMBER_WORD = re.compile(r"\d+(?:[.,:]\d+)*")


def _joined(keys: Any) -> str:
    """The neighbour's lines (their _line_keys) run together, letters and digits only."""
    return "|" + "|".join(re.sub(r"[^a-z0-9]", "", key) for key in keys) + "|"


def _line_on(key: str, theirs: set[str], joined: Optional[str] = None) -> bool:
    """Is the line (its _line_key) on the neighbour: the same line; or (2026-11-20, user) the
    same words but one, where both are numbers ("11:43" / "11:44", "0443" / "0442"; not a
    date such as 01/28/26); or, for a line of 3+ words, its text in the neighbour's text with
    only the line breaks different (OCR joined or split the same header lines: "Admit Date:
    n/a Disch Date: n/a Physician: ..." on one line here, four lines there)."""
    if key in theirs:
        return True
    if joined is not None and len(key.split()) >= 3:
        compact = re.sub(r"[^a-z0-9]", "", key)
        if len(compact) >= 8 and compact in joined.replace("|", ""):
            return True
    words = key.split()
    for other in theirs:
        other_words = other.split()
        if len(other_words) != len(words):
            continue
        diff = [(a, b) for a, b in zip(words, other_words) if a != b]
        if len(diff) == 1 and _number_pair(*diff[0]):
            return True
    return False


def _line_key(line: str) -> str:
    """A line as compared between pages: lower case, spacing and markup (#, *, |) evened out,
    page numbers left out."""
    text = PAGE_NUMBER_IN_LINE.sub(" page# ", line or "")
    return " ".join(re.sub(r"[#*|]", " ", text).lower().split())


# 2026-11-21 (user): a document cover sheet -- an EHR's "Document Details" page: label / value
# fields only (Date, Type, Subject, Approved By, Comments ...), at least 3, one of them naming
# the document (Type / Document Type / Subject / Category), each value a few words.
META_LABEL = (r"(?:document\s+type|document\s+date|type|subject|category|description|approved\s+by|reviewed\s+by"
              r"|author|created\s+by|status|comments?|received(?:\s+date)?|scanned\s+(?:by|date)|uploaded\s+by"
              r"|filed\s+by|date)")
META_FIELD = re.compile(rf"(?:^|(?<=\s)){META_LABEL}\s*:", re.IGNORECASE)
META_NAMING = {"type", "document type", "subject", "category"}
META_MIN_FIELDS = 3
META_MAX_VALUE_WORDS = 6


def cover_sheet_fields(lines: list[str]) -> Optional[list[str]]:
    """The labels of a document cover sheet, or None: every line a label / value field (a title
    line aside), 3+ fields, one naming the document, no value over 6 words."""
    labels: list[str] = []
    for line in lines or ():
        body = line.strip().strip("#*|").strip()
        found = list(META_FIELD.finditer(body))
        if not found:
            if _title_like(body):
                continue
            return None
        if found[0].start() != 0:
            return None
        for k, match in enumerate(found):
            end = found[k + 1].start() if k + 1 < len(found) else len(body)
            if len(re.findall(r"[A-Za-z0-9]+", body[match.end():end])) > META_MAX_VALUE_WORDS:
                return None
            labels.append(" ".join(match.group(0).rstrip(": ").lower().split()))
    if len(labels) >= META_MIN_FIELDS and META_NAMING & set(labels):
        return labels
    return None


# S1S: a signature in the previous page's last lines (its footer included).
SIGNATURE_LINE = re.compile(r"(?i)\b(?:electronically\s+signed|e-?signed|signed\s+by|signed|signature)\b")
SIGNATURE_TAIL_LINES = 4
# S1 / S1S (2026-11-23, user): a page carries the previous page's section or signature on only
# when it adds this many words the previous page does not have (dates and numbers left out).
ADDED_WORDS_TO_CARRY = 2

# 2026-11-21 (user): placeholder values ("None recorded.", "No information", "N/A") are no
# content of the page's own.
EMPTY_VALUE_LINE = re.compile(
    r"^\W*(?:none(?:\s+(?:recorded|documented|reported|noted|on\s+file))?|no\s+(?:information|data|records?)"
    r"(?:\s+(?:available|found|recorded))?|not\s+(?:recorded|documented|available|applicable|on\s+file)"
    r"|n\s*/\s*a|unknown)\W*$", re.IGNORECASE)


def _band(position: Optional[float]) -> Optional[str]:
    """top / middle / bottom of the page (contained_page 25% / 75%), None when unknown."""
    if position is None:
        return None
    cfg = method.canon().raw.get("contained_page") or {}
    top, bottom = float(cfg.get("top_fraction", 0.25)), float(cfg.get("bottom_fraction", 0.75))
    return "top" if position < top else "bottom" if position >= bottom else "middle"


def _line_top(page_inputs: Optional["method.PageInputs"], line: str) -> Optional[float]:
    """Where ``line`` sits on its page (its top, a share of the height): the OCR line with the
    same words, else the OCR line that contains it; None when not found."""
    want = " ".join(re.findall(r"[a-z0-9]+", (line or "").lower()))
    if not want or page_inputs is None or not page_inputs.lines:
        return None
    texts = [(" ".join(re.findall(r"[a-z0-9]+", (o.text or "").lower())), float(o.top)) for o in page_inputs.lines]
    for got, top in texts:
        if got == want:
            return top
    for got, top in texts:
        if got and want in got:
            return top
    return None


def _same_line_keys(key: str, their_keys: list[str]) -> list[int]:
    """The neighbour's lines that are this line: the same key, or the same words but one number."""
    words = key.split()
    found = []
    for j, other in enumerate(their_keys):
        if other == key:
            found.append(j)
            continue
        other_words = other.split()
        if len(other_words) != len(words):
            continue
        diff = [(a, b) for a, b in zip(words, other_words) if a != b]
        if len(diff) == 1 and _number_pair(*diff[0]):
            found.append(j)
    return found


def _number_pair(a: str, b: str) -> bool:
    """Two words that are the same number field: both numbers ("11:43" / "11:44"), or a page
    count with the same total ("30/43" / "31/43"; not a reading such as 120/80 vs 130/85)."""
    if NUMBER_WORD.fullmatch(a) and NUMBER_WORD.fullmatch(b):
        return True
    ma, mb = PAGE_COUNT.fullmatch(a), PAGE_COUNT.fullmatch(b)
    return bool(ma and mb and ma.group(2) == mb.group(2))


PAGE_COUNT = re.compile(r"(\d{1,4})/(\d{1,4})")


def _compact(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key or "")


def _edge_walk(keys: list[str], their_keys: list[str], out: list[bool]) -> None:
    """Mark the page's lines that repeat the neighbour's from one edge, in the same order: the
    page's i-th line from the edge against the neighbour's j-th (a match: the same line, one
    number different, or the same text with the line breaks different -- one line here, several
    there, or the other way round). The first line that does not match ends the run."""
    i = j = 0
    while i < len(keys) and j < len(their_keys):
        key, theirs = keys[i], their_keys[j]
        if _line_on(key, {theirs}):
            out[i] = True
            i, j = i + 1, j + 1
            continue
        mine_c, theirs_c = _compact(key), _compact(theirs)
        step = None
        # One line here, several there.
        for span in range(2, 7):
            if j + span <= len(their_keys) and mine_c and mine_c == "".join(_compact(k) for k in their_keys[j:j + span]):
                step = (1, span)
                break
        # Several lines here, one there.
        if step is None:
            for span in range(2, 7):
                if i + span <= len(keys) and theirs_c and theirs_c == "".join(_compact(k) for k in keys[i:i + span]):
                    step = (span, 1)
                    break
        if step is None:
            return
        for at in range(i, i + step[0]):
            out[at] = True
        i, j = i + step[0], j + step[1]


def repeated_lines(mine_text: str, other_text: str,
                   mine_inputs: Optional["method.PageInputs"] = None,
                   other_inputs: Optional["method.PageInputs"] = None) -> list[bool]:
    """For each non-empty line of ``mine_text``: is it the neighbour's line (2026-11-21, user)?
    A running header / footer or a block carried over, not the page's own content:
      - a run of 2+ lines that follow each other in the same order on the neighbour, anywhere; or
      - the page's lines from its top edge that repeat the neighbour's from its top, in the same
        order, and likewise from the bottom edge (_edge_walk): the running header and footer.
        "Chronic kidney disease" right under the header there and after "Diagnosis" here is not
        the neighbour's line; nor is a diagnosis that sits in the middle of the neighbour's
        problem list."""
    del mine_inputs, other_inputs  # the order from the edges, not the measured position, decides
    mine = [line for line in (mine_text or "").splitlines() if _line_key(line)]
    their_lines = [line for line in (other_text or "").splitlines() if _line_key(line)]
    keys = [_line_key(line) for line in mine]
    their_keys = [_line_key(line) for line in their_lines]
    pairs = set(zip(their_keys, their_keys[1:]))
    out = [False] * len(mine)
    for i in range(len(mine) - 1):
        if (keys[i], keys[i + 1]) in pairs:
            out[i] = out[i + 1] = True
    _edge_walk(keys, their_keys, out)
    bottom = [False] * len(mine)
    _edge_walk(keys[::-1], their_keys[::-1], bottom)
    return [a or b for a, b in zip(out, bottom[::-1])]


def _line_position(page_inputs: Optional["method.PageInputs"], line: str) -> Optional[float]:
    """Where ``line`` sits on its page (its top, a share of the height), from the OCR lines."""
    want = " ".join(re.findall(r"[a-z0-9]+", (line or "").lower()))
    if not want or page_inputs is None or not page_inputs.lines:
        return None
    for ocr_line in page_inputs.lines:
        got = " ".join(re.findall(r"[a-z0-9]+", (ocr_line.text or "").lower()))
        if got and (want in got or got in want):
            return float(ocr_line.top)
    return None


def read_barcode(path: Path) -> Optional[str]:
    """The first barcode on the page image (zxing-cpp: PDF417 on licences, 1-D and QR codes on
    cards), as its format name; None when there is none, the image cannot be read or the
    library is not installed (2026-11-19, user)."""
    try:
        import zxingcpp
        from PIL import Image

        with Image.open(path) as image:
            found = zxingcpp.read_barcodes(image.convert("L"))
    except Exception as exc:  # noqa: BLE001 - no barcode reader is no barcode, the rules go on
        logger.debug("barcode not read for %s (%s)", path.name, type(exc).__name__)
        return None
    for barcode in found or ():
        return str(getattr(barcode.format, "name", barcode.format))
    return None


def signature_details(chart_name: str) -> dict[str, dict[str, str]]:
    """Page name -> kv_extract's Electronic Signature on it (provider_signature.csv, signed rows):
    region (header / mid / footer), the signature sentence, provider name / credentials, date."""
    import csv

    from common.db.paths import imaging_csv

    path = imaging_csv(chart_name, "provider_signature")
    if not path.is_file():
        return {}
    keep = ("region", "sentence", "provider_name", "provider_credentials", "signature_date")
    with path.open(newline="", encoding="utf-8") as handle:
        return {str(row.get("page_name") or "").strip(): {k: str(row.get(k) or "") for k in keep}
                for row in csv.DictReader(handle)
                if str(row.get("signature_present") or "").strip().casefold() == "y"}


def new_lines(text: str, before: frozenset[str]) -> str:
    """The page's lines with a word not on the page before (``before``: its unique_words);
    a line repeated from it -- a running header or footer -- is left out."""
    return "\n".join(line for line in (text or "").splitlines()
                     if unique_words(line) - before)


def signature_ends_page(signature: dict[str, str], text: str, share: float = 0.6) -> bool:
    """kv_extract's signature is at the end of the page: in the footer band, or most of its
    signature sentence's words are in the page's last paragraph."""
    if "footer" in (signature.get("region") or "").casefold():
        return True
    sentence = set(re.findall(r"[a-z0-9]+", (signature.get("sentence") or "").lower()))
    if not sentence:
        return False
    tail = set(re.findall(r"[a-z0-9]+", last_paragraph(text).lower()))
    return len(sentence & tail) >= share * len(sentence)


def last_paragraph(text: str, max_lines: int = 6) -> str:
    """The page's last paragraph: the lines after its last blank line, at most ``max_lines``."""
    blocks = [b for b in re.split(r"\n\s*\n", (text or "").strip()) if b.strip()]
    lines = (blocks[-1] if blocks else "").splitlines()
    return "\n".join(lines[-max_lines:])


def _rewrite_csv(conn: Any, chart_id: int, chart_name: str) -> Path:
    """Rebuild the junk CSV from the stored verdicts.

    Always a full rewrite, so re-running either pass converges instead of
    appending duplicate rows.
    """
    from common.db import list_blank_junk_for_csv

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
) -> tuple[Optional[list["method.Line"]], Optional[float]]:
    """The page's text lines with their height on the page (final2 word boxes, else final1),
    the share of the page inside the box round the text (method.text_area)."""
    from modules.extraction.ocr_input import page_for_extraction

    found = page_for_extraction(
        _ocr_json(final2), _ocr_json(final1),
        page_name=page["page_name"], page_number=page.get("page_number"),
        image_path=page_image_path(chart_name, page["page_name"]),
    )
    if not found or not found.get("height"):
        return None, None
    return (method.lines_from_words(found["words"], float(found["height"])) or None,
            method.text_area(found["words"], found.get("width"), found.get("height")))


def edge_text_lines(chart_name: str, page_name: str) -> Optional[dict[str, Any]]:
    """EB (pass 1, 2026-11-28, user): the text lines in the header band and the footer band of the
    page image (rows of dark ink, runs of rows split by a blank gap), and whether there is ink
    between the bands (_body_ink). None when the image cannot be read."""
    try:
        import numpy as np
        from PIL import Image, ImageOps

        zones = method.canon().raw["zones"]
        header, footer = float(zones["header_band"]), float(zones["footer_band"])
        with Image.open(page_image_path(chart_name, page_name)) as image:
            gray = np.asarray(ImageOps.autocontrast(image.convert("L")))
        height, width = gray.shape
        rows = (gray < 128).sum(axis=1) > max(2, int(width * 0.002))

        def count(start: int, end: int) -> int:
            gap, min_gap, min_height = 0, max(2, int(height * 0.004)), max(2, int(height * 0.002))
            runs, run = 0, 0
            for flag in rows[start:end]:
                if flag:
                    run, gap = run + 1, 0
                    continue
                gap += 1
                if run and gap >= min_gap:
                    runs += int(run >= min_height)
                    run = 0
            return runs + int(run >= min_height)

        top = count(0, int(header * height))
        bottom = count(int((1.0 - footer) * height), height)
    except Exception as exc:  # noqa: BLE001 - an unreadable image only switches the rule off
        logger.warning("edge line check failed for %s/%s: %s", chart_name, page_name, exc)
        return None
    body_ink, fraction = _body_ink(chart_name, page_name)
    return {"top": top, "bottom": bottom, "body_ink": body_ink, "body_ink_fraction": fraction}


def _body_ink(chart_name: str, page_name: str, start: Optional[float] = None,
              end: Optional[float] = None) -> tuple[Optional[bool], Optional[float]]:
    """(ink below the short-page band, the share of that band that is ink): a picture,
    handwriting or stamp OCR did not read. ``start``: where the band begins (the content's
    bottom, at least the header band). (None, None) when the image cannot be read."""
    try:
        from PIL import Image

        from modules.image_preprocess.hw_printed import _image_bgr, page_marks

        short = method.canon().raw["short_page"]
        zones = method.canon().raw["zones"]
        top, bottom = max(float(zones["header_band"]), float(start or 0.0)), 1.0 - float(zones["footer_band"])
        if end is not None:
            bottom = min(bottom, float(end))
        with Image.open(page_image_path(chart_name, page_name)) as image:
            width, height = image.size
            band = image.convert("RGB").crop((0, int(top * height), width, int(bottom * height)))
        marks = page_marks(_image_bgr(band))
        fraction = float(marks["strong_fraction"])
        return fraction >= float(short["body_ink_max_fraction"]), round(fraction, 6)
    except Exception as exc:  # noqa: BLE001 - a missing image only switches the rule off
        logger.warning("short-page ink check failed for %s/%s: %s", chart_name, page_name, exc)
        return None, None


def extracted_page_names(chart_name: str, *fields: str, accepted: bool = False) -> set[str]:
    """Page names kv_extract selected any of ``fields`` on (staging/extraction.json); with
    ``accepted``, any row of them kv_extract accepted (or selected)."""
    from modules.extraction import staging

    staged = staging.read(chart_name)
    if staged is None:
        return set()
    if accepted:
        return {page.name for page in staged
                if any(row.get("accepted") or row.get("selected") for field in fields for row in page.rows(field)
                       if isinstance(row, dict))}
    return {page.name for page in staged if any(page.selected(field) for field in fields)}


# Two provider boxes are "the same spot" when their centres are this close, as a share of the
# page's width and height (2026-11-20, user: "almost the same box").
SAME_BOX_SHARE = 0.05


def _box_centre(row: dict[str, Any], width: Any, height: Any) -> Optional[tuple[float, float]]:
    """A row's box ([left, top, right, bottom], pixels) as its centre, a share of the page."""
    box = row.get("box")
    try:
        left, top, right, bottom = (float(v) for v in box)
        w, h = float(width), float(height)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return ((left + right) / 2 / w, (top + bottom) / 2 / h)


def provider_spots(chart_name: str) -> dict[str, tuple[str, frozenset[str], Optional[tuple[float, float]]]]:
    """Page name -> (region, name words, box centre) of kv_extract's selected provider on it:
    where the provider is printed, so the short page can tell a running provider line (same spot
    on the previous / next page) from the page's own (2026-11-19, user). The provider name row,
    else the selected credentials row naming its provider, which carries a box ("Roberto Velasco
    MD"; 2026-11-20, user)."""
    from modules.extraction import staging

    staged = staging.read(chart_name)
    if staged is None:
        return {}
    out: dict[str, tuple[str, frozenset[str], Optional[tuple[float, float]]]] = {}
    for page in staged:
        rows = [(row, str(row.get("value") or "")) for row in page.selected("provider_name")]
        rows += [(row, str(row.get("provider_name") or "")) for row in page.selected("provider_credentials")
                 if str(row.get("provider_name") or "").strip()]
        if rows:
            row, name = rows[0]
            words = frozenset(w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 1)
            out[page.name] = (str(row.get("region") or "").strip().casefold(), words,
                              _box_centre(row, page.width, page.height))
    return out


def same_spot(a: Optional[tuple], b: Optional[tuple]) -> bool:
    """Two provider spots are one running line: (nearly) the same name, at almost the same box
    (centres within SAME_BOX_SHARE of the page) when both have one, else in the same region."""
    if not a or not b:
        return False
    box_a = a[2] if len(a) > 2 else None
    box_b = b[2] if len(b) > 2 else None
    if box_a is not None and box_b is not None:
        if abs(box_a[0] - box_b[0]) > SAME_BOX_SHARE or abs(box_a[1] - box_b[1]) > SAME_BOX_SHARE:
            return False
    elif a[0] != b[0]:
        return False
    if not a[1] or not b[1]:
        return not a[1] and not b[1]
    return len(a[1] & b[1]) >= 0.8 * max(len(a[1]), len(b[1]))


def extracted_dos(chart_name: str) -> dict[str, frozenset[str]]:
    """Page name -> the dates of kv_extract's selected DOS on it (from and to, ISO form)."""
    from modules.extraction import staging
    from modules.extraction.util.dates import canonical_date

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
    with_provider: set[str] = set()
    with_name: set[str] = set()
    spots: dict[str, tuple[str, frozenset[str]]] = {}
    signature_known = False
    if pass_no == 2:
        from common.db.paths import imaging_csv
        from modules.continuity.signature import signed_page_names

        signed = signed_page_names(ctx.chart_name)
        signature_known = imaging_csv(ctx.chart_name, "provider_signature").is_file()
        # 2026-11-20 (user): any accepted provider name / credentials row counts.
        with_provider = extracted_page_names(ctx.chart_name, "provider_name", "provider_credentials", accepted=True)
        spots = provider_spots(ctx.chart_name)
        with_name = extracted_page_names(ctx.chart_name, "name")
    headings = section_heading_texts(ctx.chart_name) if pass_no == 2 else {}
    header_canon = section_heading_canon(ctx.chart_name) if pass_no == 2 else {}
    top = float(method.canon().raw["zones"]["header_band"])
    # 2026-11-19 (user): the short page reaches 25% of the page (canon contained_page.top_fraction).
    short_top = float((method.canon().raw.get("contained_page") or {}).get("top_fraction", 0.25))
    short_end = float((method.canon().raw.get("contained_page") or {}).get("bottom_fraction", 0.75))
    from modules.ocr.routing import judged_in_pass2, page_kind

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
            provider=page["page_name"] in with_provider,
            member_name_found=page["page_name"] in with_name,
            no_ink=str(q.get("hw_method") or "") in NO_INK_METHODS,
            # 2026-11-19 (user): pass 1's rules run again in pass 2 only on a handwritten (or
            # mixed) page or a bad-quality (Low) one, where Tesseract could not read it.
            pass1_skipped=pass_no == 2 and judged_in_pass2(q) and (
                page_kind(q) in {"handwritten", "mixed"} or str(q.get("quality_tag") or "").strip().lower() == "low"),
        )
        if pass_no == 1 and prelim is not None:
            # EB (2026-11-28, user): only a page of a few Tesseract lines is measured.
            lines = [line for line in unwrap_ocr_content(prelim.get(pid)).splitlines() if line.strip()]
            if len(lines) <= method.EDGE_BLANK_MAX_TEXT_LINES:
                inputs.edge_lines = edge_text_lines(ctx.chart_name, page["page_name"])
        if pass_no == 2:
            # R1d in pass 2 too (2026-11-28, user): a page of a few final-OCR lines is measured.
            final_text = unwrap_ocr_content((final2 or {}).get(pid)) or unwrap_ocr_content((final1 or {}).get(pid))
            if 0 < len([line for line in final_text.splitlines() if line.strip()]) <= method.EDGE_BLANK_MAX_TEXT_LINES:
                inputs.edge_lines = edge_text_lines(ctx.chart_name, page["page_name"])
            inputs.provider_spot = spots.get(page["page_name"])
            inputs.headings = headings.get(page["page_name"], ())
            inputs.header_canon = header_canon.get(page["page_name"], ())
            inputs.lines, inputs.text_area = _page_lines(
                (final2 or {}).get(pid), (final1 or {}).get(pid), page=page, chart_name=ctx.chart_name
            )
            bottom = method.content_bottom(inputs.lines) if inputs.lines else None
            inputs.content_bottom = bottom
            if bottom is not None and bottom <= max(top, short_top):
                inputs.body_ink, inputs.body_ink_fraction = _body_ink(ctx.chart_name, page["page_name"], start=bottom)
            elif inputs.lines and not any(line.bottom > short_top and line.top < short_end for line in inputs.lines):
                # 2026-11-28 (user): text only at the top and bottom (a header AND a footer): the
                # middle band (short_top..short_end) is measured too, so a page of drawings / a
                # body map / a photo between them is not short (it was junked as header-only).
                inputs.body_ink, inputs.body_ink_fraction = _body_ink(
                    ctx.chart_name, page["page_name"], start=short_top, end=short_end)
            inputs.ocr_match_rate = _match_rate(q)
            # Docling markdown, for the invoice rule's dollar table (2026-11-14, user).
            inputs.final1_text = unwrap_ocr_content((final1 or {}).get(pid))
            # Tesseract's text for the junk terms the final OCR missed; only where prelim is
            # trusted (printed, not low quality).
            if not page_blocks_prelim(q):
                inputs.prelim_text = unwrap_ocr_content((prelim or {}).get(pid))
        out[pid] = inputs
    return out


def _match_rate(quality_row: dict[str, Any]) -> Optional[float]:
    """The page's OCR match rate from its routing record (quality_detail.routing), None when
    the page was not routed or the rate not stored."""
    from modules.ocr.routing import final_match_rate

    detail = quality_row.get("quality_detail") or {}
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except ValueError:
            return None
    try:
        rate = final_match_rate((detail or {}).get("routing"))
        return None if rate is None else max(0.0, min(1.0, float(rate)))
    except (TypeError, ValueError, AttributeError):
        return None


def _headers_by_id(ctx: Any) -> dict[int, tuple[str, ...]]:
    """Each page's section header texts by page id, in page order, for the short page
    (2026-11-15). The last one also carries stage 6's canon name ("Dx: / Visit Diagnosis"), so
    the run-on list matches either."""
    texts = section_heading_texts(ctx.chart_name)
    canon_names = section_heading_canon(ctx.chart_name)
    out: dict[int, tuple[str, ...]] = {}
    for page in ctx.pages:
        name = page["page_name"]
        found = texts.get(name) or canon_names.get(name) or ()
        if not found:
            continue
        last_canon = (canon_names.get(name) or ("",))[-1]
        last = found[-1] if not last_canon or last_canon == found[-1] else f"{found[-1]} / {last_canon}"
        out[page["id"]] = tuple(found[:-1]) + (last,)
    return out


def section_heading_canon(chart_name: str) -> dict[str, tuple[str, ...]]:
    """Each page's section headers as stage 6's canon names (matched_canonical, else the text),
    Final OCR 2's when the page has them, else Final OCR 1's (the card rule's header check)."""
    from modules.continuity.layout import load_pages, section_headers

    out: dict[str, tuple[str, ...]] = {}
    for engine in ("final1", "final2"):
        for name, page in load_pages(chart_name, engine).items():
            names = tuple(h.get("matched_canonical") or h["text"] for h in section_headers(page)
                          if h.get("matched_canonical") or h.get("text"))
            if names:
                out[name] = names
    return out


def section_heading_texts(chart_name: str) -> dict[str, tuple[str, ...]]:
    """Each page's section header texts (stage 6, in the Final OCR JSON): Final OCR 2's when the
    page has them, else Final OCR 1's (2026-11-03, user: the fax rule reads these)."""
    from modules.continuity.layout import load_pages, section_headers

    out: dict[str, tuple[str, ...]] = {}
    for engine in ("final1", "final2"):  # final2 last: it wins where it has headers
        for name, page in load_pages(chart_name, engine).items():
            texts = tuple(h["text"] for h in section_headers(page) if h.get("text"))
            if texts:
                out[name] = texts
    return out


def similarity_matrix_path(chart_name: str, pass_no: int) -> Path:
    return staging_dir(chart_name) / f"duplicate_similarity_pass{pass_no}.csv"


def similarity_matrix(pages: list[dict[str, Any]], texts: dict[int, str]) -> list[dict[str, Any]]:
    """Page x page containment on the duplicate check's body text (header / footer lines and
    print stamps out, dup_rules.body_text): row page R, column page C = the share of R's distinct
    words that are on C, so a page contained in another is 100% one way and not the other
    (2026-11-15, user). Each row also carries the page's word count and distinct words."""
    cfg = dup_rules.settings(method.canon().raw.get("duplicate_logic"))
    all_texts = [texts.get(page["id"]) or "" for page in pages]
    repeated = dup_rules.repeated_band_lines(all_texts, cfg)
    bodies = [dup_rules.body(dup_rules.body_text(text, repeated, cfg)) for text in all_texts]
    labels = [str(page.get("page_number") or page.get("page_name")) for page in pages]
    rows: list[dict[str, Any]] = []
    for i, page in enumerate(pages):
        row: dict[str, Any] = {"page": labels[i], "page_name": page.get("page_name"),
                               "words": bodies[i].count, "distinct_words": len(bodies[i].words)}
        for j, label in enumerate(labels):
            mine = bodies[i].words
            row[f"p{label}"] = (round(100.0 * len(mine & bodies[j].words) / len(mine), 1) if mine else "")
        rows.append(row)
    return rows


def _write_similarity_matrix(chart_name: str, pass_no: int, pages: list[dict[str, Any]],
                             texts: dict[int, str]) -> Path:
    """staging/duplicate_similarity_pass<N>.csv (rebuilt every run): see similarity_matrix.
    Numbers only, no page text."""
    import csv

    rows = similarity_matrix(pages, texts)
    path = similarity_matrix_path(chart_name, pass_no)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["page"])
        writer.writeheader()
        writer.writerows(rows)
    return path


VOTES_FILE = "blank_junk.json"
VOTES_VERSION = 3


def votes_path(chart_name: str) -> Path:
    return staging_dir(chart_name) / VOTES_FILE


# The old second file (every ballot); since 2026-11-28 everything is in blank_junk.json.
DETAIL_FILE = "blank_junk_detail.json"

SKIP_REASONS = {
    "handwritten": "skipped: handwritten / mixed / form / visual page -- judged in pass 2 on the final OCR",
    "low_quality": "skipped: low-quality page -- judged in pass 2 on the final OCR",
    "blank_junk_pass1": "skipped: settled in pass 1",
    "final_keep_pass1": "skipped: a final keep in pass 1",
    "no_final_ocr_text": "skipped: no final OCR text yet",
}


def _duplicate_view(row: dict[str, Any]) -> dict[str, Any]:
    """The page's duplicate record (Duplicate_Logic.md section 8) and its closest page."""
    match = row.get("best_match") or {}
    record = (row.get("votes") or {}).get("duplicate_check") or match.get("record") or {}
    out: dict[str, Any] = {
        "label": record.get("label") or "",
        "duplicate": bool(record.get("duplicate")),
        "copy_of": record.get("copy_of"),
        "rule": record.get("rule") or "",
        "score": record.get("score"),
        "blocked_by": record.get("blocked_by") or "",
        "review": record.get("review") or "",
    }
    if match:
        out["closest_page"] = match.get("page_name")
        for key in ("similarity", "overlap", "contained_share", "contained"):
            if match.get(key) is not None:
                out[key] = match.get(key)
    return out


def _checks_view(trace: list[dict[str, Any]], order: tuple[str, ...]) -> list[dict[str, str]]:
    """Every check of the pass in order: its result where it ran, "not reached" after the one that
    fired, "not run" for one its branch skipped (2026-11-28, user)."""
    ran = {c["check"]: c["result"] for c in trace or ()}
    fired_at = next((i for i, name in enumerate(order) if str(ran.get(name, "")).startswith(("fired", "to the"))), None)
    out = []
    for i, name in enumerate(order):
        if name in ran:
            result = "no" if ran[name] == "checking" else ran[name]
        elif fired_at is not None and i > fired_at:
            result = "not reached"
        else:
            result = "not run (not for this page / pass / route)"
        out.append({"check": method.check_label(name), "result": result})
    return out


def _pass_entry(row: dict[str, Any], pass_no: int) -> dict[str, Any]:
    """One judged pass of one page: its checks, the short page's checks, the model, the verdict."""
    votes = row.get("votes") or {}
    entry: dict[str, Any] = {
        "verdict": row["label"],
        "rule": votes.get("rule") or ("duplicate" if row["flag"] == "duplicate" else None),
        "why": row["reason"],
        "confidence": round(float(row["confidence"]), 4),
        "decision_confidence": round(float(row.get("strength", row["confidence"])), 4),
        "checks": _checks_view(votes.get("checks") or [], method.PASS1_CHECKS if pass_no == 1 else method.PASS2_CHECKS),
    }
    if pass_no == 2:
        # 2026-11-28 (user): the route the page took after the common checks (short / visual / general),
        # and "others" when it went on to the model.
        entry["route"] = votes.get("route") or ("common" if not votes.get("reached_classifier") else "general")
        if votes.get("reached_classifier"):
            entry["route"] = f"{entry['route']} -> others"
        entry["model"] = votes.get("bert")
        service = votes.get("service")
        if isinstance(service, dict):
            entry["model_service"] = {k: service.get(k) for k in (
                "flag", "subclass", "confidence", "decision_reason", "review_required")}
        if votes.get("short_page_checks"):
            entry["short_page"] = {"result": votes.get("short_page"),
                                   "checks": _checks_view(votes["short_page_checks"], method.SHORT_CHECKS)}
        else:
            entry["short_page"] = "not a short page (text in the middle of the page, or a visual / handwritten page)"
    if votes.get("v2"):
        entry["v2"] = votes["v2"]  # BLANK_JUNK_LOGIC=shadow: v2's verdict beside v1's
    if votes.get("logic"):
        entry["logic"] = votes["logic"]
        entry["tier"] = votes.get("tier")
    extra = {k: votes[k] for k in (
        "card", "card_near_miss", "index_page", "fax_cover", "signature_log", "edge_lines", "keep_veto",
        "flipped_model", "not_blank_provider", "not_blank_date", "stage1_blank_overruled", "llm",
        "r4_not_alone", "r4_overruled", "protocol_keep_overruled", "fax_unsigned", "invoice_kept",
        "invoice_not_kept", "signature_repeated", "short_page_confidence_cap") if votes.get(k)}
    if extra:
        entry["notes"] = extra
    return entry


def _write_votes(
    chart_name: str,
    pass_no: int,
    rows: list[dict[str, Any]],
    best_matches: Optional[dict[str, dict[str, Any]]] = None,
    skipped: Optional[dict[str, str]] = None,
) -> Optional[Path]:
    """``staging/blank_junk.json`` (2026-11-28, user: one file for debugging). Per page:

    * ``stats`` -- ink (no ink, ink in the body, pass 1's edge lines), text (characters, words,
      lines, text area %, where the text ends, first and last line, headings), handwritten %,
      page type, visual, signed, provider;
    * ``pass_1`` / ``pass_2`` -- every check in order (no / fired / not reached / not run), the
      short page's checks, the model's probabilities, the verdict, rule, why and confidence;
      or "skipped: <why>";
    * ``duplicate`` -- the duplicate record and the closest page (similarity, overlap);
    * ``final`` -- decision, rule, why, pass, decision_confidence.

    This pass's entries are replaced and the other pass's kept; blank_junk_detail.json is removed.
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
    passes_run = sorted({*existing.get("passes_run", []), pass_no})
    key = f"pass_{pass_no}"
    for page in pages.values():
        page.pop(key, None)
    for row in rows:
        page = pages.setdefault(row["page_name"], {})
        page["page_number"] = row.get("page_number")
        page[key] = _pass_entry(row, pass_no)
        stats = (row.get("votes") or {}).get("stats")
        if stats and (pass_no == 2 or "stats" not in page):
            page["stats"] = stats
        if row.get("best_match") or (row.get("votes") or {}).get("duplicate_check"):
            page["duplicate"] = _duplicate_view(row)
    for page_name, match in (best_matches or {}).items():
        page = pages.setdefault(page_name, {})
        if "duplicate" not in page or pass_no == 2:
            page["duplicate"] = _duplicate_view({"best_match": match})
    for page_name, why in (skipped or {}).items():
        pages.setdefault(page_name, {})[key] = SKIP_REASONS.get(why, f"skipped: {why}")
    for page in pages.values():
        judged = [(n, page[f"pass_{n}"]) for n in (2, 1) if isinstance(page.get(f"pass_{n}"), dict)]
        if judged:
            n, entry = judged[0]
            page["final"] = {"decision": entry["verdict"], "rule": entry["rule"], "why": entry["why"], "pass": n,
                             "confidence": entry["confidence"],
                             # The weight continuity gives a junk run (the pre-2026-11-14 confidence).
                             "decision_confidence": entry["decision_confidence"]}
    order = ("page_number", "stats", "pass_1", "pass_2", "duplicate", "final")
    pages = {name: {k: page[k] for k in order if k in page} for name, page in sorted(
        pages.items(), key=lambda item: (item[1].get("page_number") is None, item[1].get("page_number") or 0, item[0]))}
    body = {
        "version": VOTES_VERSION,
        "chart_name": chart_name,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passes_run": passes_run,
        # BLANK_JUNK_LOGIC=shadow: every page where v2's verdict differs from v1's, both rules.
        "v2_differences": [
            {"page": name, "pass": key, "v1": entry.get("verdict"), "v1_rule": entry.get("rule"),
             "v2": entry["v2"].get("verdict"), "v2_rule": entry["v2"].get("rule"), "v2_route": entry["v2"].get("route")}
            for name, page in pages.items() for key in ("pass_1", "pass_2")
            if isinstance((entry := page.get(key)), dict) and isinstance(entry.get("v2"), dict)
            and entry["v2"].get("same") is False
        ],
        "check_order": {
            "pass_1": [method.check_label(c) for c in method.PASS1_CHECKS],
            "pass_2_common": [method.check_label(c) for c in method.PASS2_COMMON],
            "pass_2_short": [method.check_label(c) for c in method.SHORT_CHECKS],
            "pass_2_visual": [method.check_label(c) for c in method.PASS2_ROUTES["visual"]],
            "pass_2_general": [method.check_label(c) for c in method.PASS2_ROUTES["general"]],
            "pass_2_others": [method.check_label(c) for c in ("O1", "O2", "O3", "O4")],
        },
        "pages": pages,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=1, default=str), encoding="utf-8")
        tmp.replace(path)
        path.with_name(DETAIL_FILE).unlink(missing_ok=True)
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
        skipped_why: dict[str, str] = {}
        names_by_id = {page["id"]: page["page_name"] for page in ctx.pages}

        def mark_skipped(conn: Any, ctx: Any, page_ids: Any, reason: str) -> None:
            """_support.mark_skipped, remembering why for blank_junk.json (2026-11-28, user)."""
            for pid in page_ids:
                skipped_why[names_by_id.get(pid, str(pid))] = reason
            _mark_skipped(conn, ctx, page_ids, reason)

        with connect() as conn:
            todo_before = set(ctx.todo)
            non_printed = non_printed_page_ids(conn, chart_id)
            low_ids = low_quality_page_ids(conn, chart_id)
            quality = get_quality_map(conn, chart_id)
            # Final OCR 1 page objects, so the model reads them as b_jnk v3 does.
            docling_pages: dict[int, dict[str, Any]] = {}
            engine_texts: dict[int, dict[str, str]] = {}

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
                    # 2026-11-20 (user): each engine's own text, so the short page compares a
                    # page with its neighbour engine for engine (Doc Intel vs Doc Intel, Docling
                    # vs Docling).
                    engine_texts[pid] = {
                        engine: text for engine, text in (
                            ("azuredocintel", unwrap_ocr_content(final2.get(pid))),
                            ("docling", unwrap_ocr_content(final1.get(pid))),
                            # 2026-11-28 (user): every page has Tesseract -- the same-engine
                            # fallback when two pages share no final OCR engine.
                            ("tesseract", unwrap_ocr_content(prelim_texts.get(pid))),
                        ) if text.strip()
                    }
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
                # DistilBERT was sure in pass 1 that the page is a keep (R5 keep, final):
                # pass 2 does not judge it again.
                reasons = get_blank_junk_reasons(conn, chart_id, pass_no=1)
                kept = [pid for pid in ctx.todo if method.is_final_keep(reasons.get(pid))]
                # 2026-11-02 (user): a final keep with Fax in its header and no signature is
                # judged again, so the fax rule (method.fax_unsigned) can junk it.
                from modules.continuity.signature import signed_page_names

                signed_now = signed_page_names(ctx.chart_name)
                names = {page["id"]: page["page_name"] for page in ctx.pages}
                headings_now = section_heading_texts(ctx.chart_name)
                kept = [
                    pid for pid in kept
                    if not method.fax_unsigned(method.PageInputs(
                        text=texts.get(pid) or "", pass_no=2, signed=names.get(pid) in signed_now,
                        prelim_text=prelim_texts.get(pid) or "" if isinstance(prelim_texts, dict) else "",
                        headings=headings_now.get(names.get(pid) or "", ())))
                ]
                mark_skipped(conn, ctx, kept, "final_keep_pass1")
                # A page typed Blank with no text is judged (R1: Blank, 2026-10-27, user);
                # any other page without final OCR text waits for it.
                no_text = [
                    pid for pid in ctx.todo
                    if not (texts.get(pid) or "").strip() and page_type(quality.get(pid)) != "blank"
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
            inputs = _page_inputs(ctx, pass_no, quality, prelim=texts)
        best_matches: dict[str, dict[str, Any]] = {}
        signed_ids: set[int] = set()
        signatures: dict[int, dict[str, str]] = {}
        needs_image = set(non_printed)  # form / handwritten pages: their images must match (block 6)
        image_paths = {page["id"]: page_image_path(ctx.chart_name, page["page_name"]) for page in ctx.pages}
        if pass_no == 2:
            from modules.continuity.signature import signed_page_names

            signed_names = signed_page_names(ctx.chart_name)
            signed_ids = {page["id"] for page in ctx.pages if page["page_name"] in signed_names}
            details = signature_details(ctx.chart_name)
            signatures = {page["id"]: details[page["page_name"]] for page in ctx.pages if page["page_name"] in details}
        classified = _classify(
            ctx.pages, texts, ctx.todo, prior_main_ids=prior_mains,
            inputs=inputs, docling_pages=docling_pages, best_matches=best_matches,
            signed_ids=signed_ids, signatures=signatures,
            needs_image=needs_image, image_paths=image_paths,
            find_duplicates=pass_no == 2,
            exact_near=pass_no == 1,
            headers_by_id=_headers_by_id(ctx) if pass_no == 2 else None,
            engine_texts=engine_texts,
            chart_id=chart_id,
        )

        with connect() as conn:
            for row in classified:
                row_source = ocr_source
                if pass_no == 2:
                    row_source = sources.get(row["page_id"], ocr_source)
                try:
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
                except Exception as exc:  # one bad page must not sink the chart (2026-11-04, user)
                    page_name = str(row.get("page_name") or row["page_id"])
                    logger.exception("blank/junk pass %s: page %s not written", pass_no, page_name)
                    if row["page_id"] in ctx.todo:
                        mark_failed(conn, ctx, row["page_id"], f"blank/junk write failed: {exc}"[:500], page_name)
                    continue
                # Demoted prior-main pages are upserted but were not in todo.
                if row["page_id"] in ctx.todo:
                    mark_completed(conn, ctx, row["page_id"])
            mark_blank_junk_final(conn, chart_id)
            path = _rewrite_csv(conn, chart_id, ctx.chart_name)
        votes_file = _write_votes(ctx.chart_name, pass_no, classified, best_matches, skipped_why)
        try:
            _write_similarity_matrix(ctx.chart_name, pass_no, ctx.pages, texts)
        except Exception as exc:  # noqa: BLE001 - a debugging file must not fail the stage
            logger.warning("blank/junk similarity matrix not written: %s", str(exc)[:200])

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

