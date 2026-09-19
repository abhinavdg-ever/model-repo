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
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_converter_lock = threading.Lock()
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

    models = model_paths(models_dir)
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = True
    pipeline_options.do_table_structure = True
    pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE
    pipeline_options.table_structure_options.do_cell_matching = True
    pipeline_options.ocr_options = _build_rapidocr_options(models)
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


def convert_image(image_path: Path, converter: Any | None = None) -> dict[str, Any]:
    """Run Docling on one page image.

    Returns ``{markdown, document, content}`` where ``content`` is the markdown
    (plain text the rest of the pipeline reads) and ``document`` is the full
    DoclingDocument dict (layout / bbox / tables / reading order).
    """
    engine = converter if converter is not None else get_converter()
    if engine is None:
        raise RuntimeError(converter_reason() or "Docling converter unavailable")
    result = engine.convert(str(image_path))
    doc = result.document
    markdown = doc.export_to_markdown() or ""
    document = doc.export_to_dict()
    return {
        "markdown": markdown,
        "content": markdown,
        "document": document,
    }
