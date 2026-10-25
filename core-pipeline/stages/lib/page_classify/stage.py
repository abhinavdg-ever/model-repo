"""Stage: page classification → ``page_classification`` (the Extracted answer).

Every page that is not blank, junk or duplicate is classified on its own,
from the page's text (Final2 → Final1 → Tesseract, plain text), by its page
axes (``axes.predict``): the page-axes model's top 3 where it is sure, the
keyword canon (``page_canon.json``) elsewhere, Others when neither is. They are
written as ``page_type`` / ``page_subtype`` — L1 / Others with one level, L1 /
L2 with two, "L1 (L2)" / L3 with three — with the axes' codeability as the
category, ``decided_by`` model / canon / rule / others, and the whole answer
(the model's top 3 and specialty included) in ``axes``. That per-page result
is the **Extracted** answer and is what this stage writes, to Postgres and to
``<chart>/staging/page_classification.json`` (one entry per page name).

``bert_*`` / ``keyword_*`` columns are no longer filled (the old ladder is
gone; the columns stay). The rules that re-place a page for its document (a
lab page inside a Progress Note; an Others page) need the continuity stage's
documents and are applied by ``imaging_final`` as the **Final** answer.

Blank / junk / duplicate pages are ``non_codeable`` and keep their junk label
as the sub-type; a label that is a taxonomy sub-type (Invoice) also gives its
page type. A duplicate takes its original's page type and sub-type instead
(2026-10-22, user): it is the same page. It stays ``non_codeable`` -- one copy is
coded -- and ``imaging_final`` copies the original's Final values as before.

Runs before ``continuity``, which reads its page type and confidence to see
where a document ends; it reads no DOS itself.

Also writes ``imaging/<chart>_codeable.csv`` for review-ui Local Mode.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from db import (
    connect,
    get_blank_junk_final,
    get_ocr_texts,
    get_quality_map,
    upsert_page_classification,
)
from config import staging_dir
from db.paths import imaging_csv, write_csv
from stages._support import BJ_EXCLUDE, best_page_text, mark_completed, stage_run
from stages.lib.canon_store import CANON_DIR, CanonFile
from stages.lib.page_classify import axes

logger = logging.getLogger(__name__)

STAGE = "page_subtype"

_DISPLAY = {"codeable": "Codable", "non_codeable": "Non-Codable", "discharge_summary": "Discharge"}

# Axes codeability → classification_category. needs_review has no category
# value: the page is written non_codeable with needs_review set.
_AXES_CATEGORY = {"codeable": "codeable", "non_codeable": "non_codeable", "discharge": "discharge_summary"}
UNKNOWN = "Unknown"

CODEABLE_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "page_type",
    "page_subtype",
    "model_type",
    "confidence",
    "codability",
    "tag",
    "is_codeable",
    "decided_by",
    "needs_review",
    "bert_model_type",
    "bert_confidence",
    "keyword_page_subtype",
    "keyword_score",
    "keyword_margin",
    "keyword_title_hit",
    "ocr_source",
    # Page axes, appended so the columns above keep their places.
    "axes_l1",
    "axes_l1_confidence",
    "axes_l2",
    "axes_l2_confidence",
    "axes_l3",
    "axes_l3_confidence",
    "specialty",
    "specialty_confidence",
    "axes_codeability",
    "axes_codeability_confidence",
    "axes_sources",
    "axes_canon_page_type",
    "axes_codeability_rule",
    "axes_title_l1",
    "axes_route",
]

_DOS_PROFILE: CanonFile[dict[str, Any]] = CanonFile(CANON_DIR / "dos_canon.json")

STAGING_FILE = "page_classification.json"
STAGING_VERSION = 1


def staging_path(chart_name: str) -> Path:
    return staging_dir(chart_name) / STAGING_FILE


def _staged_entry(row: dict[str, Any], page_axes: Optional[axes.AxesResult], source: str) -> dict[str, Any]:
    """One page of ``page_classification.json``: the Extracted value, who decided
    it, the model's top 3, and the whole axes answer."""
    whole = page_axes.as_json() if page_axes else (row.get("axes") or None)
    return {
        "page_type": row.get("page_type"),
        "page_subtype": row.get("page_subtype"),
        "model_type": row.get("model_type"),
        "classification_category": row.get("classification_category"),
        "confidence": row.get("confidence"),
        "decided_by": row.get("decided_by"),
        "needs_review": row.get("needs_review"),
        "model_top3": (whole or {}).get("model_top3"),
        "ocr_source": source or None,
        "axes": whole,
    }


