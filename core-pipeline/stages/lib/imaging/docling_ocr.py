"""Docling layout + RapidOCR engine for final OCR 1.

Ported from the V1 ``os_ocr.py`` prototype: Docling supplies layout, table
structure (TableFormer), heading hierarchy and reading order; RapidOCR (local
``.pth`` models) supplies the text.

When Docling or the RapidOCR model files are missing, callers fall back to the
lighter RapidOCR-onnx path — a degraded run is stamped in the stage result
rather than failing the chart. Tesseract is not used here (it is prelim only).
"""
from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_converter_lock = threading.Lock()
# BATCH_WORKERS>1 otherwise runs several Torch Docling converts in parallel on
# one CPU — every page hits the timeout and orphan workers keep burning cycles.
# Only one ``engine.convert`` may run process-wide.
_docling_infer_lock = threading.Lock()
_converter: Any = None
_converter_ready = False
_converter_reason: Optional[str] = None


def rapid_models_dir() -> Path:
    try:
        from config import RAPID_MODELS_DIR

        return Path(RAPID_MODELS_DIR)
    except Exception:
        return Path(
            os.environ.get("RAPID_MODELS_DIR")
            or (Path.home() / "Desktop" / "Imaging" / "rapidocr_models")
        )


def model_paths(models_dir: Path | None = None) -> dict[str, Path]:
    root = Path(models_dir) if models_dir is not None else rapid_models_dir()
    return {
        "det": root / "PP-OCRv6_det_small.pth",
        "rec": root / "PP-OCRv6_rec_small.pth",
        "cls": root / "ch_ptocr_mobile_v2.0_cls_mobile.pth",
        "keys": root / "ppocrv6_dict.txt",
    }


def missing_model_files(models_dir: Path | None = None) -> list[Path]:
    return [
        path
        for path in model_paths(models_dir).values()
        if not path.is_file() or path.stat().st_size == 0
    ]


def docling_importable() -> bool:
    from importlib.util import find_spec

    return find_spec("docling") is not None and find_spec("rapidocr") is not None


def docling_status() -> dict[str, Any]:
    """What GET /health reports for the Docling + Layout final1 path."""
    status: dict[str, Any] = {
        "ready": False,
        "models_dir": str(rapid_models_dir()),
    }
    if not docling_importable():
        status["reason"] = (
            "docling/rapidocr not installed — "
            "pip install -r requirements-docling.txt"
        )
        return status
    missing = missing_model_files()
    if missing:
        status["reason"] = (
            "RapidOCR model files missing under RAPID_MODELS_DIR: "
            + ", ".join(p.name for p in missing)
        )
        return status
    status["ready"] = True
    return status


def _build_rapidocr_options(models: dict[str, Path]) -> Any:
    """Match RapidOcrOptions to whatever fields this Docling version exposes."""
    from docling.datamodel.pipeline_options import RapidOcrOptions
    from rapidocr.utils.typings import EngineType

    params = {
        "Det.engine_type": EngineType.TORCH,
        "Cls.engine_type": EngineType.TORCH,
        "Rec.engine_type": EngineType.TORCH,
        "Det.model_path": str(models["det"]),
        "Cls.model_path": str(models["cls"]),
        "Rec.model_path": str(models["rec"]),
        "Rec.rec_keys_path": str(models["keys"]),
    }
    fields = set(RapidOcrOptions.model_fields.keys())

    passthrough = [
        name
        for name in fields
        if "params" in name.lower() and "rapidocr" in name.lower()
    ] or [name for name in fields if name.lower() == "params"]
    if passthrough:
        field_name = passthrough[0]
        logger.info("RapidOcrOptions: passthrough field %s", field_name)
        return RapidOcrOptions(
            force_full_page_ocr=True, **{field_name: dict(params)}
        )

    legacy = {"det_model_path", "cls_model_path", "rec_model_path", "rec_keys_path"}
    if legacy <= fields:
        kwargs: dict[str, Any] = {
            "force_full_page_ocr": True,
            "det_model_path": str(models["det"]),
            "cls_model_path": str(models["cls"]),
            "rec_model_path": str(models["rec"]),
            "rec_keys_path": str(models["keys"]),
        }
        for name in fields:
            if "engine_type" in name.lower():
                kwargs[name] = EngineType.TORCH
        logger.info("RapidOcrOptions: legacy per-path fields")
        return RapidOcrOptions(**kwargs)

    raise RuntimeError(
        "Could not match RapidOcrOptions to a known Docling shape. "
        f"Fields: {sorted(fields)}"
    )


