"""Stage: key/value extraction (runs after section headers).

Reads the word boxes Final2 (Azure) stored for each page, runs every extractor over the
chart in one pass (``engine.extract_chart``) and stages what they found
(``staging.py``). Member verification, date of service and page sequencing read
that file and add it to the rules they already run.

Headings are merged into the OCR JSON beside the section-header stage's own list.

A page with no word boxes cannot be extracted. Final2 (Azure) is read first.
When that page was skipped — a high-quality printed page skips the billed call —
Final1 word boxes are used instead. A page with neither is skipped as
``no_word_boxes`` and left out of staging; the stages that read staging treat it as a page
the extraction found nothing on.

Staging is chart-wide on purpose: a value's features include how often it repeats across the
document, so the pages are extracted together, not one by one.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from config import KV_DEBUG, chart_dir, page_image_path
from db import connect, get_ocr_texts, upsert_additional_page_details, upsert_ocr_result
from db.paths import imaging_csv, ocr_dir, write_csv, write_final1_json, write_final2_json
from stages._support import (
    mark_completed,
    mark_processing,
    mark_skipped,
    stage_run,
)

from . import engine, staging
from .provider_name.extract import credentialed_providers
from .heading.extract import heading_fields
from .ocr_input import has_word_boxes, page_for_extraction
from .results import (
    ADDITIONAL_CSV_COLS,
    SIGNATURE_CSV_COLS,
    additional_csv_row,
    additional_fields,
    signature_csv_row,
    signature_fields,
)

logger = logging.getLogger(__name__)

STAGE = "kv_extract"

# Heading detector field ids → the section_headers level the review UI draws (1 = heading).
HEADING_LEVEL = {"Heading": 1, "Subheading": 2}


def _page_json(raw: Optional[str]) -> Optional[dict[str, Any]]:
    """A stored OCR row as a dict, or None for plain text (no boxes in it)."""
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _norm_box(box: list[float], page_w: float, page_h: float) -> Optional[dict[str, float]]:
    """CSS fractions (0–1) of a top-left pixel box, the ``norm`` the section_headers items carry."""
    if page_w <= 0 or page_h <= 0:
        return None

    def clip(value: float) -> float:
        return round(max(0.0, min(1.0, float(value))), 5)

    left, top, right, bottom = box
    return {
        "left": clip(left / page_w),
        "top": clip(top / page_h),
        "width": clip((right - left) / page_w),
        "height": clip((bottom - top) / page_h),
    }


def section_headers_of(staged_page: staging.StagedPage) -> list[dict[str, Any]]:
    """The page's headings in the shape the OCR JSON and the review UI read."""
    page_w = float(staged_page.width or 0)
    page_h = float(staged_page.height or 0)
    headers: list[dict[str, Any]] = []
    for field_id in heading_fields():
        for row in staged_page.selected(field_id):
            box = row.get("box")
            headers.append(
                {
                    "text": row.get("text") or "",
                    "level": HEADING_LEVEL.get(row.get("level") or "", 2),
                    "bbox": list(box) if box else [],
                    "page_width": page_w or None,
                    "page_height": page_h or None,
                    "coord_origin": "TOPLEFT" if box else None,
                    "norm": _norm_box(box, page_w, page_h) if box else None,
                }
            )
    headers.sort(key=_header_top_left)
    return headers


def _header_top_left(item: dict[str, Any]) -> tuple[float, float]:
    box = item.get("bbox")
    if isinstance(box, (list, tuple)) and len(box) >= 2:
        try:
            return (float(box[1]), float(box[0]))
        except (TypeError, ValueError):
            pass
    return (float("inf"), float("inf"))


