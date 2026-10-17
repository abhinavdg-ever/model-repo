"""Blank/junk model: DistilBERT's raw probabilities for the vote in method.py.

The model is the b_jnk DistilBERT page classifier (``BLANK_JUNK_MODEL_DIR/bert``,
8 labels: KEEP, BLANK, JUNK_INVOICE, JUNK_COVER_PAGE, JUNK_RECORD_REQUEST,
JUNK_INSTRUCTIONS, JUNK_LETTER_FAX, JUNK_OTHERS). method.py turns its softmax into a
ballot and a confidence; nothing here decides a page.

Without the BERT folder or torch / transformers there is no model: its voter abstains,
the ballot says ``unavailable`` and the vote runs on the keyword canon alone. /health
reports ``engine: keywords`` so a degraded run is visible. TF-IDF
(``tfidf_flat.joblib``) is no longer read.

Wired like b_jnk v3 ``scripts/infer.py``: a Docling page goes in through
``extract_docling_page``, plain OCR text through ``clean_ocr_text``. The vendored
``src.*`` modules go on ``sys.path`` only while loading.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from importlib.util import find_spec
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_VENDOR = Path(__file__).resolve().parent / "model"
_CONFIG_NAME = "default.json"
_BERT_DIR_NAME = "bert"


def _model_dir() -> Path:
    """Directory from ``BLANK_JUNK_MODEL_DIR`` (``.env``), resolved like the other weights."""
    from config import BLANK_JUNK_MODEL_DIR

    return Path(BLANK_JUNK_MODEL_DIR)


def _config_file() -> Path:
    return _model_dir() / _CONFIG_NAME


def _bert_dir() -> Path:
    return _model_dir() / _BERT_DIR_NAME


def _bert_files_present() -> bool:
    directory = _bert_dir()
    return (directory / "bjnk_meta.json").is_file() and (directory / "config.json").is_file()


def _bert_runtime_installed() -> bool:
    return find_spec("torch") is not None and find_spec("transformers") is not None


_lock = threading.Lock()
_model: Any = None
_model_version: str | None = None
# Vendored helpers, bound at load time (``src`` leaves sys.path afterwards).
_extract_docling_page: Any = None
_clean_ocr_text: Any = None
_load_failed = False
_load_error: str | None = None


def _model_version_from_config() -> str | None:
    try:
        cfg = json.loads(_config_file().read_text(encoding="utf-8"))
        return str(cfg.get("model_version") or "") or None
    except (OSError, ValueError):
        return None


def model_status() -> dict[str, Any]:
    """For ``/health``: configuration only, never loads the model (≈4 s)."""
    version = _model_version_from_config()
    status: dict[str, Any] = {
        "ready": False,
        "loaded": _model is not None,
        "path": str(_bert_dir()),
        "model_version": f"{version}+bert" if version else None,
        "engine": "keywords",
        "reason": None,
    }
    if not _bert_files_present():
        status["reason"] = f"BERT folder missing at {_bert_dir()} — the vote runs on keywords alone"
    elif not _bert_runtime_installed():
        status["reason"] = "torch/transformers not installed — the vote runs on keywords alone"
    elif _load_failed:
        status["reason"] = f"model failed to load — the vote runs on keywords alone: {_load_error}"
    else:
        status["ready"] = True
        status["engine"] = "bert"
    return status


def _load_model() -> Any:
    global _model, _model_version, _load_failed, _load_error
    global _extract_docling_page, _clean_ocr_text
    if _model is not None or _load_failed:
        return _model
    with _lock:
        if _model is not None or _load_failed:
            return _model
        if not _bert_files_present():
            logger.warning("Blank/junk BERT missing at %s — keywords only", _bert_dir())
            _load_failed = True
            _load_error = "BERT folder missing"
            return None
        vendor = str(_VENDOR)
        added = vendor not in sys.path
        if added:
            sys.path.insert(0, vendor)
        try:
            from src.models.bert_classifier import BertPageClassifier
            from src.preprocessing.docling_page import extract_docling_page
            from src.preprocessing.tesseract_txt import clean_ocr_text

            model = BertPageClassifier.load(_bert_dir())
            _model_version = f"{_model_version_from_config() or 'bjc-ocr'}+bert"
            _extract_docling_page = extract_docling_page
            _clean_ocr_text = clean_ocr_text
            _model = model
            logger.info("Blank/junk model loaded version=%s path=%s", _model_version, _bert_dir())
        except Exception as exc:
            logger.warning("Blank/junk BERT failed to load — keywords only: %s", exc)
            _load_failed = True
            _load_error = str(exc)
            _model = None
        finally:
            # The vendored package is named ``src`` — too generic to leave on
            # the import path. Everything it needs is imported by now.
            if added:
                try:
                    sys.path.remove(vendor)
                except ValueError:
                    pass
        return _model


def _model_input(text: str, docling: Any) -> str:
    """What v3 infer.py feeds the model: a Docling page's text, else cleaned OCR text."""
    if docling is not None:
        return _extract_docling_page(docling).text
    return _clean_ocr_text(text or "")


def model_scores(text: str, *, docling: Any = None) -> dict[str, Any]:
    """DistilBERT's raw probabilities for one page, for the voters in method.py.

    ``{"bert": {"p_keep", "p_junk", "top_junk_label"} | None}``. p_junk is every JUNK_*
    label plus BLANK. A model that is not loaded, or that raises, is None: its voter
    abstains and the verdict records it as unavailable.
    """
    from method import bucket

    out: dict[str, Any] = {"bert": None}
    model = _load_model()
    if model is None:
        return out
    try:
        model_text = _model_input(text, docling)
        if not model_text.strip():
            return out
        probs = [float(p) for p in model.predict_proba([model_text])[0]]
    except Exception:
        logger.exception("Blank/junk BERT predict failed")
        return out
    p_keep, p_junk, top = bucket(list(model.labels), probs)
    out["bert"] = {"p_keep": p_keep, "p_junk": p_junk, "top_junk_label": top}
    return out
