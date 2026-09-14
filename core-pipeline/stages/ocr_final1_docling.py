"""Stage: final OCR 1 (RapidOCR) → ocr_results + combined text file.

Named ``…_docling`` for continuity with the original plan; the engine actually
in use is RapidOCR (``rapidocr-onnxruntime``), matching
the V1 ``rapid_ocr`` prototype. The ``ocr_results.ocr_type`` value
stays ``'docling'`` because that is the slot the review UI reads for "Final
(OSS)".

The engine is constructed once per process. v6 built a fresh ``RapidOCR()``
inside the per-page function, loading the ONNX models for every page.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from config import STAGE_WORKERS, TESSERACT_CMD, page_image_path
from db import (
    connect,
    get_blank_junk_flags,
    get_ocr_texts,
    handwritten_page_ids,
    upsert_ocr_result,
)
from db.paths import write_combined_ocr_txt
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

_engine_lock = threading.Lock()
_engine: Any = None
_engine_ready = False


def _get_engine() -> Any:
    """Build the RapidOCR engine once; None means fall back to Tesseract."""
    global _engine, _engine_ready
    if _engine_ready:
        return _engine
    with _engine_lock:
        if _engine_ready:
            return _engine
        try:
            from rapidocr_onnxruntime import RapidOCR

            _engine = RapidOCR()
        except Exception as exc:
            logger.warning("RapidOCR unavailable (%s); final1 falls back to Tesseract", exc)
            _engine = None
        _engine_ready = True
        return _engine


def _ocr_page(image_path: Path) -> str:
    engine = _get_engine()
    if engine is not None:
        result, _ = engine(str(image_path))
        if not result:
            return ""
        return "\n".join(line[1] for line in result if len(line) > 1)

    import pytesseract
    from PIL import Image

    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    with Image.open(image_path) as img:
        return pytesseract.image_to_string(img) or ""


def _ocr_one(args: tuple[dict[str, Any], Path]) -> tuple[int, str, str, str]:
    page, image_path = args
    try:
        if not image_path.is_file():
            raise FileNotFoundError(f"Missing page image: {image_path}")
        return page["id"], page["page_name"], _ocr_page(image_path), ""
    except Exception as exc:
        logger.exception("Final1 OCR failed for %s", page["page_name"])
        return page["id"], page["page_name"], "", str(exc)


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, force=force) as ctx:
        with connect() as conn:
            bj1 = get_blank_junk_flags(conn, chart_id, pass_no=1)
            hw_ids = handwritten_page_ids(conn, chart_id)

            # Blank/junk/duplicate printed pages are done with. Handwritten
            # pages continue regardless — pass 1 never judged them, so their
            # verdict depends on this OCR.
            drop = [
                pid for pid in ctx.todo
                if pid not in hw_ids and bj1.get(pid, "not_blank_junk") in BJ_EXCLUDE
            ]
            mark_skipped(conn, ctx, drop, "blank_junk_pass1")

            todo = [p for p in ctx.pages if p["id"] in ctx.todo]
            for page in todo:
                mark_processing(conn, ctx, page["id"])

        results: list[tuple[int, str, str, str]] = []
        if todo:
            workers = max(1, min(STAGE_WORKERS, len(todo)))
            _get_engine()  # warm before fan-out
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(
                    pool.map(
                        _ocr_one,
                        [(p, page_image_path(ctx.chart_name, p["page_name"]))
                         for p in todo],
                    )
                )

        with connect() as conn:
            for page_id, page_name, text, error in results:
                if error:
                    mark_failed(conn, ctx, page_id, error, page_name)
                    continue
                upsert_ocr_result(
                    conn,
                    chart_id=chart_id,
                    page_id=page_id,
                    ocr_type="docling",
                    raw_text=text,
                )
                mark_completed(conn, ctx, page_id)
            stored = get_ocr_texts(conn, chart_id, "docling")

        page_texts = [(p["page_name"], stored.get(p["id"], "")) for p in ctx.pages]
        out = write_combined_ocr_txt(ctx.chart_name, "final1", page_texts)

        return {
            "chart_id": chart_id,
            "ocr_path": str(out),
            "engine": "rapidocr" if _get_engine() is not None else "tesseract",
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            "errors": ctx.errors,
        }
