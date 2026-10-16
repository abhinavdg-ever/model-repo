"""Stage: page classification → ``page_classification`` (the Extracted answer).

Every page that is not blank, junk or duplicate is classified on its own,
from the page's text (Final2 → Final1 → Tesseract, plain text), by its v24
page axes (``axes.predict``): L1, L2 and L3 from the page-axes model where it
is sure and the v24 keyword canon elsewhere. They are written as
``page_type`` / ``page_subtype`` — L1 / L1 with one level, L1 / L2 with two,
"L1 (L2)" / L3 with three — with the axes' codeability as the category and
the whole answer (specialty included) in ``axes``. That per-page result is
the **Extracted** answer and is what this stage writes.

The v1 ladder still runs, for its raw columns only (``bert_*``,
``keyword_*``): BERT (``bert.py``, retired — no model folder, so NULL) and the
v1 keyword canon (``keywords.py``), arbitrated by
``arbitration.classify_page``. Its page type is no longer written. The continuation rules that can change it (an untitled
page continuing another document; lab or radiology inside a Progress Note)
need the continuity stage's documents and are applied by ``imaging_final`` as
the **Final** answer.

Blank / junk / duplicate pages are ``non_codeable`` and keep their junk label
as the sub-type; a label that is a taxonomy sub-type (Invoice) also gives its
page type.

Runs before ``continuity``, which reads its page type and confidence to see
where a document ends; it reads no DOS itself.

Also writes ``imaging/<chart>_codeable.csv`` for review-ui Local Mode.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from db import (
    connect,
    get_blank_junk_final,
    get_ocr_texts,
    get_quality_map,
    upsert_page_classification,
)
from db.paths import imaging_csv, write_csv
from stages._support import BJ_EXCLUDE, best_page_text, mark_completed, stage_run
from stages.lib.canon_store import CANON_DIR, CanonFile
from stages.lib.page_classify import axes, bert, keywords
from stages.lib.page_classify.arbitration import classify_page

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


def _page_row(page_axes: Optional[axes.AxesResult], ladder: Any) -> dict[str, Any]:
    """The page's row: names and category from its axes; the v1 ladder's raw
    model outputs beside them."""
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
        "decided_by": "axes:" + "/".join(page_axes.sources[:3]) if page_axes else "no_text",
        "needs_review": page_type is None or codeability == axes.NEEDS_REVIEW,
        "bert_model_type": ladder.bert_model_type,
        "bert_confidence": ladder.bert_confidence,
        "keyword_page_subtype": ladder.keyword_page_subtype,
        "keyword_score": ladder.keyword_score,
        "keyword_margin": ladder.keyword_margin,
        "keyword_title_hit": ladder.keyword_title_hit,
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

        trained = bert.trained_classes()
        counts = {"main": 0, "blank_junk": 0, "review": 0, "no_bert": 0, "axes_model": 0, "specialty": 0}
        csv_rows: list[dict[str, Any]] = []
        with connect() as conn:
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
                    prediction = bert.predict(text)
                    keyword = keywords.classify(text, headings=headings)
                    ladder = classify_page(prediction, keyword, trained)
                    page_axes = axes.predict(text, headings)
                    row = _page_row(page_axes, ladder)
                    if page_axes is not None:
                        counts["axes_model"] += int(page_axes.route is not None)
                        counts["specialty"] += int(page_axes.specialty is not None)
                    counts["main"] += 1
                    counts["review"] += int(row["needs_review"])
                    counts["no_bert"] += int(prediction is None)
                    source = _ocr_source_label(final2=f2, final1=f1, prelim=pr)

                upsert_page_classification(conn, chart_id=chart_id, page_id=page_id, **row)
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
                        "keyword_title_hit": "y" if row.get("keyword_title_hit") else "n",
                        "ocr_source": source,
                        **{k: ("" if v is None else v)
                           for k, v in (page_axes.csv_columns() if page_axes else {}).items()},
                    }
                )

        path = write_csv(imaging_csv(ctx.chart_name, "codeable"), CODEABLE_COLS, csv_rows)
        logger.info(
            "page_subtype chart=%s main=%d blank_junk=%d review=%d keywords_only=%d "
            "axes_model=%d specialty=%d → %s",
            ctx.chart_name, counts["main"], counts["blank_junk"], counts["review"],
            counts["no_bert"], counts["axes_model"], counts["specialty"], path,
        )
        return {
            "chart_id": chart_id,
            "codeable_csv": str(path),
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            **counts,
            "bert": bert.status().get("ready", False),
            "page_axes": axes.status().get("ready", False),
        }
