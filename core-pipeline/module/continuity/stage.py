"""Stage: document continuity → ``page_continuity_results``.

Runs after page type and DOS. Scores every gap between consecutive pages (``engine``,
docs/APPROACH.md §4) and groups the pages into documents and junk segments. It
writes only the grouping; Final values are written by the ``imaging_final`` stage.

The key/value extraction's staging (``<chart>/staging/extraction.json``) is the first
source for every per-page fact the scorer needs:

* **DOS trust** — the tier of the staged DOS row behind the page's Extracted DOS
  (service / admit / discharge = a label, weak = an edge keyword, keyless);
* **printed page number** — the selected ``page_no`` row (N of M);
* **signature** — accepted ``electronic_signature`` rows: any one signs the page, one in
  the footer signs its closing lines;
* **visit number** — accepted ``member_id`` rows whose key is an encounter-type ID
  (FIN, CSN, account, visit);
* **section headings** — accepted ``heading_heron`` section rows, beside the OCR JSON's.

The text regexes and the provider-signature CSV fill in where the staging has nothing.
Writes the grouping to ``page_continuity_results`` (the original columns: documents and junk
groups share ``document_seq``; ``score`` is the Break Before), every score and signal of
every gap to ``<chart>/staging/continuity.json``, and ``imaging/<chart>_continuity.csv``
for review-ui Local Mode.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from db import (
    connect,
    get_additional_page_details_map,
    get_blank_junk_final,
    get_dos_map,
    get_ocr_texts,
    get_page_classification_map,
    get_quality_map,
    page_type,
    revise_blank_junk_final,
    upsert_continuity,
)
from config import staging_dir
from db.paths import imaging_csv, write_csv
from stages._support import best_page_text, mark_completed, pass1_ruled_out, stage_run
from module.blank_junk.method import text_is_visual
from module.continuity.engine import (
    PageInput,
    assign,
    load_rules,
    text_lines,
    trust_points,
)
from module.continuity import duplicates
from module.continuity.layout import load_pages, page_layout
from module.continuity.signature import has_signature_section, signed_page_names
from module.extraction.dos.extract import TIER_RANK
from module.extraction.member_id.id_types import guess_id_type
from module.extraction.util.dates import canonical_date
from module.page_classify import axes

logger = logging.getLogger(__name__)

STAGE = "continuity"

CONTINUITY_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "kind",
    "document",
    "segment",
    "continuation",
    "tag",
    "position",
    "page_in_document",
    "relation",
    "break_before",
    "break_after",
    "confidence",
    "confidence_level",
    "decided_by",
    "link_strength",
    "start_confirmed",
    "page_no",
    "page_total",
    "page_type",
    "dos_from",
    "dos_to",
    "dos_trust",
    "section_headers",
    "layout_source",
    "evidence",
    "score",
    "review_required",
]


def _iso(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")


def _printed(details: Optional[dict[str, Any]]) -> Optional[tuple[int, int]]:
    """The printed ``page N of M`` the key/value stage stored (additional_page_details)."""
    if not details:
        return None
    try:
        number = int(details.get("printed_page_no") or 0)
        total = int(details.get("printed_page_total") or 0)
    except (TypeError, ValueError):
        return None
    return (number, total) if 1 <= number <= total else None


def _staged_printed(staged: Any, max_total: int) -> Optional[tuple[int, int]]:
    for row in staged.selected("page_no") if staged else []:
        try:
            number, total = int(row.get("page_no") or 0), int(row.get("page_total") or 0)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= total <= max_total:
            return number, total
    return None


def _staged_trust(staged: Any, dos_from: str) -> Optional[int]:
    """Points for how the page's Extracted DOS was found: the best tier among the staged
    DOS rows that read that date; None when the staging has no such row."""
    if not staged or not dos_from:
        return None
    tiers = [
        str(row.get("tier") or "")
        for row in staged.accepted("dos")
        if canonical_date(str(row.get("dos_from") or row.get("value") or "")) == dos_from
    ]
    if not tiers:
        return None
    return trust_points(min(tiers, key=lambda t: TIER_RANK.get(t, 9)))


def _staged_signature(staged: Any) -> tuple[bool, bool]:
    """(signed anywhere, signed in the closing lines) from the accepted e-signatures."""
    rows = staged.accepted("electronic_signature") if staged else []
    return bool(rows), any(str(row.get("region") or "") == "footer" for row in rows)


def _staged_visits(staged: Any) -> frozenset[str]:
    """Encounter / visit / FIN / CSN / account numbers the extraction accepted."""
    return frozenset(
        str(row.get("value") or "").strip().casefold()
        for row in (staged.accepted("member_id") if staged else [])
        if str(row.get("value") or "").strip() and guess_id_type(str(row.get("key") or "")) == "encounter"
    )


def _staged_sections(staged: Any) -> list[dict[str, Any]]:
    return [
        {"text": str(row.get("text") or ""), "matched_canonical": "", "top": None}
        for row in (staged.rows("heading_heron") if staged else [])
        if row.get("accepted") and row.get("kind") == "section" and row.get("text")
    ]


def _tail_text(lines: list[Any], fraction: float = 0.33) -> str:
    """The page's last third of lines: where a closing signature sits."""
    if not lines:
        return ""
    keep = max(1, int(round(len(lines) * fraction)))
    return "\n".join(line.text for line in lines[-keep:])