def _load_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _merge_headers(
    existing: Any, extra: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Keep the section-header stage's headings and add extraction headings it missed."""
    kept = [item for item in existing if isinstance(item, dict)] if isinstance(existing, list) else []
    seen = {(item.get("text") or "").strip().casefold() for item in kept}
    merged = list(kept)
    for item in extra:
        key = (item.get("text") or "").strip().casefold()
        if not key or key in seen:
            continue
        merged.append(item)
        seen.add(key)
    return merged


def _rewrite_ocr_json(
    chart_name: str, kind: str, headers_by_page: dict[str, list[dict[str, Any]]]
) -> bool:
    """Add each page's extraction headings to ``ocr/<chart>_final1.json`` or ``_final2.json``."""
    path = ocr_dir(chart_name) / f"{chart_name}_{kind}.json"
    doc = _load_json(path)
    if doc is None:
        return False
    pages = []
    for page in doc.get("pages") or []:
        if isinstance(page, dict) and str(page.get("fileName") or "") in headers_by_page:
            name = str(page["fileName"])
            page = {
                **page,
                "section_headers": _merge_headers(
                    page.get("section_headers"), headers_by_page[name]
                ),
            }
        pages.append(page)
    if kind == "final1":
        write_final1_json(chart_name, pages, model=str(doc.get("model") or "docling+rapidocr"))
    else:
        write_final2_json(chart_name, pages)
    return True


def _store_headings(
    conn: Any, chart_id: int, page_ids: dict[str, int], headers_by_page: dict[str, list[dict[str, Any]]]
) -> None:
    """Merge the headings into the stored ``ocr_results`` JSON of every engine that read the page."""
    for ocr_type in ("azuredocintel", "docling"):
        stored = get_ocr_texts(conn, chart_id, ocr_type)
        for name, headers in headers_by_page.items():
            page_json = _page_json(stored.get(page_ids[name]))
            if page_json is None:
                continue
            page_json["section_headers"] = _merge_headers(
                page_json.get("section_headers"), headers
            )
            upsert_ocr_result(
                conn,
                chart_id=chart_id,
                page_id=page_ids[name],
                ocr_type=ocr_type,
                raw_text=json.dumps(page_json, default=str),
            )


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, force=force) as ctx:
        if not ctx.todo:
            return {"chart_id": chart_id, "pages_done": 0, "skipped": ctx.skipped, "reason": "nothing_to_do"}

        ready = engine.readiness()
        if not ready.get("ready"):
            reason = str(ready.get("reason") or "extraction_not_ready")
            logger.warning("kv_extract skipped for chart %s: %s", ctx.chart_name, reason)
            with connect() as conn:
                mark_skipped(conn, ctx, list(ctx.todo), "extraction_not_ready")
            return {
                "chart_id": chart_id,
                "ready": False,
                "reason": reason,
                "pages_done": 0,
                "skipped": ctx.skipped,
            }

        with connect() as conn:
            final2 = get_ocr_texts(conn, chart_id, "azuredocintel")
            final1 = get_ocr_texts(conn, chart_id, "docling")

        # The whole chart is extracted together, whichever pages are still to do.
        pages: list[dict[str, Any]] = []
        no_boxes: list[int] = []
        from_final1 = 0
        for page in ctx.pages:
            image = page_image_path(ctx.chart_name, page["page_name"])
            extractable = page_for_extraction(
                _page_json(final2.get(page["id"])),
                _page_json(final1.get(page["id"])),
                page_name=page["page_name"],
                page_number=page.get("page_number"),
                image_path=image,
            )
            if extractable is None:
                no_boxes.append(page["id"])
                continue
            if not has_word_boxes(_page_json(final2.get(page["id"]))):
                from_final1 += 1
            pages.append(extractable)

        with connect() as conn:
            mark_skipped(conn, ctx, no_boxes, "no_word_boxes")
            for page in ctx.pages_todo:
                mark_processing(conn, ctx, page["id"])
        if from_final1:
            logger.info(
                "chart %s: %d page(s) extracted from Final1 word boxes (no Final2 read)",
                ctx.chart_name, from_final1,
            )
        if no_boxes:
            logger.warning(
                "chart %s: %d of %d page(s) have no word boxes in Final2 or Final1; "
                "nothing is extracted from them",
                ctx.chart_name, len(no_boxes), len(ctx.pages),
            )

        version = engine.model_version()
        result = engine.extract_chart(ctx.chart_name, pages) if pages else None
        payload = staging.build(ctx.chart_name, result, version) if result else {
            "version": staging.SCHEMA_VERSION,
            "chart_name": ctx.chart_name,
            "model_version": version,
            "pages": {},
        }
        path = staging.write(ctx.chart_name, payload)
        debug_path = write_kv_debug(ctx.chart_name, payload)
        staged = staging.read(ctx.chart_name)

        # Headings go where the section-header stage wrote them.
        page_ids = {page["page_name"]: page["id"] for page in ctx.pages}
        headers_by_page = {
            staged_page.name: section_headers_of(staged_page)
            for staged_page in staged or []
            if staged_page.name in page_ids
        }
        signature_rows: list[dict[str, Any]] = []
        additional_rows: list[dict[str, Any]] = []
        finishing = {item["id"] for item in ctx.pages_todo}
        with connect() as conn:
            _store_headings(conn, chart_id, page_ids, headers_by_page)
            for page in ctx.pages:
                staged_page = staged.page(page["page_name"]) if staged else None
                signature = _selected(staged_page, "electronic_signature")
                providers = _provider_rows(staged_page)
                page_no = _chosen(staged_page, "page_no")
                headers = headers_by_page.get(page["page_name"]) or []
                sig = signature_fields(signature, providers)
                extra = additional_fields(page_no, headers)
                upsert_additional_page_details(
                    conn, chart_id=chart_id, page_id=page["id"], fields=extra
                )
                signature_rows.append(
                    signature_csv_row(
                        ctx.chart_name, page["page_name"], page.get("page_number"), sig
                    )
                )
                additional_rows.append(
                    additional_csv_row(
                        ctx.chart_name, page["page_name"], page.get("page_number"), extra
                    )
                )
                if page["id"] in finishing:
                    mark_completed(conn, ctx, page["id"])
        signature_csv = write_csv(
            imaging_csv(ctx.chart_name, "provider_signature"),
            SIGNATURE_CSV_COLS,
            signature_rows,
        )
        additional_csv = write_csv(
            imaging_csv(ctx.chart_name, "additional_page_details"),
            ADDITIONAL_CSV_COLS,
            additional_rows,
        )
        for kind in ("final1", "final2"):
            _rewrite_ocr_json(ctx.chart_name, kind, headers_by_page)

        headings = sum(len(headers) for headers in headers_by_page.values())
        logger.info(
            "Key/value extraction: %d page(s) staged, %d heading(s) (model %s)",
            len(headers_by_page), headings, version,
        )
        return {
            "chart_id": chart_id,
            "staging": str(path),
            "model_version": version,
            "pages_done": ctx.done,
            "pages_extracted": len(pages),
            "pages_no_word_boxes": len(no_boxes),
            "headings": headings,
            "provider_signature_csv": str(signature_csv),
            "additional_page_details_csv": str(additional_csv),
            "kv_debug": str(debug_path) if debug_path else "",
            "skipped": ctx.skipped,
        }