def build_converter(models_dir: Path | None = None) -> Any:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import (
        PdfPipelineOptions,
        TableFormerMode,
    )
    from docling.document_converter import DocumentConverter, ImageFormatOption

    missing = missing_model_files(models_dir)
    if missing:
        listed = "\n".join(f"  {p}" for p in missing)
        raise RuntimeError(
            "Missing RapidOCR model files for Docling:\n"
            f"{listed}\n"
            f"Set RAPID_MODELS_DIR to the folder holding all four."
        )

    # ACCURATE TableFormer was measured at ~13 min/page on large TIFFs with
    # empty RapidOCR regions. FAST keeps layout/tables usable without blocking
    # the rest of the chain. Override with DOCLING_TABLE_MODE=accurate.
    table_mode_raw = (
        os.environ.get("DOCLING_TABLE_MODE") or "fast"
    ).strip().casefold()
    table_mode = (
        TableFormerMode.ACCURATE
        if table_mode_raw in {"accurate", "acc", "full"}
        else TableFormerMode.FAST
    )
    do_tables = (os.environ.get("DOCLING_DO_TABLES") or "true").strip().casefold() not in {
        "0",
        "false",
        "off",
        "no",
    }

    models = model_paths(models_dir)
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.do_table_structure = do_tables
    # Image inputs must stay at scale 1.0. Docling's PDF default (2.0) doubles
    # page.size while provenance bboxes stay in native image pixels — overlays
    # then render at half width/height on the review-ui page image.
    if hasattr(pipeline_options, "images_scale"):
        pipeline_options.images_scale = float(
            os.environ.get("DOCLING_IMAGES_SCALE") or "1.0"
        )
    if do_tables:
        pipeline_options.table_structure_options.mode = table_mode
        # Cell matching fills TableFormer cells with OCR text — without it dense
        # form tables export as structure with empty cells. It costs time per
        # page, so a page can still hit DOCLING_PAGE_TIMEOUT_SECONDS; that
        # timeout is the only thing that sends final1 to RapidOCR-onnx.
        # Set false to trade table text for speed.
        pipeline_options.table_structure_options.do_cell_matching = (
            os.environ.get("DOCLING_TABLE_CELL_MATCHING") or "true"
        ).strip().casefold() in {"1", "true", "yes", "on"}
    pipeline_options.ocr_options = _build_rapidocr_options(models)
    logger.info(
        "Docling pipeline: tables=%s mode=%s cell_matching=%s images_scale=%s",
        do_tables,
        getattr(table_mode, "value", table_mode) if do_tables else "n/a",
        getattr(
            pipeline_options.table_structure_options,
            "do_cell_matching",
            False,
        )
        if do_tables
        else False,
        getattr(pipeline_options, "images_scale", None),
    )
    return DocumentConverter(
        format_options={
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline_options),
        }
    )


def get_converter() -> Any | None:
    """Process-wide Docling converter, or None when unavailable."""
    global _converter, _converter_ready, _converter_reason
    if _converter_ready:
        return _converter
    with _converter_lock:
        if _converter_ready:
            return _converter
        status = docling_status()
        if not status["ready"]:
            _converter = None
            _converter_reason = status.get("reason")
            logger.warning("Docling final1 unavailable: %s", _converter_reason)
        else:
            try:
                _converter = build_converter()
                _converter_reason = None
                logger.info(
                    "Docling + RapidOCR converter ready (models=%s)",
                    status["models_dir"],
                )
            except Exception as exc:
                _converter = None
                _converter_reason = f"{type(exc).__name__}: {exc}"
                logger.warning("Docling converter failed to build: %s", exc)
        _converter_ready = True
        return _converter


