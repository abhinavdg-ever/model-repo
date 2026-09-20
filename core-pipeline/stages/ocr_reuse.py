"""Reuse on-disk ``ocr/`` artifacts (or DB ``ocr_results``) when ``SKIP_OCR`` is set.

When ``skip_ocr`` is true (or ``SKIP_OCR=true``):

  1. If the chart's ``ocr/`` folder already has usable files → skip OCR engines
     and re-hydrate ``ocr_results`` from disk.
  2. Else if ``ocr_results`` already has rows → skip OCR engines and **write**
     the three folder files (``*_prelim.txt``, ``*_final1.json``,
     ``*_final2.json``) from the DB.
  3. Else run OCR normally.

``force=True`` always runs OCR.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from db import connect, get_ocr_texts, list_pages, set_pages_stage, upsert_ocr_result
from db.paths import (
    ocr_dir,
    parse_combined_ocr_txt,
    parse_ocr_json,
    write_combined_ocr_txt,
    write_final1_json,
    write_final2_json,
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


def ocr_results_present(chart_id: int) -> bool:
    """True when ``ocr_results`` has at least one non-empty row for this chart."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT 1 AS ok
              FROM ocr_results
             WHERE chart_id = %s
               AND COALESCE(char_count, 0) > 0
             LIMIT 1
            """,
            (chart_id,),
        ).fetchone()
    return bool(row)


def _page_doc_from_raw(
    raw: str,
    *,
    page_name: str,
    page_number: Any,
    ocr_type: str,
) -> dict[str, Any]:
    """Rebuild a final1/final2 page object from a stored ``raw_text`` cell."""
    base: dict[str, Any] = {
        "pageNumber": page_number,
        "fileName": page_name,
        "content": "",
    }
    if not raw:
        return base
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        base["content"] = raw
        if ocr_type == "docling":
            base["markdown"] = raw
        return base
    if isinstance(parsed, dict):
        base.update(parsed)
        base["fileName"] = page_name
        base["pageNumber"] = page_number
        if "content" not in base and "markdown" in base:
            base["content"] = base.get("markdown") or ""
        return base
    base["content"] = str(parsed)
    if ocr_type == "docling":
        base["markdown"] = base["content"]
    return base


def materialize_ocr_from_db(chart_id: int, chart_name: str) -> dict[str, Any]:
    """Write the three ``ocr/`` files from ``ocr_results`` and mark stages done."""
    summary: dict[str, Any] = {
        "chart_id": chart_id,
        "chart_name": chart_name,
        "source": "ocr_results",
        "written": {},
        "stages_marked": [],
    }
    with connect() as conn:
        pages = list_pages(conn, chart_id)
        page_ids = [p["id"] for p in pages]
        loaded_kinds: list[str] = []

        prelim = get_ocr_texts(conn, chart_id, "tesseract")
        if prelim:
            page_texts = [
                (p["page_name"], prelim.get(p["id"], ""))
                for p in pages
                if p["id"] in prelim
            ]
            if page_texts:
                path = write_combined_ocr_txt(chart_name, "prelim", page_texts)
                summary["written"]["prelim"] = str(path)
                loaded_kinds.append("ocr_prelim")

        final1 = get_ocr_texts(conn, chart_id, "docling")
        if final1:
            out_pages = [
                _page_doc_from_raw(
                    final1.get(p["id"], ""),
                    page_name=p["page_name"],
                    page_number=p.get("page_number"),
                    ocr_type="docling",
                )
                for p in pages
                if p["id"] in final1
            ]
            if out_pages:
                path = write_final1_json(chart_name, out_pages, model="skip_ocr_from_db")
                summary["written"]["final1"] = str(path)
                loaded_kinds.append("ocr_final1")

        final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
        if final2:
            out_pages = [
                _page_doc_from_raw(
                    final2.get(p["id"], ""),
                    page_name=p["page_name"],
                    page_number=p.get("page_number"),
                    ocr_type="azuredocintel",
                )
                for p in pages
                if p["id"] in final2
            ]
            if out_pages:
                path = write_final2_json(chart_name, out_pages)
                summary["written"]["final2"] = str(path)
                loaded_kinds.append("ocr_final2")

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
        "SKIP_OCR: materialized chart %s ocr/ from DB — %s",
        chart_name,
        list(summary["written"]),
    )
    return summary


def hydrate_ocr_from_disk(chart_id: int, chart_name: str) -> dict[str, Any]:
    """Load existing ocr/ files into ``ocr_results`` and mark OCR stages done."""
    root = ocr_dir(chart_name)
    summary: dict[str, Any] = {
        "chart_id": chart_id,
        "chart_name": chart_name,
        "source": "disk",
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


def apply_skip_ocr(chart_id: int, chart_name: str) -> dict[str, Any]:
    """Disk first, then DB→folder. Caller already decided skip is active."""
    if ocr_artifacts_present(chart_name):
        return hydrate_ocr_from_disk(chart_id, chart_name)
    return materialize_ocr_from_db(chart_id, chart_name)


def should_skip_ocr_stages(
    *,
    chart_name: str,
    chart_id: Optional[int] = None,
    force: bool,
    skip_ocr: Optional[bool] = None,
) -> bool:
    """Whether the orchestrator should bypass OCR engines for this chart.

    ``skip_ocr`` is the per-request override (API/CLI). ``None`` falls back to
    the ``SKIP_OCR`` env flag. ``force=True`` always runs OCR.
    """
    from config import SKIP_OCR

    enabled = SKIP_OCR if skip_ocr is None else bool(skip_ocr)
    if force or not enabled:
        return False
    if ocr_artifacts_present(chart_name):
        return True
    if chart_id is not None and ocr_results_present(chart_id):
        return True
    logger.info(
        "skip_ocr requested but no usable ocr/ files or ocr_results for %s — "
        "running OCR",
        chart_name,
    )
    return False
