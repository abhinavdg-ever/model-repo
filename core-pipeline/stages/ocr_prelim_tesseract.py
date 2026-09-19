"""Stage: preliminary OCR (Tesseract) → per-page rows + combined text file.

The combined ``<chart>_prelim.txt`` is still written because the review UI's
Local Mode reads it, but downstream stages read text from ``ocr_results``
instead of re-parsing that file once per page.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytesseract
from PIL import Image

from config import STAGE_WORKERS, TESSERACT_CMD, page_image_path
from db import connect, get_ocr_texts, upsert_ocr_result
from db.paths import write_combined_ocr_txt
from stages._support import mark_completed, mark_failed, mark_processing, stage_run

logger = logging.getLogger(__name__)

STAGE = "ocr_prelim"

if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


def _ocr_image(path: Path) -> str:
    with Image.open(path) as img:
        return pytesseract.image_to_string(img) or ""


def _ocr_one(args: tuple[dict[str, Any], Path]) -> tuple[int, str, str, str]:
    """(page_id, page_name, text, error) — runs on a worker thread.

    Tesseract releases the GIL in its C extension, so threads give real
    parallelism here without the memory cost of processes.
    """
    page, image_path = args
    try:
        if not image_path.is_file():
            raise FileNotFoundError(f"Missing page image: {image_path}")
        return page["id"], page["page_name"], _ocr_image(image_path), ""
    except Exception as exc:
        logger.exception("Prelim OCR failed for %s", page["page_name"])
        return page["id"], page["page_name"], "", str(exc)


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, force=force) as ctx:
        todo = ctx.pages_todo

        with connect() as conn:
            for page in todo:
                mark_processing(conn, ctx, page["id"])

        results: list[tuple[int, str, str, str]] = []
        if todo:
            workers = max(1, min(STAGE_WORKERS, len(todo)))
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
                    ocr_type="tesseract",
                    raw_text=text,
                )
                mark_completed(conn, ctx, page_id)

            # Rebuild the combined file from the database so it reflects every
            # page, not just the ones this (possibly resumed) run touched.
            stored = get_ocr_texts(conn, chart_id, "tesseract")

        page_texts = [(p["page_name"], stored.get(p["id"], "")) for p in ctx.pages]
        out = write_combined_ocr_txt(ctx.chart_name, "prelim", page_texts)

        return {
            "chart_id": chart_id,
            "ocr_path": str(out),
            "pages_done": ctx.done,
            "errors": ctx.errors,
        }