def converter_reason() -> Optional[str]:
    get_converter()
    return _converter_reason


def clean_docling_markdown(text: str) -> str:
    """Strip Docling noise: image placeholders, empty tables, excess blank lines."""
    import re

    if not text:
        return ""
    out = re.sub(r"<!--\s*image\s*-->", "", text, flags=re.IGNORECASE)
    # Drop lines that are only markdown table chrome (| --- | or empty cells).
    cleaned_lines: list[str] = []
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append("")
            continue
        # Pure separator row: |---|---|
        if re.fullmatch(r"\|?[\s\-:|]+\|?", stripped):
            continue
        # Row of only empty cells: | | | |
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if stripped.startswith("|") and all(c == "" for c in cells):
            continue
        cleaned_lines.append(line.rstrip())
    # Collapse runs of blank lines to a single blank.
    collapsed: list[str] = []
    blank = False
    for line in cleaned_lines:
        if not line.strip():
            if blank:
                continue
            collapsed.append("")
            blank = True
        else:
            collapsed.append(line)
            blank = False
    return "\n".join(collapsed).strip() + ("\n" if collapsed else "")


def _iter_doc_pages(doc: Any) -> list[tuple[int, Any]]:
    """Yield ``(page_no_1based, page)`` from Docling ``doc.pages``.

    DoclingDocument.pages is normally a ``dict[int, Page]`` keyed by 1-based
    page number. Older shapes (list) still work. Mis-iterating a dict with
    ``enumerate`` was discarding sizes, so every header lost its ``norm``.
    """
    pages = getattr(doc, "pages", None)
    if pages is None:
        return []
    out: list[tuple[int, Any]] = []
    if isinstance(pages, dict):
        for key, page in pages.items():
            try:
                page_no = int(key) if not isinstance(key, int) else key
            except (TypeError, ValueError):
                page_no = len(out) + 1
            if page_no < 1:
                page_no = 1
            out.append((page_no, page))
        return out
    if isinstance(pages, (list, tuple)):
        for idx, page in enumerate(pages, start=1):
            out.append((idx, page))
    return out


def _page_sizes_map(doc: Any) -> dict[int, tuple[float, float]]:
    """Map 0- and 1-based page numbers → (width, height)."""
    sizes: dict[int, tuple[float, float]] = {}
    try:
        for page_no, page in _iter_doc_pages(doc):
            size = getattr(page, "size", None)
            if size is None and isinstance(page, dict):
                size = page.get("size")
            if size is None:
                continue
            w = float(getattr(size, "width", None) or (size.get("width") if isinstance(size, dict) else 0) or 0)
            h = float(getattr(size, "height", None) or (size.get("height") if isinstance(size, dict) else 0) or 0)
            if w > 0 and h > 0:
                sizes[page_no] = (w, h)
                sizes[page_no - 1] = (w, h)  # Docling prov.page_no is often 0-based
    except Exception:
        pass
    return sizes


