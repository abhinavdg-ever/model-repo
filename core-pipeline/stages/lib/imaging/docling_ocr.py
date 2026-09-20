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
# Two process-wide converters: cell-matching on vs off. Primary final1 uses
# the env default (usually True); hybrid fallback forces False for headers.
_converters: dict[bool, Any] = {}
_converter_ready: dict[bool, bool] = {}
_converter_reason: Optional[str] = None


def _env_cell_matching() -> bool:
    return (
        os.environ.get("DOCLING_TABLE_CELL_MATCHING") or "true"
    ).strip().casefold() in {"1", "true", "yes", "on"}


def _resolve_cell_matching(cell_matching: bool | None) -> bool:
    return _env_cell_matching() if cell_matching is None else bool(cell_matching)


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


def build_converter(
    models_dir: Path | None = None,
    *,
    cell_matching: bool | None = None,
) -> Any:
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
    use_cell_matching = _resolve_cell_matching(cell_matching)

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
        # Cell matching fills TableFormer cells with OCR text. Without it,
        # dense form tables export as empty markdown rows and the page looks
        # header-only in Final (OSS). Default ON for the primary pass; the
        # hybrid fallback forces False for section headers after a timeout.
        pipeline_options.table_structure_options.do_cell_matching = use_cell_matching
    pipeline_options.ocr_options = _build_rapidocr_options(models)
    logger.info(
        "Docling pipeline: tables=%s mode=%s cell_matching=%s images_scale=%s",
        do_tables,
        getattr(table_mode, "value", table_mode) if do_tables else "n/a",
        use_cell_matching if do_tables else False,
        getattr(pipeline_options, "images_scale", None),
    )
    return DocumentConverter(
        format_options={
            InputFormat.IMAGE: ImageFormatOption(pipeline_options=pipeline_options),
        }
    )


def get_converter(cell_matching: bool | None = None) -> Any | None:
    """Process-wide Docling converter for the requested cell-matching mode.

    ``cell_matching=None`` uses ``DOCLING_TABLE_CELL_MATCHING`` (primary path).
    Pass ``False`` for the hybrid header-only fallback after a primary timeout.
    """
    global _converter_reason
    key = _resolve_cell_matching(cell_matching)
    if _converter_ready.get(key):
        return _converters.get(key)
    with _converter_lock:
        if _converter_ready.get(key):
            return _converters.get(key)
        status = docling_status()
        if not status["ready"]:
            _converters[key] = None
            _converter_reason = status.get("reason")
            logger.warning("Docling final1 unavailable: %s", _converter_reason)
        else:
            try:
                _converters[key] = build_converter(cell_matching=key)
                _converter_reason = None
                logger.info(
                    "Docling + RapidOCR converter ready (models=%s, cell_matching=%s)",
                    status["models_dir"],
                    key,
                )
            except Exception as exc:
                _converters[key] = None
                _converter_reason = f"{type(exc).__name__}: {exc}"
                logger.warning("Docling converter failed to build: %s", exc)
        _converter_ready[key] = True
        return _converters.get(key)


