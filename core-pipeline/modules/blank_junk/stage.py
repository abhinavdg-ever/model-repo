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
    mark_skipped,
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
# The words that keep a short page when they are all it adds to its neighbours (2026-11-08, user).
KEEP_IF_ONLY_ADDED = frozenset({"visit", "diagnosis"})
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
) -> list[dict[str, Any]]:
    """Classify ``todo`` pages, then look for duplicates among Main pages (dup_rules.find).

    ``inputs`` carries each page's method.PageInputs without the model scores, which
    are added here. ``prior_main_ids`` are pages already ``not_blank_junk`` from an earlier
    pass: they are compared too, and may become a duplicate or a Copy of an earlier page.

    ``signed_ids`` / ``signatures``: kv_extract's Electronic Signature per page (blocks 4 and 5);
    ``needs_image``: form and handwritten pages, ``image_paths``: the page images (block 6).
    ``best_matches`` (page name -> its closest page and duplicate record, see _write_votes) is
    filled for every page compared.

    ``exact_near`` (pass 1): exact copies within 3 pages only. ``find_duplicates`` (pass 2):
    every rule.
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

    def _neighbour_signed(page_id: int) -> bool:
        return any(n is not None and n in signed for n in around.get(page_id, (None, None)))

    by_id: dict[int, dict[str, Any]] = {}
    for page in pages:
        page_id = page["id"]
        if page_id not in todo:
            continue
        text = texts.get(page_id) or ""
        page_inputs = (inputs or {}).get(page_id) or method.PageInputs(text=text, pass_no=1)
        page_inputs.text = text
        page_inputs.neighbour_signed = _neighbour_signed(page_id)
        # 2026-11-04 (user): pass 1 is rules only; the model runs in pass 2.
        scores = (model_scores(text, docling=(docling_pages or {}).get(page_id))
                  if text.strip() and page_inputs.pass_no == 2 else {})
        page_inputs.bert = scores.get("bert")
        verdict = method.post_process(method.decide(page_inputs), page_inputs)
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
        candidates = [
            dup_rules.Page(
                page_id=page["id"], position=i, text=texts.get(page["id"]) or "",
                signed=page["id"] in signed,
                signature=_signature_key((signatures or {}).get(page["id"])),
                needs_image=page["id"] in (needs_image or set()),
                image=(image_paths or {}).get(page["id"]),
            )
            for i, page in enumerate(pages)
            if _main(page["id"]) and not _blank(page["id"])
        ]
        outcomes = dup_rules.find(
            candidates, all_texts=[texts.get(page["id"]) or "" for page in pages], cfg=cfg,
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

    # The short page (2026-11-08, user; replaces the 2026-11-05 "contained in a page within 3"):
    # a page whose content ends in the top 20% of the page (no line positions: never short).
    # Candidates: every page that reached the classifier, whatever it made of it; a page an
    # earlier rule settled (blank, visual, handwritten, signed, card, fax, contents) is not.
    # Invoice pages stay Main. "Before" / "after" = the previous / next non-blank page.
    # In order, the first that applies decides:
    #   1. the page before ends in its Electronic Signature (kv_extract: footer band, or the
    #      signature sentence in its last paragraph) and this page carries it on: the provider's
    #      name / credentials (kv_extract), the signer's name, credentials or a date -> Main;
    #   2. fewer than 5 distinct words not on the page before or after -> Junk / Others (unless
    #      the only ones left are "Diagnosis" / "Visit Diagnosis": a diagnosis page, kept).
    contained: set[int] = set()
    if find_duplicates:
        cfg = method.canon().raw.get("contained_page", {})
        top = float(cfg.get("top_fraction", 0.20))
        junk_below = int(cfg.get("junk_below_unique_words", 5))
        words_of: dict[int, frozenset[str]] = {}

        def _words(page_id: Optional[int]) -> frozenset[str]:
            if page_id is None:
                return frozenset()
            if page_id not in words_of:
                words_of[page_id] = frozenset(unique_words(texts.get(page_id) or ""))
            return words_of[page_id]

        def _keep(pid: int, rule: str, why: str) -> None:
            own = by_id.get(pid) or {}
            note = f"{SHORT_MARK}{rule}: {why} -> Main"
            if own.get("code") == CODE_MAIN:
                own["votes"] = {**(own.get("votes") or {}), "short_page": note}
                return
            earlier = own.get("votes")
            by_id[pid] = _row_for(page_by_id[pid], code=CODE_MAIN, reason=note, confidence=conf.backup(),
                                  strength=CONTAINED_CONFIDENCE,
                                  votes={**earlier, "short_page": note} if earlier else None)

        def _junk(pid: int, code: int, note: str) -> None:
            own = by_id.get(pid) or {}
            earlier = own.get("votes")
            by_id[pid] = _row_for(page_by_id[pid], code=code, reason=note, confidence=conf.backup(),
                                  strength=CONTAINED_CONFIDENCE,
                                  votes={**earlier, "short_page": note} if earlier else None)
            partial.discard(pid)
            contained.add(pid)

        for pid in order:
            own = by_id.get(pid) or {}
            votes = own.get("votes") or {}
            if pid in dup_of or not (texts.get(pid) or "").strip():
                continue
            if not votes.get("reached_classifier"):
                continue
            bottom = getattr((inputs or {}).get(pid), "content_bottom", None)
            if bottom is None or bottom > top or method.is_invoice_page(own.get("reason")):
                continue
            prev_id, next_id = around.get(pid, (None, None))
            short = f"content ends at {bottom:.0%} <= {top:.0%} of the page"
            # 1. the page before ends in its Electronic Signature (kv_extract: in the footer band,
            # or its signature sentence in that page's last paragraph), carried on here.
            prev_sig = (signatures or {}).get(prev_id) if prev_id is not None and prev_id in signed else None
            if prev_sig and signature_ends_page(prev_sig, texts.get(prev_id) or ""):
                carried = method.provider_or_date(
                    texts.get(pid) or "", bool(getattr((inputs or {}).get(pid), "provider", False)),
                    signer=prev_sig.get("provider_name") or "")
                if carried:
                    _keep(pid, "signature_continued",
                          f"{short}; page {page_by_id[prev_id].get('page_number')} ends in its Electronic "
                          f"Signature ({prev_sig.get('region') or 'last paragraph'}), carried on here ({carried})")
                    continue
            # 2. what it adds to the pages before and after.
            left = _words(pid) - _words(prev_id) - _words(next_id)
            if len(left) >= junk_below:
                continue
            if left and left <= KEEP_IF_ONLY_ADDED and "diagnosis" in left:
                _keep(pid, "diagnosis", f"{short}; the only words not on the pages before / after are "
                                         f"{', '.join(sorted(left))}")
                continue
            _junk(pid, CODE_OTHERS, f"{SHORT_MARK}repeated: {short}; {len(left)} distinct word(s) not on the "
                                    f"page before or after (< {junk_below}) -> Junk / Others")

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
    and the share of the page the text covers (method.text_area)."""
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