def write_staged(chart_name: str, pages: dict[str, dict[str, Any]]) -> Optional[Path]:
    """``staging/page_classification.json``: ``pages`` keyed by page name, written
    atomically (a temp file, then replace) like blank_junk.json."""
    from datetime import datetime, timezone

    path = staging_path(chart_name)
    body = {
        "version": STAGING_VERSION,
        "chart_name": chart_name,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pages": pages,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=1, default=str), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.warning("could not write %s: %s", path, exc)
        return None
    return path


def read_staged(chart_name: str) -> dict[str, dict[str, Any]]:
    """The staged pages by page name; {} when the file is missing, unreadable or
    of another version (readers then fall back to page_classification)."""
    path = staging_path(chart_name)
    try:
        body = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}
    if body.get("version") != STAGING_VERSION:
        return {}
    pages = body.get("pages")
    return pages if isinstance(pages, dict) else {}


def _ocr_source_label(*, final2: Optional[str], final1: Optional[str], prelim: Optional[str]) -> str:
    if final2 and str(final2).strip():
        return "final2"
    if final1 and str(final1).strip():
        return "final1"
    if prelim and str(prelim).strip():
        return "prelim"
    return ""


def _headings(*raws: Optional[str]) -> list[str]:
    """Heading lines the OCR layout detected on the page (section-header
    candidates and matched headers), from the stored Final2 / Final1 JSON."""
    import json

    out: list[str] = []
    for raw in raws:
        try:
            page = json.loads(raw) if raw and str(raw).lstrip().startswith("{") else None
        except ValueError:
            page = None
        if not isinstance(page, dict):
            continue
        for header in (page.get("section_header_candidates") or []) + (page.get("section_headers") or []):
            if isinstance(header, dict) and header.get("text"):
                out.append(str(header["text"]))
        if out:
            break
    return out


def _bj_label(flag: str, junk_subtype: Optional[str]) -> str:
    if flag == "blank":
        return "Blank"
    if flag == "duplicate":
        return "Duplicate"
    return (junk_subtype or "Others").strip() or "Others"


# What a duplicate takes from its original's row: its page type. Codability stays
# non_codeable (only the original is coded).
DUPLICATE_COPIES = ("page_type", "page_subtype", "model_type", "axes")


def _original_of(bj_rows: dict[int, dict[str, Any]], page_id: int) -> Optional[int]:
    """The page a duplicate copies, followed through a chain of duplicates."""
    seen = {page_id}
    current = page_id
    while True:
        row = bj_rows.get(current) or {}
        nxt = row.get("duplicate_of_page_id") if row.get("blank_junk_flag") == "duplicate" else None
        if nxt is None or nxt in seen:
            return current if current != page_id else None
        seen.add(nxt)
        current = nxt


def _blank_junk_row(flag: str, junk_subtype: Optional[str], confidence: Any) -> dict[str, Any]:
    """Blank / junk / duplicate: not classified. The junk label is the
    sub-type; there is no page type."""
    label = _bj_label(flag, junk_subtype)
    return {
        "page_type": None,
        "page_subtype": label,
        "model_type": None,
        "classification_category": "non_codeable",
        "confidence": float(confidence) if confidence is not None else None,
        "duplicate_flag": flag == "duplicate",
        "decided_by": "blank_junk",
        "needs_review": False,
    }