STAGING_FILE = "continuity.json"
# Every gap's full scoring (each signal, sub-scores, flags), for debugging.
DETAIL_FILE = "continuity_detail.json"


def scores_path(chart_name: str):
    return staging_dir(chart_name) / STAGING_FILE


# Each signal in words, for continuity.json's "because" (2026-10-23, user: make it simple).
_SAYS = {
    "different_dos": "different DOS",
    "same_dos": "same DOS",
    "pagination_follows": "page numbers run on",
    "signed_in_tail": "previous page signed at its end",
    "signed_anywhere": "previous page signed",
    "last_numbered_page": "previous page is its last numbered page",
    "has_title": "title at the top",
    "visit_heading": "visit heading",
    "page_type_change": "page type changes",
    "junk_run": "junk pages begin or end",
    "says_continued": "says continued",
    "sections_forward": "sections run on in order",
    "same_visit_number": "same visit number",
    "text_start": "where the text starts",
    "same_header_line": "same header line",
    "same_footer_line": "same footer line",
    "wording_char": "wording runs on",
    "wording_word": "wording runs on (words)",
    "same_junk_kind": "same kind of junk",
    "blank_between": "blank page between",
    "cut_mid_sentence": "previous page ends mid-sentence",
    "starts_mid_thought": "starts mid-sentence",
    "same_page_type": "same page type",
}


def _because(gap: dict[str, Any]) -> list[str]:
    """The gap's strongest reasons in words, strongest first (at most four), after any
    rule that forced it."""
    out: list[str] = []
    evidence = gap.get("evidence") or {}
    if evidence.get("demographics_boundary"):
        out.append("Demographics page beside another kind of page: always a break")
    parts = {**(gap.get("ending") or {}), **(gap.get("opening") or {}), **(gap.get("pair") or {})}
    for name, value in sorted(parts.items(), key=lambda kv: -abs(kv[1]))[:4]:
        if name == "page_fill":
            words = "previous page ends early" if value > 0 else "previous page is full"
        elif name == "visit_heading" and evidence.get("visit_heading"):
            words = f"visit heading '{evidence['visit_heading']}'"
        else:
            words = _SAYS.get(name, name.replace("_", " "))
        out.append(f"{words} {value:+g}")
    if gap.get("final_pass"):
        out.append(f"final pass: {gap['final_pass']}")
    return out


def _gap_view(gap: Optional[dict[str, Any]], edge: str) -> Any:
    if not gap:
        return edge
    return {
        "decision": "Break" if gap.get("decision") == "break" else "Same document",
        "score": gap.get("score"),
        "sure": gap.get("level"),
        "because": _because(gap),
    }


