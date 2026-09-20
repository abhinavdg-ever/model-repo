"""Stage: final OCR 1 (Docling layout + RapidOCR) → ocr_results + JSON file.

Preferred engine is Docling with local RapidOCR ``.pth`` models (layout,
TableFormer, reading order) — the V1 ``os_ocr.py`` path.

When the primary Docling pass (env cell-matching, usually on) times out,
returns sparse/empty markdown, or raises, final1:

1. Runs a second Docling pass with ``cell_matching=false`` for section headers.
2. Takes body ``content`` / ``markdown`` from RapidOCR-onnxruntime.
3. Stores the same page JSON shape (``engine=docling-headers+rapidocr-onnx``).

If that header pass also fails, RapidOCR-onnx alone is used
(``engine=rapidocr-onnx``, empty ``section_headers``). Tesseract is already
stage 2 / prelim — it is not repeated here.

``ocr_results.ocr_type`` stays ``'docling'`` — that is the slot the review UI
labels "Final (OSS)".
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from config import (
    DOCLING_PAGE_TIMEOUT_SECONDS,
    DOCLING_WORKERS,
    STAGE_WORKERS,
    page_image_path,
)
from db import (
    connect,
    get_blank_junk_flags,
    get_ocr_texts,
    low_quality_page_ids,
    non_printed_page_ids,
    upsert_ocr_result,
)
from db.paths import write_final1_json
from stages._support import (
    BJ_EXCLUDE,
    mark_completed,
    mark_failed,
    mark_processing,
    mark_skipped,
    stage_run,
)

logger = logging.getLogger(__name__)

STAGE = "ocr_final1"
HYBRID_ENGINE = "docling-headers+rapidocr-onnx"

_engine_lock = threading.Lock()
_onnx_engine: Any = None
_onnx_ready = False


def _get_onnx_engine() -> Any:
    """Fallback RapidOCR (onnxruntime) when Docling is unavailable."""
    global _onnx_engine, _onnx_ready
    if _onnx_ready:
        return _onnx_engine
    with _engine_lock:
        if _onnx_ready:
            return _onnx_engine
        try:
            from rapidocr_onnxruntime import RapidOCR

            _onnx_engine = RapidOCR()
        except Exception as exc:
            logger.warning("RapidOCR-onnx unavailable (%s)", exc)
            _onnx_engine = None
        _onnx_ready = True
        return _onnx_engine


def _ocr_onnx(image_path: Path) -> str:
    engine = _get_onnx_engine()
    if engine is None:
        raise RuntimeError(
            "RapidOCR-onnxruntime is not installed; final1 has no Tesseract "
            "fallback (prelim already uses Tesseract). "
            "Install rapidocr-onnxruntime or enable Docling + .pth models."
        )
    result, _ = engine(str(image_path))
    if not result:
        return ""
    return "\n".join(line[1] for line in result if len(line) > 1)


def _hybrid_headers_and_rapid(
    image_path: Path,
    *,
    page_name: str,
    reason: str,
) -> dict[str, Any]:
    """Fast Docling (no cell matching) for headers + RapidOCR-onnx for body."""
    from stages.lib.imaging.docling_ocr import (
        convert_image_with_timeout,
        get_converter,
    )

    headers: list[dict[str, Any]] = []
    fast = get_converter(cell_matching=False)
    if fast is not None:
        try:
            extracted = convert_image_with_timeout(
                image_path,
                converter=fast,
                timeout_seconds=DOCLING_PAGE_TIMEOUT_SECONDS,
            )
            headers = list(extracted.get("section_headers") or [])
            logger.info(
                "Final1 hybrid headers for %s (%s): %d header(s)",
                page_name,
                reason,
                len(headers),
            )
        except Exception as exc:
            logger.warning(
                "Final1 hybrid Docling headers failed for %s (%s) — Rapid only",
                page_name,
                exc,
            )
    else:
        logger.warning(
            "Final1 hybrid: no cell_matching=false converter — Rapid only for %s",
            page_name,
        )

    text = _ocr_onnx(image_path)
    if headers:
        return {
            "content": text,
            "markdown": text,
            "document": None,
            "section_headers": headers,
            "engine": HYBRID_ENGINE,
        }
    return {
        "content": text,
        "markdown": text,
        "document": None,
        "section_headers": [],
        "engine": "rapidocr-onnx",
    }


def _ocr_one(args: tuple[dict[str, Any], Path, bool]) -> dict[str, Any]:
    page, image_path, prefer_docling = args
    out: dict[str, Any] = {
        "page_id": page["id"],
        "page_name": page["page_name"],
        "page_number": page.get("page_number"),
        "content": "",
        "markdown": "",
        "document": None,
        "section_headers": [],
        "engine": "unknown",
        "error": "",
    }
    try:
        if not image_path.is_file():
            raise FileNotFoundError(f"Missing page image: {image_path}")

        if prefer_docling:
            from stages.lib.imaging.docling_ocr import (
                convert_image_with_timeout,
                get_converter,
                markdown_is_sparse,
            )

            converter = get_converter()
            if converter is not None:
                fallback_reason: Optional[str] = None
                try:
                    extracted = convert_image_with_timeout(
                        image_path,
                        converter=converter,
                        timeout_seconds=DOCLING_PAGE_TIMEOUT_SECONDS,
                    )
                    content = extracted.get("content") or ""
                    elapsed = extracted.get("elapsed_seconds")
                    if not markdown_is_sparse(content):
                        out["content"] = content
                        out["markdown"] = extracted.get("markdown") or content
                        out["document"] = extracted.get("document")
                        out["section_headers"] = (
                            extracted.get("section_headers") or []
                        )
                        out["engine"] = "docling+rapidocr"
                        if elapsed is not None:
                            logger.info(
                                "Final1 Docling ok %s in %.1fs",
                                page["page_name"],
                                elapsed,
                            )
                        return out
                    fallback_reason = (
                        f"sparse/empty ({elapsed or 0.0:.1f}s, chars={len(content)})"
                    )
                    logger.warning(
                        "Docling sparse/empty for %s (%.1fs, chars=%d) — "
                        "hybrid headers + RapidOCR-onnx",
                        page["page_name"],
                        elapsed or 0.0,
                        len(content or ""),
                    )
                except TimeoutError as exc:
                    fallback_reason = "timeout"
                    logger.warning("%s — hybrid headers + RapidOCR-onnx", exc)
                except Exception as exc:
                    fallback_reason = f"{type(exc).__name__}: {exc}"
                    logger.warning(
                        "Docling failed for %s (%s) — hybrid headers + RapidOCR-onnx",
                        page["page_name"],
                        exc,
                    )

                if fallback_reason is not None:
                    merged = _hybrid_headers_and_rapid(
                        image_path,
                        page_name=page["page_name"],
                        reason=fallback_reason,
                    )
                    out.update(merged)
                    return out

        text = _ocr_onnx(image_path)
        out["content"] = text
        out["markdown"] = text
        out["engine"] = "rapidocr-onnx"
        return out
    except Exception as exc:
        logger.exception("Final1 OCR failed for %s", page["page_name"])
        out["error"] = str(exc)
        return out


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    # Docling is used automatically when the package + RapidOCR .pth models are
    # present — no enable flag. Otherwise RapidOCR-onnx only.
    prefer_docling = False
    engine_label = "rapidocr-onnx"
    try:
        from stages.lib.imaging.docling_ocr import converter_reason, get_converter

        if get_converter() is not None:
            prefer_docling = True
            engine_label = "docling+rapidocr"
            # Warm the fast (no cell-matching) converter for hybrid fallback.
            get_converter(cell_matching=False)
        else:
            logger.info(
                "Docling final1 not ready (%s); using RapidOCR-onnx. "
                "Output is still %%s_final1.json.",
                converter_reason() or "unavailable",
            )
    except Exception as exc:
        logger.info("Docling final1 unavailable (%s); using RapidOCR-onnx", exc)

    with stage_run(chart_id, STAGE, force=force) as ctx:
        with connect() as conn:
            bj1 = get_blank_junk_flags(conn, chart_id, pass_no=1)
            pass1_skippers = non_printed_page_ids(conn, chart_id) | low_quality_page_ids(
                conn, chart_id
            )

            # Blank/junk/duplicate printed pages are done with. Handwritten /
            # low-quality pages continue — pass 1 never judged them.
            drop = [
                pid for pid in ctx.todo
                if pid not in pass1_skippers
                and bj1.get(pid, "not_blank_junk") in BJ_EXCLUDE
            ]
            bj_skipped = set(drop)
            mark_skipped(conn, ctx, drop, "blank_junk_pass1")

            todo = [p for p in ctx.pages if p["id"] in ctx.todo]
            for page in todo:
                mark_processing(conn, ctx, page["id"])

        results: list[dict[str, Any]] = []
        if todo:
            # Docling is heavy; still fan out, but keep the shared converter.
            if prefer_docling:
                from stages.lib.imaging.docling_ocr import get_converter

                get_converter()
            else:
                _get_onnx_engine()
            workers = max(1, min(STAGE_WORKERS, len(todo)))
            if prefer_docling:
                # Torch RapidOCR + Docling thrash under fan-out; serial by default.
                workers = max(1, min(DOCLING_WORKERS, workers))
                logger.info(
                    "Final1 Docling workers=%d timeout=%.0fs",
                    workers,
                    DOCLING_PAGE_TIMEOUT_SECONDS,
                )
            else:
                logger.info("Final1 RapidOCR-onnx workers=%d", workers)
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="page") as pool:
                results = list(
                    pool.map(
                        _ocr_one,
                        [
                            (
                                p,
                                page_image_path(ctx.chart_name, p["page_name"]),
                                prefer_docling,
                            )
                            for p in todo
                        ],
                    )
                )

        with connect() as conn:
            for item in results:
                if item.get("error"):
                    mark_failed(
                        conn, ctx, item["page_id"], item["error"], item["page_name"]
                    )
                    continue
                page_doc = {
                    "pageNumber": item.get("page_number"),
                    "fileName": item["page_name"],
                    "content": item.get("content") or "",
                    "markdown": item.get("markdown") or item.get("content") or "",
                    "engine": item.get("engine"),
                    "section_headers": item.get("section_headers") or [],
                }
                if item.get("document") is not None:
                    page_doc["document"] = item["document"]
                upsert_ocr_result(
                    conn,
                    chart_id=chart_id,
                    page_id=item["page_id"],
                    ocr_type="docling",
                    raw_text=json.dumps(page_doc, default=str),
                )
                mark_completed(conn, ctx, item["page_id"])
                if item.get("engine"):
                    engine_label = str(item["engine"])
            stored = get_ocr_texts(conn, chart_id, "docling")

        out_pages: list[dict[str, Any]] = []
        for page in ctx.pages:
            raw = stored.get(page["id"])
            page_doc: dict[str, Any] = {
                "pageNumber": page.get("page_number"),
                "fileName": page["page_name"],
                "content": "",
                "markdown": "",
                "section_headers": [],
            }
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        page_doc.update(parsed)
                        page_doc["fileName"] = page["page_name"]
                        page_doc["pageNumber"] = page.get("page_number")
                    else:
                        page_doc["content"] = str(parsed)
                        page_doc["markdown"] = page_doc["content"]
                except (ValueError, TypeError):
                    page_doc["content"] = raw
                    page_doc["markdown"] = raw
            if page["id"] in bj_skipped and not str(page_doc.get("content") or "").strip():
                page_doc["skippedReason"] = "blank_junk_pass1"
                page_doc["section_headers"] = []
            out_pages.append(page_doc)

        out = write_final1_json(ctx.chart_name, out_pages, model=engine_label)
        # Remove a stale .txt from older runs so Local Mode prefers the JSON.
        legacy_txt = out.with_suffix(".txt")
        if legacy_txt.is_file():
            try:
                legacy_txt.unlink()
            except OSError:
                pass

        return {
            "chart_id": chart_id,
            "ocr_path": str(out),
            "engine": engine_label,
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            "errors": ctx.errors,
        }
