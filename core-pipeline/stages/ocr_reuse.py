"""Reuse on-disk ``ocr/`` artifacts when ``SKIP_OCR`` is set.

Downstream stages read ``ocr_results`` in Postgres, so skipping the OCR stages
also re-hydrates the DB from ``*_prelim.txt`` / ``*_final1.json`` /
``*_final2.json`` when those files exist.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from db import connect, list_pages, set_pages_stage, upsert_ocr_result
from db.paths import (
    ocr_dir,
    parse_combined_ocr_txt,
    parse_ocr_json,
)

logger = logging.getLogger(__name__)

OCR_STAGE_NAMES = ("ocr_prelim", "ocr_final1", "ocr_final2")


def ocr_artifacts_present(chart_name: str) -> bool:
    """True when the chart's ``ocr/`` folder has at least one usable OCR file."""
    root = ocr_dir(chart_name)
    if not root.is_dir():
        return False
    prelim = root / f"{chart_name}_prelim.txt"
    final1 = root / f"{chart_name}_final1.json"
    final2 = root / f"{chart_name}_final2.json"
    if prelim.is_file() and prelim.stat().st_size > 0:
        return True
    for path in (final1, final2):
        if not path.is_file() or path.stat().st_size == 0:
            continue
        try:
            if parse_ocr_json(path):
                return True
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    return False


def hydrate_ocr_from_disk(chart_id: int, chart_name: str) -> dict[str, Any]:
    """Load existing ocr/ files into ``ocr_results`` and mark OCR stages done."""
    root = ocr_dir(chart_name)
    summary: dict[str, Any] = {
        "chart_id": chart_id,
        "chart_name": chart_name,
        "loaded": {},
        "stages_marked": [],
    }
    with connect() as conn:
        pages = list_pages(conn, chart_id)
        by_name = {p["page_name"]: p["id"] for p in pages}
        page_ids = [p["id"] for p in pages]

        loaded_kinds: list[str] = []

        prelim_path = root / f"{chart_name}_prelim.txt"
        if prelim_path.is_file() and prelim_path.stat().st_size > 0:
            texts = parse_combined_ocr_txt(prelim_path)
            count = 0
            for name, text in texts.items():
                page_id = by_name.get(name)
                if page_id is None:
                    continue
                upsert_ocr_result(
                    conn,
                    chart_id=chart_id,
                    page_id=page_id,
                    ocr_type="tesseract",
                    raw_text=text,
                )
                count += 1
            summary["loaded"]["tesseract"] = count
            loaded_kinds.append("ocr_prelim")

        for kind, ocr_type, filename in (
            ("ocr_final1", "docling", f"{chart_name}_final1.json"),
            ("ocr_final2", "azuredocintel", f"{chart_name}_final2.json"),
        ):
            path = root / filename
            if not path.is_file() or path.stat().st_size == 0:
                continue
            try:
                texts = parse_ocr_json(path)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                logger.warning("SKIP_OCR: could not parse %s (%s)", path.name, exc)
                continue
            count = 0
            for name, text in texts.items():
                page_id = by_name.get(name)
                if page_id is None:
                    continue
                # Store final JSON rows as the stage would (content envelope).
                if ocr_type == "tesseract":
                    raw = text
                else:
                    page = next((p for p in pages if p["page_name"] == name), None)
                    raw = json.dumps(
                        {
                            "pageNumber": page.get("page_number") if page else None,
                            "fileName": name,
                            "content": text,
                            "markdown": text,
                            "engine": "skip_ocr_reuse",
                        }
                    )
                upsert_ocr_result(
                    conn,
                    chart_id=chart_id,
                    page_id=page_id,
                    ocr_type=ocr_type,
                    raw_text=raw,
                )
                count += 1
            summary["loaded"][ocr_type] = count
            if count:
                loaded_kinds.append(kind)

        # Mark every OCR stage completed so chart progress advances past them.
        for stage_name in OCR_STAGE_NAMES:
            if not page_ids:
                continue
            set_pages_stage(
                conn,
                chart_id=chart_id,
                page_ids=page_ids,
                stage_name=stage_name,
                pass_no=1,
                status="completed" if stage_name in loaded_kinds else "skipped",
                skip_reason=(
                    None
                    if stage_name in loaded_kinds
                    else "skip_ocr_no_artifact"
                ),
            )
            summary["stages_marked"].append(stage_name)

    logger.info(
        "SKIP_OCR: hydrated chart %s from disk — %s",
        chart_name,
        summary["loaded"],
    )
    return summary


def should_skip_ocr_stages(*, chart_name: str, force: bool) -> bool:
    """Whether the orchestrator should bypass OCR engines for this chart."""
    from config import SKIP_OCR

    if force or not SKIP_OCR:
        return False
    if ocr_artifacts_present(chart_name):
        return True
    logger.info(
        "SKIP_OCR=true but no usable files under ocr/%s/ — running OCR",
        chart_name,
    )
    return False