def _write_scores(chart_name: str, inputs: list[PageInput], rows: list[dict[str, Any]], rules: Any) -> str:
    """``continuity.json``: the documents, then each page's gap before (prev_to_this), gap
    after (this_to_next) and final result, in words. Every signal of every gap goes to
    ``continuity_detail.json`` beside it."""
    documents: dict[int, dict[str, Any]] = {}
    detail_documents: dict[int, dict[str, Any]] = {}
    pages: dict[str, Any] = {}
    detail_pages = []
    for item, row in zip(inputs, rows):
        number = row["document_seq"]
        junk = row["segment_kind"] == "junk"
        if number is not None:
            doc = detail_documents.setdefault(number, {
                "document": number, "kind": row["segment_kind"], "pages": [],
                # A junk group has no visit: its DOS is skipped.
                "dos_from": "Skipped" if junk else row["final_dos_from"],
                "dos_to": "Skipped" if junk else row["final_dos_to"],
                "page_type": row["final_page_type"],
            })
            doc["pages"].append(item.page_name)
            documents.setdefault(number, {
                "document": number,
                "kind": "Junk" if junk else "Document",
                "page_type": row["final_page_type"],
                "dos": "Skipped" if junk else row["final_dos_from"],
                "pages": doc["pages"],
            })
        before, after = row["signals"]["before"], row["signals"]["after"]
        pages[item.page_name] = {
            "page": item.page_number,
            "kind": item.kind,
            "prev_to_this": _gap_view(before, "first page"),
            "this_to_next": _gap_view(after, "last page"),
            "final": {
                "document": number,
                "tag": row["label"],
                "page_type": row.get("final_page_type"),
                "dos": row.get("final_dos_from"),
                "review": bool(row["review_required"]),
            },
        }
        detail_pages.append({
            "page_name": item.page_name,
            "page_number": item.page_number,
            "kind": item.kind,
            "document": number,
            "label": row["label"],
            "tag": row["tag"],
            "break_before": row["break_before"],
            "break_after": row["break_after"],
            "decided_by": row["decided_by"],
            "confidence": row["confidence"],
            "confidence_level": row["confidence_level"],
            "review_required": row["review_required"],
            "dos_from": item.dos_from,
            "dos_trust": item.dos_trust,
            "printed": list(item.printed) if item.printed else None,
            "signed": item.signed,
            "signed_in_tail": item.signed_in_tail,
            "title": item.title_hit,
            "page_type": item.page_type,
            "visit_ids": sorted(item.visit_ids),
            "gap_before": before,
            "gap_after": after,
        })
    written_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path = scores_path(chart_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # The encounter stage, which runs next, appends "encounters" to this file.
    payload: dict[str, Any] = {
        "version": 2,
        "chart_name": chart_name,
        "written_at": written_at,
        "documents": list(documents.values()),
        "pages": pages,
    }
    detail = {
        "version": 1,
        "chart_name": chart_name,
        "written_at": written_at,
        "canon_version": 4,
        "documents": list(detail_documents.values()),
        "pages": detail_pages,
    }
    for target, body in ((path, payload), (path.with_name(DETAIL_FILE), detail)):
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
        tmp.replace(target)
    return str(path)


def _kind(final: Optional[dict[str, Any]]) -> str:
    flag = (final or {}).get("blank_junk_flag") or "not_blank_junk"
    if _content_copy(final) or _carried_copy(final):
        return "duplicate"  # an earlier run's duplicate content / carried-forward page: checked again
    return {"blank": "blank", "junk": "junk", "duplicate": "duplicate"}.get(flag, "main")


# A junk page inside a Progress Note made Main again carries this, then its junk subtype.
# 2026-11-03 (user), undone 2026-11-04, restored 2026-11-06 (user).
UNJUNK_MARK = "unjunked_in_progress_note:"
# Junk that stays junk inside a note: blank / tiny / cover mark (R1, R2), contained in a nearby
# page, and cards (junk_subtype Cards).
_KEEP_JUNK = ("R1", "R2", "short_page_contained_in_page_id:")


def _unjunked(final: Optional[dict[str, Any]]) -> Optional[str]:
    """The junk subtype of a page an earlier run unjunked inside a Progress Note."""
    reason = str((final or {}).get("reason") or "")
    if not reason.startswith(UNJUNK_MARK):
        return None
    return reason[len(UNJUNK_MARK):].split(" (", 1)[0].strip() or "Others"


def _unjunk_inside_notes(
    chart_id: int,
    chart_name: str,
    inputs: list[PageInput],
    rows: list[dict[str, Any]],
    bj: dict[int, dict[str, Any]],
    rules: Any,
    texts: Optional[dict[int, str]] = None,
) -> list[dict[str, Any]]:
    """2026-11-06 (user): continuity may undo a junk. A junk page whose nearest Main pages before
    and after it are in one document that opens on a Progress Note is a Main page again, unless
    it is a short page (R1 / R2 blank, tiny or cover mark; contained in a nearby page) or a card.
    Re-checked every run: a page no longer inside a note is junk again, with its subtype."""
    document = [row.get("document_seq") if row.get("segment_kind") == "main" else None for row in rows]
    first_type: dict[Any, str] = {}
    for item, doc in zip(inputs, document):
        if doc is not None and doc not in first_type:
            first_type[doc] = item.page_type or ""

    def inside_note(i: int) -> bool:
        before = next((document[k] for k in range(i - 1, -1, -1) if document[k] is not None), None)
        after = next((document[k] for k in range(i + 1, len(inputs)) if document[k] is not None), None)
        return before is not None and before == after and first_type.get(before) == "Progress Note"

    unjunk, rejunk = [], []
    for i, item in enumerate(inputs):
        final = bj.get(item.page_id) or {}
        earlier = _unjunked(final)
        if item.kind == "junk" and earlier is None:
            reason = str(final.get("reason") or "")
            if item.junk_subtype == "Cards" or reason.startswith(_KEEP_JUNK):
                continue
            if inside_note(i):
                unjunk.append((item, item.junk_subtype or "Others"))
        elif earlier is not None and not inside_note(i):
            rejunk.append((item, earlier))
    if not (unjunk or rejunk):
        return rows
    with connect() as conn:
        for item, subtype in unjunk:
            revise_blank_junk_final(
                conn, item.page_id, blank_junk_flag="not_blank_junk", duplicate_of_page_id=None,
                reason=f"{UNJUNK_MARK}{subtype} (junk inside a Progress Note, not a short page -> Main)",
            )
            item.kind, item.junk_subtype = "main", ""
            item.text = (texts or {}).get(item.page_id) or item.text
            item.lines = item.lines or text_lines(item.text)
        for item, subtype in rejunk:
            revise_blank_junk_final(
                conn, item.page_id, blank_junk_flag="junk", duplicate_of_page_id=None,
                reason=f"re-checked: no longer inside a Progress Note -> Junk / {subtype}", junk_subtype=subtype,
            )
            item.kind, item.junk_subtype = "junk", subtype
        _rewrite_junk_csv(conn, chart_id, chart_name)
    logger.info("continuity chart=%s junk inside a Progress Note: %d -> Main, %d back to junk",
                chart_name, len(unjunk), len(rejunk))
    return assign(inputs, rules)


def _carried_copy(final: Optional[dict[str, Any]]) -> bool:
    """A page an earlier run's duplicate check made a plain Main page (carried-forward content)."""
    return duplicates.CARRIED_MARK in str((final or {}).get("reason") or "")


def _carried_original(final: Optional[dict[str, Any]]) -> Optional[int]:
    import re as _re

    found = _re.search(_re.escape(duplicates.CARRIED_MARK) + r"(\d+)", str((final or {}).get("reason") or ""))
    return int(found.group(1)) if found else None


# Blank / junk pass 2's same content under different encounters (2026-11-04): already decided,
# never re-checked here.
SAME_CONTENT_NOTE = "same content, different encounter"


def _content_copy(final: Optional[dict[str, Any]]) -> bool:
    """A page an earlier run's duplicate check made a Main page with duplicate content."""
    reason = str((final or {}).get("reason") or "")
    return (bool((final or {}).get("duplicate_of_page_id")) and duplicates.CONTENT_MARK in reason
            and SAME_CONTENT_NOTE not in reason)


def _review_duplicates(
    chart_id: int,
    chart_name: str,
    inputs: list[PageInput],
    rows: list[dict[str, Any]],
    texts: dict[int, str],
    bj: dict[int, dict[str, Any]],
    rules: Any,
) -> list[dict[str, Any]]:
    """The encounter check on duplicates (duplicates.py): revise the blank / junk
    verdicts it changes, and assign the documents again when any changed."""
    from module.blank_junk.stage import extracted_dos

    by_name = {item.page_name: item.page_id for item in inputs}
    dos = {by_name[name]: dates for name, dates in extracted_dos(chart_name).items() if name in by_name}
    partial_of = {
        item.page_id: int(bj[item.page_id]["duplicate_of_page_id"])
        for item in inputs
        if item.kind == "main" and (bj.get(item.page_id) or {}).get("duplicate_of_page_id")
    }
    result = duplicates.review(inputs, rows, texts, partial_of, dos)
    restored = [
        item for item in inputs
        if (_content_copy(bj.get(item.page_id)) and item.page_id not in result.demote)
        or (_carried_copy(bj.get(item.page_id)) and item.page_id not in result.release)
    ]
    if not (result.promote or result.demote or result.release or result.partial or restored):
        return rows
    by_id = {item.page_id: item for item in inputs}
    with connect() as conn:
        for page_id, (original, average) in result.promote.items():
            revise_blank_junk_final(
                conn, page_id, blank_junk_flag="duplicate", duplicate_of_page_id=original,
                reason=f"duplicate_of_page_id:{original} (its encounter averages {average:.4f} "
                       f"against the original's: a duplicate encounter; was a partial duplicate)",
            )
            item = by_id[page_id]
            item.kind, item.duplicate_of, item.text, item.dos_from, item.dos_to = "duplicate", original, "", "", ""
        for page_id, (original, average) in result.demote.items():
            revise_blank_junk_final(
                conn, page_id, blank_junk_flag="not_blank_junk", duplicate_of_page_id=original,
                reason=f"{duplicates.CONTENT_MARK}{original} (its encounter averages {average:.4f} "
                       f"against the original's, not above 0.98: duplicate content, not a duplicate encounter)",
            )
            # A Main page with its original's content values: the stages after blank /
            # junk skipped it as a duplicate.
            item, source = by_id[page_id], by_id.get(original)
            item.kind, item.duplicate_of, item.text = "main", None, texts.get(page_id) or ""
            item.lines = item.lines or text_lines(item.text)
            if source is not None:
                item.page_type, item.page_subtype, item.names = source.page_type, source.page_subtype, source.names
                item.page_confidence, item.title_hit = source.page_confidence, source.title_hit
                item.dos_from, item.dos_to, item.dos_trust = source.dos_from, source.dos_to, source.dos_trust
        for page_id, (original, average) in result.release.items():
            revise_blank_junk_final(
                conn, page_id, blank_junk_flag="not_blank_junk", duplicate_of_page_id=None,
                reason=f"{duplicates.CARRIED_MARK}{original} (a single page, not a 100% copy, in another "
                       f"encounter: carried-forward content, not a duplicate)",
            )
            item, source = by_id[page_id], by_id.get(original)
            item.kind, item.duplicate_of, item.text = "main", None, texts.get(page_id) or ""
            item.lines = item.lines or text_lines(item.text)
            if source is not None:
                item.page_type, item.page_subtype, item.names = source.page_type, source.page_subtype, source.names
                item.page_confidence, item.title_hit = source.page_confidence, source.title_hit
                item.dos_from, item.dos_to, item.dos_trust = source.dos_from, source.dos_to, source.dos_trust
        for page_id, (original, overlap) in result.partial.items():
            # The same encounter, not a 100% match (2026-11-03, user): a Partial -- a Main page
            # naming the page it nearly copies, with its original's values for grouping.
            revise_blank_junk_final(
                conn, page_id, blank_junk_flag="not_blank_junk", duplicate_of_page_id=original,
                reason=f"partial_duplicate_of_page_id:{original} (overlap={overlap:.4f}: the same "
                       f"encounter, not a 100% match)",
            )
            item, source = by_id[page_id], by_id.get(original)
            item.kind, item.duplicate_of, item.text = "main", None, texts.get(page_id) or ""
            item.lines = item.lines or text_lines(item.text)
            if source is not None:
                item.page_type, item.page_subtype, item.names = source.page_type, source.page_subtype, source.names
                item.page_confidence, item.title_hit = source.page_confidence, source.title_hit
                item.dos_from, item.dos_to, item.dos_trust = source.dos_from, source.dos_to, source.dos_trust
        for item in restored:
            original = bj[item.page_id].get("duplicate_of_page_id") or _carried_original(bj[item.page_id])
            if not original:
                continue
            revise_blank_junk_final(
                conn, item.page_id, blank_junk_flag="duplicate", duplicate_of_page_id=original,
                reason=f"duplicate_of_page_id:{original} (re-checked: a duplicate)",
            )
        _rewrite_junk_csv(conn, chart_id, chart_name)
    logger.info(
        "continuity chart=%s duplicate check: %d partial -> duplicate, %d duplicate -> duplicate content, "
        "%d duplicate -> carried-forward Main, %d same-encounter duplicate -> Partial",
        chart_name, len(result.promote), len(result.demote), len(result.release), len(result.partial),
    )
    return assign(inputs, rules)


def _rewrite_junk_csv(conn: Any, chart_id: int, chart_name: str) -> None:
    """The junk CSV after the duplicate check changed verdicts (as the blank / junk stage
    writes it)."""
    try:
        from module.blank_junk.stage import _rewrite_csv
    except Exception:  # noqa: BLE001 - the CSV is a convenience; the database is the record
        logger.warning("continuity chart=%s: junk CSV not rewritten after the duplicate check", chart_name)
        return
    _rewrite_csv(conn, chart_id, chart_name)


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, force=force) as ctx:
        with connect() as conn:
            bj = get_blank_junk_final(conn, chart_id)
            prelim = get_ocr_texts(conn, chart_id, "tesseract")
            final1 = get_ocr_texts(conn, chart_id, "docling")
            final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
            quality = get_quality_map(conn, chart_id)
            classification = get_page_classification_map(conn, chart_id)
            dos = get_dos_map(conn, chart_id)
            details = get_additional_page_details_map(conn, chart_id)
            # Pages pass 1 settled are read as before Final OCR 1 ran on them (2026-10-22):
            # their Tesseract text, no Final layout -- that OCR changes nothing downstream.
            settled = pass1_ruled_out(conn, chart_id)

        from module.extraction.stage import ensure_staging

        staging = ensure_staging(chart_id, ctx.chart_name)
        signed_names = signed_page_names(ctx.chart_name)
        json2 = load_pages(ctx.chart_name, "final2")
        json1 = load_pages(ctx.chart_name, "final1")

        rules = load_rules()
        # Continuity is chart-global: every page is an input even on a resume.
        inputs: list[PageInput] = []
        sources: dict[Any, str] = {}
        # Every page's own text, duplicates included, for the duplicate check.
        own_texts: dict[int, str] = {}
        for page in ctx.pages:
            page_id = page["id"]
            name = page["page_name"]
            final = bj.get(page_id)
            kind = _kind(final)
            staged = staging.page(name) if staging else None
            pass1_only = page_id in settled
            own_texts[page_id] = "" if kind == "blank" else best_page_text(
                final2=None if pass1_only else final2.get(page_id),
                final1=None if pass1_only else final1.get(page_id),
                prelim=prelim.get(page_id),
                quality_row=quality.get(page_id),
            )
            text = "" if kind in ("blank", "duplicate") else own_texts[page_id]
            lines, headers, source = (
                ([], [], "") if pass1_only else page_layout(json2.get(name), json1.get(name))
            )
            lines = lines or text_lines(text)
            sources[page_id] = source or "text"
            page_dos = dos.get(page_id) or {}
            dos_from = _iso(page_dos.get("date_of_service_from"))
            kind_row = classification.get(page_id) or {}
            sig_any, sig_tail = _staged_signature(staged)
            inputs.append(
                PageInput(
                    page_id=page_id,
                    page_name=name,
                    page_number=page.get("page_number"),
                    text=text,
                    lines=lines,
                    section_headers=headers + _staged_sections(staged),
                    # Pages are compared on their family (L1) and full type path.
                    page_type=axes.family_of(kind_row),
                    page_subtype=kind_row.get("page_subtype") or "",
                    names=axes.names_of(kind_row),
                    page_confidence=(
                        float(kind_row["confidence"]) if kind_row.get("confidence") is not None else None
                    ),
                    title_hit=bool(axes.title_of(kind_row)) if axes.stored(kind_row) else bool(kind_row.get("keyword_title_hit")),
                    dos_from=dos_from if kind == "main" else "",
                    dos_to=_iso(page_dos.get("date_of_service_to")) if kind == "main" else "",
                    dos_trust=(_staged_trust(staged, dos_from) or trust_points(None, rules)) if dos_from else None,
                    signed=sig_any or name in signed_names or has_signature_section(text),
                    signed_in_tail=sig_tail or has_signature_section(_tail_text(lines)),
                    printed=_staged_printed(staged, rules.max_total) or _printed(details.get(page_id)),
                    visit_ids=_staged_visits(staged),
                    kind=kind,
                    junk_subtype=str((final or {}).get("junk_subtype") or "") if kind == "junk" else "",
                    junk_confidence=(
                        float(final["confidence"]) if kind == "junk" and (final or {}).get("confidence") is not None else None
                    ),
                    duplicate_of=(
                        (final or {}).get("duplicate_of_page_id") or _carried_original(final)
                    ) if kind == "duplicate" else None,
                    visual=kind == "main" and text_is_visual(page_type(quality.get(page_id)), text),
                )
            )

        rows = assign(inputs, rules)
        # 2026-10-29 (user): a far copy in a Progress Note / Discharge Summary / Operative Note / Clinical Summary is a
        # duplicate only when its whole encounter is a copy.
        rows = _review_duplicates(chart_id, ctx.chart_name, inputs, rows, own_texts, bj, rules)
        # The short page contained in a nearby page is junked in blank / junk pass 2 since
        # 2026-11-03 (user); continuity reads it as any junk page.
        # 2026-11-06 (user): a junk page inside a Progress Note that is not a short page is Main.
        rows = _unjunk_inside_notes(chart_id, ctx.chart_name, inputs, rows, bj, rules, own_texts)
        scores_file = _write_scores(ctx.chart_name, inputs, rows, rules)

        csv_rows: list[dict[str, Any]] = []
        documents = junk_segments = 0
        with connect() as conn:
            for page, row, item in zip(ctx.pages, rows, inputs):
                page_id = page["id"]
                # Content and junk pages get a row (one numbering for documents and junk
                # groups); blank pages and duplicates have none (a duplicate takes its
                # original's values in imaging_final).
                if row["document_seq"] is not None and item.kind in ("main", "junk"):
                    upsert_continuity(conn, chart_id=chart_id, page_id=page_id, row=row)
                if page_id in ctx.todo:
                    mark_completed(conn, ctx, page_id)
                if row["segment_kind"] == "main" and row["tag"] in ("new", "standalone"):
                    documents += 1
                elif row["segment_kind"] == "junk" and row["tag"] in ("new", "standalone"):
                    junk_segments += 1
                csv_rows.append(
                    {
                        "chart_name": ctx.chart_name,
                        "page_name": page["page_name"],
                        "page_number": page.get("page_number"),
                        "kind": item.kind,
                        "document": row["document_seq"] or "",
                        "segment": (
                            f"Doc {row['document_seq']}" if row["segment_kind"] == "main"
                            else f"Junk {row['document_seq']}" if row["segment_kind"] == "junk" else ""
                        ),
                        "continuation": row["label"],
                        "tag": row["tag"] or "",
                        "position": row["position"] or "",
                        "page_in_document": row["seq"] or "",
                        "relation": row["relation"],
                        "break_before": "" if row["break_before"] is None else row["break_before"],
                        "break_after": "" if row["break_after"] is None else row["break_after"],
                        "confidence": "" if row["confidence"] is None else row["confidence"],
                        "confidence_level": row["confidence_level"],
                        "decided_by": row["decided_by"],
                        "link_strength": row["link_strength"] or "",
                        "start_confirmed": "y" if row["start_confirmed"] else ("n" if row["relation"] else ""),
                        "page_no": row["page_no"] or "",
                        "page_total": row["page_total"] or "",
                        "page_type": row["page_type"],
                        # A junk group has no visit: its DOS is skipped.
                        "dos_from": "Skipped" if row["segment_kind"] == "junk" else row["dos_from"],
                        "dos_to": "Skipped" if row["segment_kind"] == "junk" else row["dos_to"],
                        "dos_trust": item.dos_trust or "",
                        "section_headers": json.dumps(
                            [h.get("matched_canonical") or h.get("text") for h in row["section_headers"]]
                        ),
                        "layout_source": sources.get(page_id, ""),
                        "evidence": row["evidence"],
                        "score": "" if row.get("score") is None else row["score"],
                        "review_required": "y" if row.get("review_required") else ("n" if row["relation"] else ""),
                    }
                )

        path = write_csv(imaging_csv(ctx.chart_name, "continuity"), CONTINUITY_COLS, csv_rows)
        logger.info(
            "continuity chart=%s pages=%d documents=%d junk_segments=%d → %s",
            ctx.chart_name, len(rows), documents, junk_segments, path,
        )
        return {
            "chart_id": chart_id,
            "continuity_csv": str(path),
            "continuity_json": scores_file,
            "documents": documents,
            "junk_segments": junk_segments,
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
        }
