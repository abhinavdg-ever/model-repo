"""Stage: page classification → ``page_classification`` (the Extracted answer).

Two predictors run on every page that is not blank, junk or duplicate, on its
own, from the page's text (Final2 → Final1 → Tesseract, plain text):

* BERT (``bert.py``) predicts a ``model_type`` with a confidence.
* The keyword canon (``keywords.py``) predicts a ``page_subtype`` with a score,
  a margin and whether a title term hit.

``arbitration.classify_page`` decides the page type (the ladder), the sub-type
and the codability. That per-page result is the **Extracted** answer and is
what this stage writes. The continuation rules that can change it (an untitled
page continuing another document; lab or radiology inside a Progress Note)
need the continuity stage's documents and are applied by ``imaging_final`` as
the **Final** answer.

Blank / junk / duplicate pages are ``non_codeable`` and keep their junk label
as the sub-type; a label that is a taxonomy sub-type (Invoice) also gives its
page type.

Runs before ``dos_extract``, which reads its sub-type to decide default-date
and non-encounter pages; it reads no DOS itself.

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
from stages.lib.page_classify import bert, keywords, taxonomy
from stages.lib.page_classify.arbitration import classify_page

logger = logging.getLogger(__name__)

STAGE = "page_subtype"

_DISPLAY = {"codeable": "Codeable", "non_codeable": "Non-Codeable", "discharge_summary": "Discharge"}

CODEABLE_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "page_type",
    "page_subtype",
    "model_type",
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


def _bj_label(flag: str, junk_subtype: Optional[str]) -> str:
    if flag == "blank":
        return "Blank"
    if flag == "duplicate":
        return "Duplicate"
    return (junk_subtype or "Others").strip() or "Others"


def _blank_junk_row(flag: str, junk_subtype: Optional[str], confidence: Any) -> dict[str, Any]:
    label = _bj_label(flag, junk_subtype)
    names = taxonomy.load()
    page_type = names.page_type_of_subtype(label)
    return {
        "page_type": page_type,
        "page_subtype": label,
        "model_type": names.model_type(page_type, label) if page_type else None,
        "classification_category": "non_codeable",
        "confidence": float(confidence) if confidence is not None else None,
        "duplicate_flag": flag == "duplicate",
        "decided_by": "blank_junk",
        "needs_review": False,
    }


def _page_row(result: Any) -> dict[str, Any]:
    names = taxonomy.load()
    return {
        "page_type": result.page_type,
        "page_subtype": result.page_subtype,
        "model_type": result.model_type,
        # No page type means no prediction: not_sure has no CHECK value, so
        # the row is written as non_codeable and needs_review says why.
        "classification_category": names.category(result.page_type) or "non_codeable",
        "confidence": result.bert_confidence,
        "duplicate_flag": False,
        "decided_by": result.decided_by,
        "needs_review": result.needs_review,
        "bert_model_type": result.bert_model_type,
        "bert_confidence": result.bert_confidence,
        "keyword_page_subtype": result.keyword_page_subtype,
        "keyword_score": result.keyword_score,
        "keyword_margin": result.keyword_margin,
        "keyword_title_hit": result.keyword_title_hit,
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
        counts = {"main": 0, "blank_junk": 0, "review": 0, "no_bert": 0}
        csv_rows: list[dict[str, Any]] = []
        with connect() as conn:
            for page in ctx.pages:
                page_id = page["id"]
                bj = bj_rows.get(page_id) or {}
                flag = bj.get("blank_junk_flag") or "not_blank_junk"
                f2, f1, pr = final2.get(page_id), final1.get(page_id), prelim.get(page_id)
                if flag in BJ_EXCLUDE:
                    row = _blank_junk_row(flag, bj.get("junk_subtype"), bj.get("confidence"))
                    counts["blank_junk"] += 1
                    source = ""
                else:
                    text = best_page_text(final2=f2, final1=f1, prelim=pr, quality_row=quality.get(page_id))
                    prediction = bert.predict(text)
                    result = classify_page(prediction, keywords.classify(text), trained)
                    row = _page_row(result)
                    counts["main"] += 1
                    counts["review"] += int(result.needs_review)
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
                        "codability": taxonomy.load().codability(row["page_type"]) or "",
                        "tag": category,
                        "is_codeable": _DISPLAY.get(category, ""),
                        "needs_review": "y" if row["needs_review"] else "n",
                        "keyword_title_hit": "y" if row.get("keyword_title_hit") else "n",
                        "ocr_source": source,
                    }
                )

        path = write_csv(imaging_csv(ctx.chart_name, "codeable"), CODEABLE_COLS, csv_rows)
        logger.info(
            "page_subtype chart=%s main=%d blank_junk=%d review=%d keywords_only=%d → %s",
            ctx.chart_name, counts["main"], counts["blank_junk"], counts["review"],
            counts["no_bert"], path,
        )
        return {
            "chart_id": chart_id,
            "codeable_csv": str(path),
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            **counts,
            "bert": bert.status().get("ready", False),
        }