def extract_section_headers(doc: Any) -> list[dict[str, Any]]:
    """Compact heading boxes for the review-ui overlay.

    Shortlist = Docling-labeled ``section_header`` / ``title`` items only.
    Canon matching happens afterward in ``filter_section_headers`` — no
    regex / ALL-CAPS heuristics invent extra candidates.

    Each item: ``{text, level, bbox, page_width, page_height, coord_origin, norm}``
    where ``norm`` is CSS-ready fractions (0–1) when page size is known.
    """
    headers: list[dict[str, Any]] = []
    seen: set[str] = set()
    page_sizes = _page_sizes_map(doc)

    header_labels = {
        "section_header",
        "title",
        "section-header",
        "sectionheader",
    }
    skip_labels = {
        "page_header",
        "page_footer",
        "page_number",
        "picture",
        "table",
        "caption",
        "footnote",
    }

    def _label_str(item: Any) -> str:
        lab = getattr(item, "label", None)
        if lab is None:
            return ""
        return str(getattr(lab, "value", lab)).strip().casefold().replace(" ", "_")

    def _append(text: str, level: int, prov: Any) -> None:
        text = (text or "").strip()
        if not text:
            return
        key = re.sub(r"\s+", " ", text).casefold()
        if key in seen:
            return
        seen.add(key)

        l = t = r = b = 0.0
        # Image converts are TOPLEFT; PDF-style BOTTOMLEFT is the exception.
        origin = "TOPLEFT"
        page_no = 1
        has_box = False
        if prov is not None:
            first = prov[0] if isinstance(prov, (list, tuple)) and prov else prov
            bbox = getattr(first, "bbox", None)
            if bbox is None and isinstance(first, dict):
                bbox = first.get("bbox")
            if bbox is not None:
                if hasattr(bbox, "l"):
                    l, t, r, b = float(bbox.l), float(bbox.t), float(bbox.r), float(bbox.b)
                    origin = str(
                        getattr(getattr(first, "coord_origin", None), "value", None)
                        or getattr(getattr(bbox, "coord_origin", None), "value", None)
                        or getattr(first, "coord_origin", None)
                        or getattr(bbox, "coord_origin", None)
                        or "TOPLEFT"
                    )
                    raw_page = getattr(first, "page_no", None)
                    page_no = int(raw_page) if raw_page is not None else 1
                    has_box = True
                elif isinstance(bbox, dict):
                    l = float(bbox.get("l") or bbox.get("left") or 0)
                    t = float(bbox.get("t") or bbox.get("top") or 0)
                    r = float(bbox.get("r") or bbox.get("right") or 0)
                    b = float(bbox.get("b") or bbox.get("bottom") or 0)
                    origin = str(
                        first.get("coord_origin")
                        or bbox.get("coord_origin")
                        or "TOPLEFT"
                    )
                    raw_page = first.get("page_no")
                    page_no = int(raw_page) if raw_page is not None else 1
                    has_box = True

        pw, ph = page_sizes.get(page_no, (0.0, 0.0))
        if (not pw or not ph) and page_sizes:
            for key in sorted(k for k in page_sizes if k >= 1):
                pw, ph = page_sizes[key]
                break
            if not pw:
                pw, ph = next(iter(page_sizes.values()))
        norm = (
            _bbox_to_css_norm(l, t, r, b, pw, ph, origin) if has_box and pw and ph else None
        )
        headers.append(
            {
                "text": text,
                "level": int(level) if level else 2,
                "bbox": [round(l, 2), round(t, 2), round(r, 2), round(b, 2)]
                if has_box
                else [],
                "page_width": pw or None,
                "page_height": ph or None,
                "coord_origin": origin if has_box else None,
                "norm": norm,
            }
        )

    # Docling-labeled section headers / titles only (shortlist).
    try:
        iterate = getattr(doc, "iterate_items", None)
        if callable(iterate):
            for item, level in iterate():
                lab = _label_str(item)
                if lab in skip_labels:
                    continue
                if lab not in header_labels and "section" not in lab and lab != "title":
                    continue
                text = getattr(item, "text", None) or getattr(item, "orig", None) or ""
                _append(str(text), int(level) if level else 1, getattr(item, "prov", None))
    except Exception as exc:
        logger.debug("iterate_items labeled header extract failed: %s", exc)

    # Fallback: export_to_dict texts[] with the same labels only.
    if not headers:
        try:
            data = doc.export_to_dict() if hasattr(doc, "export_to_dict") else doc
            if isinstance(data, dict):
                headers.extend(extract_section_headers_from_dict(data, seen=seen))
        except Exception as exc:
            logger.debug("dict header extract failed: %s", exc)
    return headers


