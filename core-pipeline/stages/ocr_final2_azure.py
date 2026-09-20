"""Stage: final OCR 2 (Azure Document Intelligence, prebuilt-read).

This is the only stage that costs money per page, so it is the one that most
needs the resume behaviour: a page already ``completed`` is never re-sent.

One ``DocumentIntelligenceClient`` is built per process and shared; v6 built a
new client (and a new TLS handshake) for every page.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from config import (
    AZURE_DI_CONNECTION_POOL_SIZE,
    AZURE_DI_FEATURES,
    AZURE_DI_MAX_CONCURRENT,
    AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT,
    AZURE_DOCUMENT_INTELLIGENCE_KEY,
    AZURE_POLL_TIMEOUT_SECONDS,
    AZURE_RETRY_ATTEMPTS,
    AZURE_RETRY_BASE_DELAY,
    AZURE_RETRY_MAX_DELAY,
    STAGE_WORKERS,
    page_image_path,
)
from db import (
    connect,
    get_blank_junk_flags,
    get_ocr_texts,
    high_quality_printed_page_ids,
    low_quality_page_ids,
    non_printed_page_ids,
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
_di_semaphore: Optional[threading.Semaphore] = None
_di_semaphore_lock = threading.Lock()


def _azure_configured() -> bool:
    return bool(
        AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and AZURE_DOCUMENT_INTELLIGENCE_KEY
    )


def _di_features() -> Optional[list[str]]:
    """Parse AZURE_DI_FEATURES into a list, or None when disabled."""
    raw = (AZURE_DI_FEATURES or "").strip()
    if not raw or raw in {"0", "false", "off", "none", "-"}:
        return None
    features = [p.strip() for p in raw.split(",") if p.strip()]
    return features or None


def _get_di_semaphore() -> threading.Semaphore:
    """Process-wide cap on concurrent DI analyzes across all in-flight charts."""
    global _di_semaphore
    if _di_semaphore is not None:
        return _di_semaphore
    with _di_semaphore_lock:
        if _di_semaphore is None:
            _di_semaphore = threading.Semaphore(max(1, AZURE_DI_MAX_CONCURRENT))
        return _di_semaphore


def _get_client() -> Any:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            from azure.ai.documentintelligence import DocumentIntelligenceClient
            from azure.core.credentials import AzureKeyCredential

            from azure_retry import azure_sdk_retry_kwargs

            _client = DocumentIntelligenceClient(
                endpoint=AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT,
                credential=AzureKeyCredential(AZURE_DOCUMENT_INTELLIGENCE_KEY),
                **azure_sdk_retry_kwargs(
                    connection_pool_maxsize=AZURE_DI_CONNECTION_POOL_SIZE
                ),
            )
        return _client


def _barcode_dict(barcode: Any) -> dict[str, Any]:
    return {
        "kind": getattr(barcode, "kind", None),
        "value": getattr(barcode, "value", None),
        "confidence": getattr(barcode, "confidence", None),
    }


def _flatten_polygon(raw: Any) -> list[float]:
    """Azure DI polygon → flat ``[x1,y1,…,x4,y4]`` (pixels, top-left origin)."""
    if raw is None:
        return []
    out: list[float] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if hasattr(item, "x") and hasattr(item, "y"):
                out.extend([float(item.x), float(item.y)])
            else:
                try:
                    out.append(float(item))
                except (TypeError, ValueError):
                    return []
    return out if len(out) >= 8 and len(out) % 2 == 0 else []


def _polygon_bbox(polygon: list[float]) -> tuple[float, float, float, float] | None:
    if len(polygon) < 8:
        return None
    xs = polygon[0::2]
    ys = polygon[1::2]
    return min(xs), min(ys), max(xs), max(ys)


def _css_norm_topleft(
    l: float, t: float, r: float, b: float, page_w: float, page_h: float
) -> dict[str, float] | None:
    """Azure Read coordinates are top-left pixel space → CSS fractions 0–1."""
    if page_w <= 0 or page_h <= 0:
        return None

    def clip(v: float) -> float:
        return max(0.0, min(1.0, float(v)))

    left = min(l, r)
    right = max(l, r)
    top = min(t, b)
    bottom = max(t, b)
    return {
        "left": round(clip(left / page_w), 5),
        "top": round(clip(top / page_h), 5),
        "width": round(clip((right - left) / page_w), 5),
        "height": round(clip((bottom - top) / page_h), 5),
    }


def _line_dict(line: Any) -> dict[str, Any]:
    content = getattr(line, "content", None)
    if content is None and isinstance(line, dict):
        content = line.get("content")
    polygon = _flatten_polygon(
        getattr(line, "polygon", None)
        if not isinstance(line, dict)
        else line.get("polygon")
    )
    return {
        "content": str(content or ""),
        "polygon": polygon,
    }


def _word_dict(word: Any) -> dict[str, Any]:
    content = getattr(word, "content", None)
    if content is None and isinstance(word, dict):
        content = word.get("content")
    conf = getattr(word, "confidence", None)
    if conf is None and isinstance(word, dict):
        conf = word.get("confidence")
    polygon = _flatten_polygon(
        getattr(word, "polygon", None)
        if not isinstance(word, dict)
        else word.get("polygon")
    )
    return {
        "content": str(content or ""),
        "confidence": conf,
        "polygon": polygon,
    }


def candidates_from_lines(
    lines: list[dict[str, Any]],
    *,
    page_w: float,
    page_h: float,
    unit: str | None = None,
    image_size: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    """OCR lines → raw header candidates (no canon filter).

    Every non-empty line is a candidate. Maps OCR coords → image pixels via
    ``scale = image / page``, then stores fractions of the image. The
    ``section_headers`` stage filters these against the canon list.
    """
    iw = ih = 0.0
    if image_size:
        iw, ih = image_size
    use_w = iw if iw > 0 else page_w
    use_h = ih if ih > 0 else page_h
    sx = (iw / page_w) if (iw > 0 and page_w > 0) else 1.0
    sy = (ih / page_h) if (ih > 0 and page_h > 0) else 1.0

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in lines:
        text = str(line.get("content") or "").strip()
        if not text:
            continue
        key = re.sub(r"\s+", " ", text).casefold()
        if key in seen:
            continue
        seen.add(key)
        polygon = list(line.get("polygon") or [])
        box = _polygon_bbox(polygon)
        norm = None
        bbox: list[float] = []
        if box is not None:
            l, t, r, b = box
            l, t, r, b = l * sx, t * sy, r * sx, b * sy
            bbox = [round(l, 2), round(t, 2), round(r, 2), round(b, 2)]
            norm = _css_norm_topleft(l, t, r, b, use_w, use_h)
        candidates.append(
            {
                "text": text,
                "level": 2,
                "bbox": bbox,
                "polygon": polygon,
                "page_width": use_w or None,
                "page_height": use_h or None,
                "unit": "pixel" if iw > 0 else unit,
                "coord_origin": "TOPLEFT",
                "norm": norm,
            }
        )
    return candidates


def _section_headers_from_lines(
    lines: list[dict[str, Any]],
    *,
    page_w: float,
    page_h: float,
    unit: str | None = None,
    image_size: tuple[float, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """OCR lines → ``(candidates, filtered section_headers)``.

    Filtering is best-effort here so Final2 JSON is usable before the
    standalone ``section_headers`` stage runs; that stage re-derives from
    candidates / ``pagesMeta`` when the canon list changes.
    """
    candidates = candidates_from_lines(
        lines,
        page_w=page_w,
        page_h=page_h,
        unit=unit,
        image_size=image_size,
    )
    try:
        from stages.lib.imaging.section_headers_io import apply_header_filter

        return candidates, apply_header_filter(candidates, use_minilm=False)
    except Exception:
        return candidates, []


def _page_meta(page: Any) -> dict[str, Any]:
    lines_raw = getattr(page, "lines", None) or []
    words_raw = getattr(page, "words", None) or []
    barcodes = getattr(page, "barcodes", None) or []
    width = getattr(page, "width", None)
    height = getattr(page, "height", None)
    unit = getattr(page, "unit", None)
    if unit is not None and hasattr(unit, "value"):
        unit = getattr(unit, "value", unit)
    lines = [_line_dict(ln) for ln in lines_raw]
    # Words carry polygons too — useful for precise boxes, but large. Keep them.
    words = [_word_dict(w) for w in words_raw]
    return {
        "pageNumber": getattr(page, "page_number", None),
        "angle": getattr(page, "angle", None),
        "width": width,
        "height": height,
        "unit": str(unit) if unit is not None else None,
        "lineCount": len(lines),
        "wordCount": len(words),
        "lines": lines,
        "words": words,
        "barcodes": [_barcode_dict(b) for b in barcodes],
    }


def _languages_meta(result: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for lang in getattr(result, "languages", None) or []:
        out.append(
            {
                "locale": getattr(lang, "locale", None),
                "confidence": getattr(lang, "confidence", None),
            }
        )
    return out


def _ocr_azure(image_path: Path) -> dict[str, Any]:
    from azure_retry import call_with_retry

    features = _di_features()

    def _image_size() -> tuple[float, float] | None:
        try:
            from PIL import Image

            with Image.open(image_path) as im:
                w, h = im.size
                if w > 0 and h > 0:
                    return float(w), float(h)
        except Exception:
            return None
        return None

    def _call() -> dict[str, Any]:
        client = _get_client()
        kwargs: dict[str, Any] = {
            "content_type": "application/octet-stream",
        }
        if features:
            kwargs["features"] = features
        with image_path.open("rb") as fh:
            poller = client.begin_analyze_document(
                "prebuilt-read", body=fh, **kwargs
            )
        result = poller.result(timeout=AZURE_POLL_TIMEOUT_SECONDS)
        pages_meta = [
            _page_meta(p) for p in (getattr(result, "pages", None) or [])
        ]
        image_size = _image_size()
        section_headers: list[dict[str, Any]] = []
        section_header_candidates: list[dict[str, Any]] = []
        for meta in pages_meta:
            try:
                pw = float(meta.get("width") or 0)
                ph = float(meta.get("height") or 0)
            except (TypeError, ValueError):
                pw = ph = 0.0
            unit = meta.get("unit")
            if unit is not None and hasattr(unit, "value"):
                unit = getattr(unit, "value", unit)
            cands, kept = _section_headers_from_lines(
                list(meta.get("lines") or []),
                page_w=pw,
                page_h=ph,
                unit=str(unit) if unit is not None else None,
                image_size=image_size,
            )
            section_header_candidates.extend(cands)
            section_headers.extend(kept)
        return {
            "content": getattr(result, "content", None) or "",
            "pages_meta": pages_meta,
            "section_header_candidates": section_header_candidates,
            "section_headers": section_headers,
            "languages": _languages_meta(result),
            "features": features or [],
        }

    with _get_di_semaphore():
        return call_with_retry(
            _call,
            attempts=AZURE_RETRY_ATTEMPTS,
            base_delay=AZURE_RETRY_BASE_DELAY,
            max_delay=AZURE_RETRY_MAX_DELAY,
            label=f"azure.di:{image_path.name}",
        )


def _ocr_one(args: tuple[dict[str, Any], Path, bool]) -> dict[str, Any]:
    page, image_path, use_azure = args
    out: dict[str, Any] = {
        "page_id": page["id"],
        "page_name": page["page_name"],
        "page_number": page.get("page_number"),
        "content": "",
        "pages_meta": [],
        "section_header_candidates": [],
        "section_headers": [],
        "languages": [],
        "features": [],
        "error": "",
        "engine": "azure" if use_azure else "fallback",
    }
    try:
        if use_azure and image_path.is_file():
            extracted = _ocr_azure(image_path)
            out["content"] = extracted.get("content") or ""
            out["pages_meta"] = extracted.get("pages_meta") or []
            out["section_header_candidates"] = (
                extracted.get("section_header_candidates") or []
            )
            out["section_headers"] = extracted.get("section_headers") or []
            out["languages"] = extracted.get("languages") or []
            out["features"] = extracted.get("features") or []
            return out
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
        hq_printed: set[int] = set()
        with connect() as conn:
            bj1 = get_blank_junk_flags(conn, chart_id, pass_no=1)
            pass1_skippers = non_printed_page_ids(conn, chart_id) | low_quality_page_ids(
                conn, chart_id
            )
            hq_printed = high_quality_printed_page_ids(conn, chart_id)

            drop = [
                pid for pid in ctx.todo
                if pid not in pass1_skippers
                and bj1.get(pid, "not_blank_junk") in BJ_EXCLUDE
            ]
            bj_skipped = set(drop)
            mark_skipped(conn, ctx, drop, "blank_junk_pass1")
            # High-quality printed pages already have usable final1 text —
            # skip the billed Azure call.
            mark_skipped(
                conn, ctx, sorted(hq_printed & ctx.todo), "high_quality_printed"
            )

            todo = [p for p in ctx.pages if p["id"] in ctx.todo]
            for page in todo:
                mark_processing(conn, ctx, page["id"])

        results: list[dict[str, Any]] = []
        if todo:
            # Azure DI is a network call, so more workers than CPUs is fine —
            # but the service throttles, so this stays on the same dial.
            workers = max(1, min(STAGE_WORKERS, len(todo)))
            logger.info("Final2 Azure DI workers=%d", workers)
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="page") as pool:
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
                    "section_header_candidates": item.get("section_header_candidates")
                    or [],
                    "section_headers": item.get("section_headers") or [],
                    "languages": item.get("languages") or [],
                    "features": item.get("features") or [],
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
        # HQ-printed / blank-junk skips are stamped so review-ui can explain why.
        out_pages: list[dict[str, Any]] = []
        for page in ctx.pages:
            raw = stored.get(page["id"])
            content = ""
            pages_meta: list[Any] = []
            languages: list[Any] = []
            section_headers: list[Any] = []
            section_header_candidates: list[Any] = []
            if raw:
                try:
                    parsed = json.loads(raw)
                    content = str(parsed.get("content") or "")
                    pages_meta = list(parsed.get("pagesMeta") or [])
                    languages = list(parsed.get("languages") or [])
                    section_headers = list(
                        parsed.get("section_headers")
                        or parsed.get("sectionHeaders")
                        or []
                    )
                    section_header_candidates = list(
                        parsed.get("section_header_candidates")
                        or parsed.get("sectionHeaderCandidates")
                        or []
                    )
                except (ValueError, AttributeError, TypeError):
                    content = raw
            entry: dict[str, Any] = {
                "pageNumber": page.get("page_number"),
                "fileName": page["page_name"],
                "content": content,
                "pagesMeta": pages_meta,
                "section_header_candidates": section_header_candidates,
                "section_headers": section_headers,
                "languages": languages,
            }
            if not str(content or "").strip():
                if page["id"] in bj_skipped:
                    entry["skippedReason"] = "blank_junk_pass1"
                elif page["id"] in hq_printed:
                    entry["skippedReason"] = "high_quality_printed"
            out_pages.append(entry)
        out = write_final2_json(ctx.chart_name, out_pages)

        return {
            "chart_id": chart_id,
            "ocr_path": str(out),
            "used_azure": use_azure,
            "pages_done": ctx.done,
            "skipped": ctx.skipped,
            "errors": ctx.errors,
        }