def _page_row(page_axes: Optional[axes.AxesResult]) -> dict[str, Any]:
    """The page's row: names, category and decided_by from its axes."""
    page_type, page_subtype = axes.page_names(page_axes)
    codeability = page_axes.codeability if page_axes else axes.NEEDS_REVIEW
    path = [x for x in (page_axes.l1, page_axes.l2, page_axes.l3) if x] if page_axes else []
    return {
        "page_type": page_type or UNKNOWN,
        "page_subtype": page_subtype or UNKNOWN,
        "model_type": " > ".join(path) or None,
        "classification_category": _AXES_CATEGORY.get(codeability, "non_codeable"),
        "confidence": page_axes.l1_confidence if page_axes else None,
        "duplicate_flag": False,
        "decided_by": page_axes.decided_by if page_axes else "no_text",
        "needs_review": page_type is None or codeability == axes.NEEDS_REVIEW or (
            page_axes is not None and page_axes.decided_by == axes.BY_OTHERS),
        "axes": page_axes.as_json() if page_axes else None,
    }


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, force=force) as ctx:
        with connect() as conn:
            bj_rows = get_blank_junk_final(conn, chart_id)
            prelim = get_ocr_texts(conn, chart_id, "tesseract")
            final1 = get_ocr_texts(conn, chart_id, "docling")
            final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
            quality = get_quality_map(conn, chart_id)

        counts = {"main": 0, "blank_junk": 0, "review": 0, "axes_model": 0, "canon": 0, "others": 0, "specialty": 0}
        # Every page's row first: a duplicate takes its original's page type, and the
        # original can come later in the chart.
        built: dict[int, tuple[dict[str, Any], Any, str]] = {}
        for page in ctx.pages:
            page_id = page["id"]
            bj = bj_rows.get(page_id) or {}
            flag = bj.get("blank_junk_flag") or "not_blank_junk"
            f2, f1, pr = final2.get(page_id), final1.get(page_id), prelim.get(page_id)
            page_axes = None
            if flag in BJ_EXCLUDE:
                row = _blank_junk_row(flag, bj.get("junk_subtype"), bj.get("confidence"))
                counts["blank_junk"] += 1
                source = ""
            else:
                text = best_page_text(final2=f2, final1=f1, prelim=pr, quality_row=quality.get(page_id))
                headings = tuple(_headings(f2, f1))
                page_axes = axes.predict(text, headings)
                row = _page_row(page_axes)
                if page_axes is not None:
                    counts["axes_model"] += int(page_axes.decided_by == axes.MODEL)
                    counts["canon"] += int(page_axes.decided_by == axes.CANON)
                    counts["others"] += int(page_axes.decided_by == axes.BY_OTHERS)
                    counts["specialty"] += int(page_axes.specialty is not None)
                counts["main"] += 1
                counts["review"] += int(row["needs_review"])
                source = _ocr_source_label(final2=f2, final1=f1, prelim=pr)
            built[page_id] = (row, page_axes, source)

        for page_id, (row, _, _) in built.items():
            if not row["duplicate_flag"]:
                continue
            original = _original_of(bj_rows, page_id)
            reference = built.get(original, (None,))[0] if original is not None else None
            if reference is not None and reference.get("page_type"):
                row.update({key: reference.get(key) for key in DUPLICATE_COPIES})
                row["decided_by"] = f"duplicate_of:{original}"

        csv_rows: list[dict[str, Any]] = []
        staged: dict[str, dict[str, Any]] = {}
        with connect() as conn:
            for page in ctx.pages:
                page_id = page["id"]
                row, page_axes, source = built[page_id]
                upsert_page_classification(conn, chart_id=chart_id, page_id=page_id, **row)
                staged[page["page_name"]] = _staged_entry(row, page_axes, source)
                if page_id in ctx.todo:
                    mark_completed(conn, ctx, page_id)

                category = row["classification_category"]
                csv_rows.append(
                    {
                        "chart_name": ctx.chart_name,
                        "page_name": page["page_name"],
                        "page_number": page.get("page_number"),
                        **{k: ("" if v is None else v) for k, v in row.items()
                           if k in CODEABLE_COLS},
                        "codability": _DISPLAY.get(category, ""),
                        "tag": category,
                        "is_codeable": _DISPLAY.get(category, ""),
                        "needs_review": "y" if row["needs_review"] else "n",
                        "ocr_source": source,
                        **{k: ("" if v is None else v)
                           for k, v in (page_axes.csv_columns() if page_axes else {}).items()},
                    }
                )

        path = write_csv(imaging_csv(ctx.chart_name, "codeable"), CODEABLE_COLS, csv_rows)
        staged_path = write_staged(ctx.chart_name, staged)
        logger.info(
            "page_subtype chart=%s main=%d blank_junk=%d review=%d model=%d canon=%d others=%d "
            "specialty=%d → %s",
            ctx.chart_name, counts["main"], counts["blank_junk"], counts["review"],
            counts["axes_model"], counts["canon"], counts["others"], counts["specialty"], path,
        )
        return {
            "chart_id": chart_id,
            "codeable_csv": str(path),
            "page_classification_json": str(staged_path) if staged_path else None,
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            **counts,
            "page_axes": axes.status().get("ready", False),
        }
