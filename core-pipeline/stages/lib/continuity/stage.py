"""Stage: document continuity → ``page_continuity_results``.

Runs after page type and DOS. Scores every gap between consecutive pages (``engine``,
docs/CONTINUITY_LOGIC.md) and groups the pages into documents and junk segments. It
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
    upsert_continuity,
)
from config import staging_dir
from db.paths import imaging_csv, write_csv
from stages._support import best_page_text, mark_completed, pass1_ruled_out, stage_run
from stages.lib.continuity.engine import (
    PageInput,
    assign,
    load_rules,
    text_lines,
    trust_points,
)
from stages.lib.continuity.layout import load_pages, page_layout
from stages.lib.continuity.signature import has_signature_section, signed_page_names
from stages.lib.extraction.dos.extract import TIER_RANK
from stages.lib.extraction.member_id.id_types import guess_id_type
from stages.lib.extraction.util.dates import canonical_date
from stages.lib.page_classify import axes

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


def scores_path(chart_name: str):
    return staging_dir(chart_name) / STAGING_FILE


def _write_scores(chart_name: str, inputs: list[PageInput], rows: list[dict[str, Any]], rules: Any) -> str:
    """Every gap's scores and signals, per page and per document, beside extraction.json."""
    documents: dict[int, dict[str, Any]] = {}
    pages = []
    for item, row in zip(inputs, rows):
        number = row["document_seq"]
        if number is not None:
            doc = documents.setdefault(number, {
                "document": number, "kind": row["segment_kind"], "pages": [],
                # A junk group has no visit: its DOS is skipped.
                "dos_from": "Skipped" if row["segment_kind"] == "junk" else row["final_dos_from"],
                "dos_to": "Skipped" if row["segment_kind"] == "junk" else row["final_dos_to"],
                "page_type": row["final_page_type"],
            })
            doc["pages"].append(item.page_name)
        pages.append({
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
            "gap_before": row["signals"]["before"],
            "gap_after": row["signals"]["after"],
        })
    path = scores_path(chart_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "chart_name": chart_name,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "canon_version": 4,
        "documents": list(documents.values()),
        "pages": pages,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)
    return str(path)


def _kind(final: Optional[dict[str, Any]]) -> str:
    flag = (final or {}).get("blank_junk_flag") or "not_blank_junk"
    return {"blank": "blank", "junk": "junk", "duplicate": "duplicate"}.get(flag, "main")


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

        from stages.lib.extraction.stage import ensure_staging

        staging = ensure_staging(chart_id, ctx.chart_name)
        signed_names = signed_page_names(ctx.chart_name)
        json2 = load_pages(ctx.chart_name, "final2")
        json1 = load_pages(ctx.chart_name, "final1")

        rules = load_rules()
        # Continuity is chart-global: every page is an input even on a resume.
        inputs: list[PageInput] = []
        sources: dict[Any, str] = {}
        for page in ctx.pages:
            page_id = page["id"]
            name = page["page_name"]
            final = bj.get(page_id)
            kind = _kind(final)
            staged = staging.page(name) if staging else None
            pass1_only = page_id in settled
            text = "" if kind in ("blank", "duplicate") else best_page_text(
                final2=None if pass1_only else final2.get(page_id),
                final1=None if pass1_only else final1.get(page_id),
                prelim=prelim.get(page_id),
                quality_row=quality.get(page_id),
            )
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
                    # Pages are compared on their family (v24 L1) and full type path.
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
                    duplicate_of=(final or {}).get("duplicate_of_page_id") if kind == "duplicate" else None,
                )
            )

        rows = assign(inputs, rules)
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