def extract_section_headers_from_dict(
    data: dict[str, Any],
    *,
    seen: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Same shortlist as ``extract_section_headers``, from an exported dict.

    Used by the standalone ``section_headers`` stage when Final1 JSON still
    carries ``document`` (or when rebuilding candidates without re-OCR).
    """
    if not isinstance(data, dict):
        return []
    header_labels = {
        "section_header",
        "title",
        "section-header",
        "sectionheader",
    }
    headers: list[dict[str, Any]] = []
    seen_keys = seen if seen is not None else set()

    for t in data.get("texts") or []:
        if not isinstance(t, dict):
            continue
        lab = str(t.get("label") or "").strip().casefold().replace(" ", "_")
        if lab not in header_labels and "section" not in lab and lab != "title":
            continue
        text = str(t.get("text") or "").strip()
        if not text:
            continue
        key = re.sub(r"\s+", " ", text).casefold()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        l = t_coord = r = b = 0.0
        origin = "TOPLEFT"
        has_box = False
        pw = ph = 0.0
        prov = t.get("prov") or []
        first = prov[0] if isinstance(prov, list) and prov else prov
        bbox = None
        if isinstance(first, dict):
            bbox = first.get("bbox")
            origin = str(first.get("coord_origin") or origin)
        if isinstance(bbox, dict):
            l = float(bbox.get("l") or bbox.get("left") or 0)
            t_coord = float(bbox.get("t") or bbox.get("top") or 0)
            r = float(bbox.get("r") or bbox.get("right") or 0)
            b = float(bbox.get("b") or bbox.get("bottom") or 0)
            has_box = True
        page_sizes = data.get("pages") or {}
        # Docling dict pages may be a list or map; best-effort size.
        if isinstance(page_sizes, list) and page_sizes:
            p0 = page_sizes[0] if isinstance(page_sizes[0], dict) else {}
            size = p0.get("size") if isinstance(p0, dict) else None
            if isinstance(size, dict):
                pw = float(size.get("width") or 0)
                ph = float(size.get("height") or 0)
        norm = None
        if has_box and pw > 0 and ph > 0:
            norm = _bbox_to_css_norm(l, t_coord, r, b, pw, ph, origin)
        headers.append(
            {
                "text": text,
                "level": 1 if lab == "title" else 2,
                "bbox": [round(l, 2), round(t_coord, 2), round(r, 2), round(b, 2)]
                if has_box
                else [],
                "page_width": pw or None,
                "page_height": ph or None,
                "coord_origin": origin if has_box else None,
                "norm": norm,
            }
        )
    return headers


def _image_pixel_size(image_path: Path) -> tuple[float, float] | None:
    """Return ``(width, height)`` of the page image, or None."""
    try:
        from PIL import Image

        with Image.open(image_path) as im:
            w, h = im.size
            if w > 0 and h > 0:
                return float(w), float(h)
    except Exception:
        pass
    return None


def _resolve_norm_page_size(
    page_w: float,
    page_h: float,
    image_size: tuple[float, float] | None,
    bboxes: list[tuple[float, float, float, float]],
) -> tuple[float, float]:
    """Pick the denominator for CSS norms so overlays match the displayed image.

    When Docling's ``images_scale`` left ``page.size`` at ~2× the native image
    (or bboxes stayed in native pixels while page.size grew), dividing by
    page.size yields half-size boxes. Prefer the native image size when it
    clearly fits the bbox extents better.
    """
    if page_w <= 0 or page_h <= 0:
        if image_size:
            return image_size
        return page_w, page_h
    if not image_size:
        return page_w, page_h
    iw, ih = image_size
    if iw <= 0 or ih <= 0:
        return page_w, page_h

    # Same size (±8%) → already aligned.
    if abs(page_w - iw) / iw <= 0.08 and abs(page_h - ih) / ih <= 0.08:
        return page_w, page_h

    # page ≈ 2× image (classic images_scale=2) → bboxes are usually in image
    # pixels; normalize against the image so overlays match the file we show.
    if abs(page_w / iw - 2.0) <= 0.15 and abs(page_h / ih - 2.0) <= 0.15:
        return iw, ih

    # image ≈ 2× page → bboxes in page space; keep Docling page size.
    if abs(iw / page_w - 2.0) <= 0.15 and abs(ih / page_h - 2.0) <= 0.15:
        return page_w, page_h

    # Extent test: if every bbox fits the image but overflows half of page,
    # page.size is the inflated one.
    if bboxes:
        max_x = max(max(l, r) for l, t, r, b in bboxes)
        max_y = max(max(t, b) for l, t, r, b in bboxes)
        fits_image = max_x <= iw * 1.02 and max_y <= ih * 1.02
        overflows_half_page = max_x > page_w * 0.55 or max_y > page_h * 0.55
        if fits_image and page_w > iw * 1.4:
            return iw, ih
        if fits_image and not overflows_half_page and page_w >= iw * 1.8:
            return iw, ih

    return page_w, page_h


def _renorm_headers_for_image(
    headers: list[dict[str, Any]],
    image_path: Path,
) -> list[dict[str, Any]]:
    """Recompute ``norm`` in displayed-image fractions (OCR→image scale)."""
    image_size = _image_pixel_size(image_path)
    if not image_size or not headers:
        return headers
    iw, ih = image_size
    out: list[dict[str, Any]] = []
    for h in headers:
        item = dict(h)
        box = item.get("bbox")
        if not isinstance(box, (list, tuple)) or len(box) < 4:
            out.append(item)
            continue
        try:
            l, t, r, b = (float(x) for x in box[:4])
        except (TypeError, ValueError):
            out.append(item)
            continue
        pw = float(item.get("page_width") or 0) or iw
        ph = float(item.get("page_height") or 0) or ih
        origin = str(item.get("coord_origin") or "TOPLEFT")
        # document-processing: scale_x = image_w / page_w
        if pw > 0 and ph > 0:
            sx, sy = iw / pw, ih / ph
            l, t, r, b = l * sx, t * sy, r * sx, b * sy
        norm = _bbox_to_css_norm(l, t, r, b, iw, ih, origin)
        item["page_width"] = iw
        item["page_height"] = ih
        item["bbox"] = [round(l, 2), round(t, 2), round(r, 2), round(b, 2)]
        item["norm"] = norm
        out.append(item)
    return out


def _bbox_to_css_norm(
    l: float,
    t: float,
    r: float,
    b: float,
    page_w: float,
    page_h: float,
    origin: str,
) -> dict[str, float] | None:
    """Convert Docling bbox to CSS top-left fractions (0–1).

    Image OCR almost always uses TOPLEFT (y down). PDF-style BOTTOMLEFT (y up)
    is supported when declared. If the declared origin disagrees with whether
    ``t`` is above ``b``, trust the geometry.
    """
    if page_w <= 0 or page_h <= 0:
        return None
    origin_u = (origin or "TOPLEFT").upper().replace("-", "").replace("_", "")
    # t/b ordering is a reliable signal when the flag is missing or wrong.
    if t < b and origin_u.startswith("BOTTOM"):
        origin_u = "TOPLEFT"
    elif t > b and origin_u.startswith("TOP"):
        origin_u = "BOTTOMLEFT"

    left = min(l, r)
    right = max(l, r)
    if origin_u.startswith("BOTTOM"):
        top_y = max(t, b)  # higher in bottom-left space = toward top of page
        bot_y = min(t, b)
        css_top = (page_h - top_y) / page_h
        css_height = (top_y - bot_y) / page_h
    else:
        top_y = min(t, b)
        bot_y = max(t, b)
        css_top = top_y / page_h
        css_height = (bot_y - top_y) / page_h
    css_left = left / page_w
    css_width = (right - left) / page_w

    def clip(v: float) -> float:
        return max(0.0, min(1.0, float(v)))

    return {
        "left": round(clip(css_left), 5),
        "top": round(clip(css_top), 5),
        "width": round(clip(css_width), 5),
        "height": round(clip(css_height), 5),
    }


def convert_image(image_path: Path, converter: Any | None = None) -> dict[str, Any]:
    """Run Docling on one page image.

    Returns ``{markdown, document, content, section_header_candidates,
    section_headers}``. ``document`` is omitted unless
    ``DOCLING_EXPORT_DOCUMENT=true``.

    Canon matching is owned by the ``section_headers`` stage (re-runnable from
    JSON alone). This path still applies a best-effort filter so
    ``through=ocr_final1`` has overlays before that stage runs; the stage
    overwrites ``section_headers`` from ``section_header_candidates``.
    """
    import time

    engine = converter if converter is not None else get_converter()
    if engine is None:
        raise RuntimeError(converter_reason() or "Docling converter unavailable")
    started = time.perf_counter()
    result = engine.convert(str(image_path))
    doc = result.document
    markdown = clean_docling_markdown(doc.export_to_markdown() or "")
    candidates = _renorm_headers_for_image(extract_section_headers(doc), image_path)
    section_headers = list(candidates)
    try:
        from stages.lib.imaging.section_headers_io import apply_header_filter

        section_headers = apply_header_filter(candidates)
        if len(candidates) != len(section_headers):
            logger.info(
                "Section headers filtered %d → %d (semantic ≥ threshold)",
                len(candidates),
                len(section_headers),
            )
    except Exception as exc:
        logger.warning("Section-header semantic filter skipped: %s", exc)
    export_doc = (
        os.environ.get("DOCLING_EXPORT_DOCUMENT") or "false"
    ).strip().casefold() in {"1", "true", "yes", "on"}
    document = doc.export_to_dict() if export_doc else None
    elapsed = time.perf_counter() - started
    logger.info(
        "Docling converted %s in %.1fs (chars=%d, candidates=%d, headers=%d, document=%s)",
        image_path.name,
        elapsed,
        len(markdown),
        len(candidates),
        len(section_headers),
        "yes" if document is not None else "skipped",
    )
    return {
        "markdown": markdown,
        "content": markdown,
        "document": document,
        "section_header_candidates": candidates,
        "section_headers": section_headers,
        "elapsed_seconds": elapsed,
    }


def convert_image_with_timeout(
    image_path: Path,
    *,
    converter: Any | None = None,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    """``convert_image`` bounded by a wall-clock timeout.

    Process-wide lock: with ``BATCH_WORKERS>1``, four charts otherwise call
    Docling at once on the same CPU/converter and every page times out (the
    ``1.jpg`` / ``page_0`` spam). Waiters that cannot get the lock within
    ``timeout_seconds`` raise ``TimeoutError`` and fall back to RapidOCR-onnx.

    On convert timeout the orphan may keep the lock until it finishes so we do
    not stack another Torch job on top of it.
    """
    import contextvars
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    limit = max(1.0, float(timeout_seconds))
    if not _docling_infer_lock.acquire(timeout=limit):
        raise TimeoutError(
            f"Docling busy >{limit:.0f}s (another chart still converting) — "
            f"skip {image_path.name}"
        )

    released = False

    def _release(_fut: Any = None) -> None:
        nonlocal released
        if not released:
            released = True
            try:
                _docling_infer_lock.release()
            except RuntimeError:
                pass

    # Carry [batch#] [chart#] into the worker thread.
    ctx = contextvars.copy_context()
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="docling")
    fut: Any = None
    try:
        fut = pool.submit(ctx.run, convert_image, image_path, converter)
        try:
            out = fut.result(timeout=limit)
            _release()
            return out
        except FuturesTimeout as exc:
            fut.add_done_callback(_release)
            logger.warning(
                "Docling timeout after %.0fs on %s — abandoning worker "
                "(lock held until it ends); caller should fall back to Rapid",
                limit,
                image_path.name,
            )
            raise TimeoutError(
                f"Docling exceeded {limit:.0f}s on {image_path.name}"
            ) from exc
        except Exception:
            if fut is not None and not fut.done():
                fut.add_done_callback(_release)
            else:
                _release()
            raise
    except Exception:
        if fut is None:
            _release()
        raise
    finally:
        # Critical: wait=False or we block until the stuck convert ends.
        pool.shutdown(wait=False, cancel_futures=True)