def write_kv_debug(chart_name: str, payload: dict[str, Any]) -> Optional[Path]:
    """Raw extractor rows for a check. Written only when KV_DEBUG is on."""
    if not KV_DEBUG:
        return None
    path = chart_dir(chart_name) / "debug" / f"{chart_name}_kv.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Key/value debug: %s", path)
    return path


def _selected(staged_page: Optional[staging.StagedPage], field_id: str) -> list[dict[str, Any]]:
    if staged_page is None:
        return []
    return staged_page.selected(field_id)


def _provider_rows(staged_page: Optional[staging.StagedPage]) -> list[dict[str, Any]]:
    """Every accepted provider with Dr. or a credential, or next to Provider, in page order.

    The trained model keeps a single winner. A page can still name a provider
    and a cosigner, so the output keeps each of them.
    """
    if staged_page is None:
        return []
    return credentialed_providers(staged_page.accepted("provider_name"))


def _chosen(staged_page: Optional[staging.StagedPage], field_id: str) -> Optional[dict[str, Any]]:
    chosen = _selected(staged_page, field_id)
    return chosen[0] if chosen else None


def ensure_staging(chart_id: int, chart_name: str) -> staging.Staged:
    """The chart's staging, running the extraction first when there is none.

    Re-running one later stage on its own (``only=["member_verify"]``) finds no
    file yet. The extraction reads stored OCR, so it is run again. When the
    weights are not installed, an empty staging is returned and the caller
    keeps its own rules.
    """
    staged = staging.read(chart_name)
    if staged is not None:
        return staged
    if not engine.readiness().get("ready"):
        logger.info("chart %s: extraction weights are not ready; later stages use their own rules", chart_name)
        return staging.Staged({"version": staging.SCHEMA_VERSION, "pages": {}, "model_version": ""})
    logger.info("chart %s: no extraction staging; running the extraction first", chart_name)
    run(chart_id, force=True)
    staged = staging.read(chart_name)
    if staged is None:
        return staging.Staged({"version": staging.SCHEMA_VERSION, "pages": {}, "model_version": ""})
    return staged