def reset_converters_for_tests() -> None:
    """Clear the converter cache (unit tests only)."""
    global _converter_reason
    with _converter_lock:
        _converters.clear()
        _converter_ready.clear()
        _converter_reason = None


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
                for t in data.get("texts") or []:
                    if not isinstance(t, dict):
                        continue
                    lab = str(t.get("label") or "").strip().casefold().replace(" ", "_")
                    if lab not in header_labels and "section" not in lab and lab != "title":
                        continue
                    text = str(t.get("text") or "").strip()
                    _append(
                        text,
                        1 if lab == "title" else 2,
                        t.get("prov") or [],
                    )
        except Exception as exc:
            logger.debug("dict header extract failed: %s", exc)
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

    Returns ``{markdown, document, content, section_headers}``. ``document``
    (full DoclingDocument dict) is omitted unless ``DOCLING_EXPORT_DOCUMENT=true``.
    ``section_headers`` is extracted then filtered to phrases ≥ the configured
    semantic threshold against known clinical headers (MiniLM / lexical).
    """
    import time

    engine = converter if converter is not None else get_converter()
    if engine is None:
        raise RuntimeError(converter_reason() or "Docling converter unavailable")
    started = time.perf_counter()
    result = engine.convert(str(image_path))
    doc = result.document
    markdown = clean_docling_markdown(doc.export_to_markdown() or "")
    section_headers = extract_section_headers(doc)
    try:
        from stages.lib.imaging.section_header_match import filter_section_headers

        before = len(section_headers)
        section_headers = filter_section_headers(section_headers)
        if before != len(section_headers):
            logger.info(
                "Section headers filtered %d → %d (semantic ≥ threshold)",
                before,
                len(section_headers),
            )
    except Exception as exc:
        logger.warning("Section-header semantic filter skipped: %s", exc)
    section_headers = _renorm_headers_for_image(section_headers, image_path)
    export_doc = (
        os.environ.get("DOCLING_EXPORT_DOCUMENT") or "false"
    ).strip().casefold() in {"1", "true", "yes", "on"}
    document = doc.export_to_dict() if export_doc else None
    elapsed = time.perf_counter() - started
    logger.info(
        "Docling converted %s in %.1fs (chars=%d, headers=%d, document=%s)",
        image_path.name,
        elapsed,
        len(markdown),
        len(section_headers),
        "yes" if document is not None else "skipped",
    )
    return {
        "markdown": markdown,
        "content": markdown,
        "document": document,
        "section_headers": section_headers,
        "elapsed_seconds": elapsed,
    }


def markdown_is_empty(markdown: str) -> bool:
    """True when Docling produced no usable text (only placeholders / whitespace)."""
    import re

    stripped = (markdown or "").strip()
    if not stripped:
        return True
    # Docling emits ``<!-- image -->`` for figures when OCR found no text.
    without_placeholders = re.sub(
        r"<!--\s*image\s*-->", "", stripped, flags=re.IGNORECASE
    )
    without_placeholders = re.sub(r"[|#\-\s]+", "", without_placeholders)
    return len(without_placeholders) < 8


def markdown_is_sparse(markdown: str, *, min_alnum: int = 120) -> bool:
    """True when Docling kept headers/chrome but little body text (empty tables).

    Form pages with TableFormer structure but no cell matching often look like
    a handful of section titles and one address line — enough to skip the
    empty check, not enough for member/DOS. Triggers RapidOCR-onnx fallback.
    """
    import re

    if markdown_is_empty(markdown):
        return True
    # Drop markdown heading markers and table pipes; count real characters.
    body = re.sub(r"^#+\s*", "", markdown or "", flags=re.MULTILINE)
    body = re.sub(r"[|#*`>\-]+", " ", body)
    alnum = re.sub(r"[^A-Za-z0-9]", "", body)
    return len(alnum) < max(8, int(min_alnum))


def convert_image_with_timeout(
    image_path: Path,
    *,
    converter: Any | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """``convert_image`` bounded by a wall-clock timeout.

    On timeout raises ``TimeoutError`` and **does not wait** for the stuck
    worker (``shutdown(wait=False)``). A prior bug used ``with ThreadPoolExecutor``
    which always waits on exit — that is why a page could still burn ~13 minutes
    after a "timeout". The orphan thread may keep using CPU until it finishes;
    callers must fall back to RapidOCR-onnx and continue the chain.
    """
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

    limit = max(1.0, float(timeout_seconds))
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(convert_image, image_path, converter)
        try:
            return fut.result(timeout=limit)
        except FuturesTimeout as exc:
            logger.warning(
                "Docling timeout after %.0fs on %s — abandoning worker, "
                "caller should fall back",
                limit,
                image_path.name,
            )
            raise TimeoutError(
                f"Docling exceeded {limit:.0f}s on {image_path.name}"
            ) from exc
    finally:
        # Critical: wait=False or we block until the 13‑minute convert ends.
        pool.shutdown(wait=False, cancel_futures=True)
