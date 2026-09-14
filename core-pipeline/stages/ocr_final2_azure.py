"""Stage: final OCR 2 (Azure Document Intelligence, prebuilt-read).

This is the only stage that costs money per page, so it is the one that most
needs the resume behaviour: a page already ``completed`` is never re-sent.

One ``DocumentIntelligenceClient`` is built per process and shared; v6 built a
new client (and a new TLS handshake) for every page.
"""
from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from config import (
    AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT,
    AZURE_DOCUMENT_INTELLIGENCE_KEY,
    AZURE_POLL_TIMEOUT_SECONDS,
    STAGE_WORKERS,
    page_image_path,
)
from db import (
    connect,
    get_blank_junk_flags,
    get_ocr_texts,
    handwritten_page_ids,
    upsert_ocr_result,
)
from db.paths import write_final2_json
from stages._support import (
    BJ_EXCLUDE,
    mark_completed,
    mark_failed,
    mark_processing,
    mark_skipped,
    stage_run,
)

logger = logging.getLogger(__name__)

STAGE = "ocr_final2"

_client_lock = threading.Lock()
_client: Any = None


def _azure_configured() -> bool:
    return bool(
        AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and AZURE_DOCUMENT_INTELLIGENCE_KEY
    )


def _get_client() -> Any:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.core.credentials import AzureKeyCredential

            _client = DocumentIntelligenceClient(
                endpoint=AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT,
                credential=AzureKeyCredential(AZURE_DOCUMENT_INTELLIGENCE_KEY),
            )
        return _client


def _ocr_azure(image_path: Path) -> dict[str, Any]:
    client = _get_client()
    with image_path.open("rb") as fh:
        poller = client.begin_analyze_document(
            "prebuilt-read", body=fh, content_type="application/octet-stream"
        )
    result = poller.result(timeout=AZURE_POLL_TIMEOUT_SECONDS)
    pages_meta = [
        {
            "pageNumber": getattr(p, "page_number", None),
            "angle": getattr(p, "angle", None),
            "width": getattr(p, "width", None),
            "height": getattr(p, "height", None),
            "unit": getattr(p, "unit", None),
        }
        for p in (getattr(result, "pages", None) or [])
    ]
    return {
        "content": getattr(result, "content", None) or "",
        "pages_meta": pages_meta,
    }


def _ocr_one(args: tuple[dict[str, Any], Path, bool]) -> dict[str, Any]:
    page, image_path, use_azure = args
    out: dict[str, Any] = {
        "page_id": page["id"],
        "page_name": page["page_name"],
        "page_number": page.get("page_number"),
        "content": "",
        "pages_meta": [],
        "error": "",
        "engine": "azure" if use_azure else "fallback",
    }
    try:
        if use_azure and image_path.is_file():
            extracted = _ocr_azure(image_path)
            out["content"] = extracted["content"]
            out["pages_meta"] = extracted["pages_meta"]
        else:
            out["content"] = ""
    except Exception as exc:
        logger.exception("Final2 OCR failed for %s", page["page_name"])
        out["error"] = str(exc)
    return out


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    use_azure = _azure_configured()
    if not use_azure:
        logger.warning(
            "Azure Document Intelligence is not configured. Final2 will produce no "
            "text; handwritten pages therefore get no blank/junk verdict in pass 2 "
            "and member/DOS fall back to final1/prelim text."
        )

    with stage_run(chart_id, STAGE, force=force) as ctx:
        with connect() as conn:
            bj1 = get_blank_junk_flags(conn, chart_id, pass_no=1)
            hw_ids = handwritten_page_ids(conn, chart_id)

            drop = [
                pid for pid in ctx.todo
                if pid not in hw_ids and bj1.get(pid, "not_blank_junk") in BJ_EXCLUDE
            ]
            mark_skipped(conn, ctx, drop, "blank_junk_pass1")

            todo = [p for p in ctx.pages if p["id"] in ctx.todo]
            for page in todo:
                mark_processing(conn, ctx, page["id"])

        results: list[dict[str, Any]] = []
        if todo:
            # Azure DI is a network call, so more workers than CPUs is fine —
            # but the service throttles, so this stays on the same dial.
            workers = max(1, min(STAGE_WORKERS, len(todo)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(
                    pool.map(
                        _ocr_one,
                        [(p, page_image_path(ctx.chart_name, p["page_name"]), use_azure)
                         for p in todo],
                    )
                )

        with connect() as conn:
            for item in results:
                if item["error"]:
                    mark_failed(
                        conn, ctx, item["page_id"], item["error"], item["page_name"]
                    )
                    continue
                page_doc = {
                    "pageNumber": item["page_number"],
                    "fileName": item["page_name"],
                    "content": item["content"],
                    "pagesMeta": item["pages_meta"],
                }
                upsert_ocr_result(
                    conn,
                    chart_id=chart_id,
                    page_id=item["page_id"],
                    ocr_type="azuredocintel",
                    raw_text=json.dumps(page_doc),
                )
                mark_completed(conn, ctx, item["page_id"])
            stored = get_ocr_texts(conn, chart_id, "azuredocintel")

        # Rebuild the combined JSON from the database, covering every page.
        out_pages: list[dict[str, Any]] = []
        for page in ctx.pages:
            raw = stored.get(page["id"])
            content = ""
            if raw:
                try:
                    content = str(json.loads(raw).get("content") or "")
                except (ValueError, AttributeError):
                    content = raw
            out_pages.append(
                {
                    "pageNumber": page.get("page_number"),
                    "fileName": page["page_name"],
                    "content": content,
                }
            )
        out = write_final2_json(ctx.chart_name, out_pages)

        return {
            "chart_id": chart_id,
            "ocr_path": str(out),
            "used_azure": use_azure,
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            "errors": ctx.errors,
        }
