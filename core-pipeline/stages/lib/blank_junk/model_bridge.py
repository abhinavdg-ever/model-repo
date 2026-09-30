"""TF-IDF blank/junk model (KEEP / BLANK / JUNK), regex as fallback.

The joblib checkpoint unpickles classes under ``src.*``. Those modules are
vendored at ``stages/lib/blank_junk/model/`` and put on ``sys.path`` only
while loading. Weights are the directory in ``BLANK_JUNK_MODEL_DIR``
(default ``models/blank-junk/`` next to the other model paths in ``.env``).

Who decides what:

* **Blank / junk / keep and the junk subtype** — the model. Regex runs only
  when the model file is missing or prediction raises, and that reason is
  stamped ``regex_fallback:<why>``.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from classify import (
    CODE_BLANK,
    CODE_COVER_PAGE,
    CODE_INSTRUCTIONS,
    CODE_INVOICE,
    CODE_LETTER_FAX,
    CODE_MAIN,
    CODE_OTHERS,
    CODE_RECORD_REQUEST,
    classify_text,
)

logger = logging.getLogger(__name__)

_VENDOR = Path(__file__).resolve().parent / "model"
_WEIGHTS_NAME = "tfidf_flat.joblib"
_CONFIG_NAME = "default.json"
_BERT_DIR_NAME = "bert_page"


def _model_dir() -> Path:
    """Directory from ``BLANK_JUNK_MODEL_DIR`` (``.env``), resolved like the other weights."""
    from config import BLANK_JUNK_MODEL_DIR

    return Path(BLANK_JUNK_MODEL_DIR)


def _model_file() -> Path:
    return _model_dir() / _WEIGHTS_NAME


def _config_file() -> Path:
    return _model_dir() / _CONFIG_NAME


def _bert_dir() -> Path:
    return _model_dir() / _BERT_DIR_NAME


def _bert_files_present() -> bool:
    directory = _bert_dir()
    return (directory / "bjnk_meta.json").is_file() and (directory / "config.json").is_file()


def _bert_runtime_installed() -> bool:
    return find_spec("torch") is not None and find_spec("transformers") is not None

# Model fine labels → schema junk_subtype. The model's label is the subtype.
_AUDIT_CODE = {
    "JUNK_COVER_REQUEST": CODE_RECORD_REQUEST,
    "JUNK_FAX_TRANSMISSION": CODE_LETTER_FAX,
    "JUNK_SEPARATOR_BARCODE": CODE_OTHERS,
    "JUNK_PRINTER_SYSTEM_TEST": CODE_OTHERS,
    "JUNK_POSTAL_MAIL": CODE_OTHERS,
    "JUNK_INSURANCE_ID": CODE_INVOICE,
    "JUNK_INVOICE": CODE_INVOICE,
    "JUNK_COVER_PAGE": CODE_COVER_PAGE,
    "JUNK_RECORD_REQUEST": CODE_RECORD_REQUEST,
    "JUNK_INSTRUCTIONS": CODE_INSTRUCTIONS,
    "JUNK_LETTER_FAX": CODE_LETTER_FAX,
    "JUNK_OTHERS": CODE_OTHERS,
    "JUNK_BLACK_SCAN_DEFECT": CODE_OTHERS,
    "JUNK_NON_CLINICAL_PHOTO": CODE_OTHERS,
    "BLANK_SYSTEM": CODE_BLANK,
    "BLANK_ABSOLUTE": CODE_BLANK,
    "BLANK_TECHNICAL": CODE_BLANK,
    "BLANK_HEADER_FOOTER_ONLY": CODE_BLANK,
}

_lock = threading.Lock()
_service: Any = None
_load_failed = False
_load_error: str | None = None


def model_available() -> bool:
    return _model_file().is_file()


def _model_version_from_config() -> str | None:
    try:
        cfg = json.loads(_config_file().read_text(encoding="utf-8"))
        return str(cfg.get("model_version") or "") or None
    except (OSError, ValueError):
        return None


def model_status() -> dict[str, Any]:
    """For ``/health``: configuration only, never loads the model (≈4 s)."""
    model_file = _model_file()
    bert_on = _bert_files_present() and _bert_runtime_installed()
    version = _model_version_from_config()
    status: dict[str, Any] = {
        "ready": False,
        "loaded": _service is not None,
        "path": str(_bert_dir() if bert_on else model_file),
        "model_version": f"{version}+bert" if bert_on and version else version,
        "reason": None,
    }
    if bert_on:
        status["ready"] = True
        return status
    if not model_file.is_file():
        status["reason"] = (
            f"model file missing at {model_file} — blank/junk runs on regex rules only"
        )
    elif find_spec("sklearn") is None or find_spec("joblib") is None:
        status["reason"] = "scikit-learn/joblib not installed — regex rules only"
    elif _load_failed:
        status["reason"] = f"model failed to load — regex rules only: {_load_error}"
    else:
        status["ready"] = True
    return status


def _load_service() -> Any:
    global _service, _load_failed, _load_error
    if _service is not None or _load_failed:
        return _service
    with _lock:
        if _service is not None or _load_failed:
            return _service
        model_file = _model_file()
        if not model_file.is_file() and not _bert_files_present():
            logger.warning(
                "Blank/junk model missing at %s — regex fallback only",
                model_file,
            )
            _load_failed = True
            _load_error = "model file missing"
            return None
        vendor = str(_VENDOR)
        added = vendor not in sys.path
        if added:
            sys.path.insert(0, vendor)
        try:
            from src.inference.predict import PageClassifierService
            from src.models.classifiers import FlatClassifier
            from src.models.decision import DecisionConfig

            cfg: dict[str, Any] = {}
            config_file = _config_file()
            if config_file.is_file():
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
            decision = DecisionConfig(**(cfg.get("decision") or {}))
            routing = cfg.get("routing") or {}
            version = str(cfg.get("model_version") or "bjc-ocr")
            model = None
            # Every page with text is scored by the model. Empty OCR is still
            # routed as blank before the model, because there is nothing to score.
            route_short_pages = False
            if _bert_files_present():
                try:
                    from src.models.bert_classifier import BertPageClassifier

                    model = BertPageClassifier.load(_bert_dir())
                    version = f"{version}+bert"
                except ImportError as exc:
                    logger.warning(
                        "BERT blank/junk needs torch and transformers (%s); using TF-IDF",
                        exc,
                    )
                    model = None
            if model is None:
                if not model_file.is_file():
                    logger.warning(
                        "Blank/junk model missing at %s — regex fallback only",
                        model_file,
                    )
                    _load_failed = True
                    _load_error = "model file missing"
                    return None
                model = FlatClassifier.load(str(model_file))
                version = f"{version}+tfidf"
            _service = PageClassifierService(
                model,
                model_version=version,
                decision=decision,
                min_dictionary_words=int(routing.get("min_dictionary_words") or 2),
                route_short_pages=route_short_pages,
            )
            logger.info(
                "Blank/junk model loaded version=%s path=%s",
                _service.model_version,
                model_file,
            )
        except Exception as exc:
            logger.exception(
                "Blank/junk model failed to load — regex fallback only"
            )
            _load_failed = True
            _load_error = str(exc)
            _service = None
        finally:
            # The vendored package is named ``src`` — too generic to leave on
            # the import path. Everything it needs is imported by now.
            if added:
                try:
                    sys.path.remove(vendor)
                except ValueError:
                    pass
        return _service


def _regex(text: str, *, why: str) -> tuple[int, str, float | None]:
    code, reason = classify_text(text)
    tag = f"regex_fallback:{why}"
    if reason:
        tag = f"{tag}:{reason}"
    return code, tag, None


def _junk_subtype(model_label: str | None) -> tuple[int, str]:
    """Subtype for a page the model called JUNK. The model's own label wins."""
    code = _AUDIT_CODE.get(model_label or "")
    if code is not None and code != CODE_BLANK:
        return code, f"subtype:model:{model_label}"
    return CODE_OTHERS, f"subtype:model:{model_label or 'JUNK'}"


def classify_page(text: str) -> tuple[int, str, float | None]:
    """Return ``(code, reason, confidence)`` from the model.

    Regex runs only when the model file is missing or prediction raises.
    """
    service = _load_service()
    if service is None:
        return _regex(text, why="model_unavailable")

    try:
        result = service.predict_one("page", text or "")
    except Exception:
        logger.exception("Blank/junk model predict failed — regex fallback")
        return _regex(text, why="predict_error")

    flag = str(result.flag or "").upper()
    conf = float(result.confidence) if result.confidence is not None else None
    reason = f"model:{result.model_version}:{result.decision_reason}"
    if result.audit_tag:
        reason = f"{reason}:{result.audit_tag}"

    if flag == "BLANK":
        return CODE_BLANK, reason, conf
    if flag == "JUNK":
        code, subtype_reason = _junk_subtype(result.subclass or result.audit_tag)
        return code, f"{reason}:{subtype_reason}", conf
    if flag == "KEEP":
        return CODE_MAIN, reason, conf
    return _regex(text, why=f"unknown_flag:{flag}")