def _body_ink(chart_name: str, page_name: str) -> tuple[Optional[bool], Optional[float]]:
    """(ink below the short-page band, the share of that band that is ink): a picture,
    handwriting or stamp OCR did not read. (None, None) when the image cannot be read."""
    try:
        from PIL import Image

        from modules.image_preprocess.hw_printed import _image_bgr, page_marks

        short = method.canon().raw["short_page"]
        zones = method.canon().raw["zones"]
        top, bottom = float(zones["header_band"]), 1.0 - float(zones["footer_band"])
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
    from modules.extraction import staging

    staged = staging.read(chart_name)
    if staged is None:
        return set()
    return {page.name for page in staged if any(page.selected(field) for field in fields)}


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
    signature_known = False
    if pass_no == 2:
        from common.db.paths import imaging_csv
        from modules.continuity.signature import signed_page_names

        signed = signed_page_names(ctx.chart_name)
        signature_known = imaging_csv(ctx.chart_name, "provider_signature").is_file()
        with_provider = extracted_page_names(ctx.chart_name, "provider_name", "provider_credentials")
        with_name = extracted_page_names(ctx.chart_name, "name")
    headings = section_heading_texts(ctx.chart_name) if pass_no == 2 else {}
    header_canon = section_heading_canon(ctx.chart_name) if pass_no == 2 else {}
    top = float(method.canon().raw["zones"]["header_band"])
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
        )
        if pass_no == 2:
            inputs.headings = headings.get(page["page_name"], ())
            inputs.header_canon = header_canon.get(page["page_name"], ())
            inputs.lines, inputs.text_area = _page_lines(
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


VOTES_FILE = "blank_junk.json"
VOTES_VERSION = 1


def votes_path(chart_name: str) -> Path:
    return staging_dir(chart_name) / VOTES_FILE


# Every pass's full ballots (DistilBERT, rules, the best duplicate match), for debugging.
DETAIL_FILE = "blank_junk_detail.json"


def _duplicate_view(entry: dict[str, Any]) -> dict[str, Any]:
    """The page's duplicate record (Duplicate_Logic.md section 8) and its closest page."""
    match = entry.get("best_match") or {}
    record = entry.get("duplicate_check") or match.get("record") or {}
    out: dict[str, Any] = {
        "label": record.get("label") or "",
        "duplicate": bool(record.get("duplicate")),
        "copy_of": record.get("copy_of"),
        "rule": record.get("rule") or "",
        "score": record.get("score"),
        "blocked_by": record.get("blocked_by") or "",
        "review": record.get("review") or "",
    }
    if match.get("similarity") is not None:
        out["closest_page"] = match.get("page_name")
        out["similarity"] = match.get("similarity")
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
                # The weight continuity gives a junk run (the pre-2026-11-14 confidence).
                "decision_confidence": entry.get("decision_confidence"),
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
    result, rule, why, and duplicate (label, duplicate, copy_of, rule, score, blocked_by, review; the closest page and its
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
            "decision_confidence": round(float(row.get("strength", row["confidence"])), 4),
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
            inputs = _page_inputs(ctx, pass_no, quality)
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

