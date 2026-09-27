"""TF-IDF blank/junk model (KEEP / BLANK / JUNK), regex as fallback.

The joblib checkpoint unpickles classes under ``src.*``. Those modules are
vendored at ``stages/lib/blank_junk/model/`` and put on ``sys.path`` only
while loading. Weights are the directory in ``BLANK_JUNK_MODEL_DIR``
(default ``models/blank-junk/`` next to the other model paths in ``.env``).

Who decides what:

* **Blank / junk / keep** — the model. When it is missing, fails, or marks
  the page for review, the regex rules in ``classify.classify_text`` decide,
  and the reason is stamped ``regex_fallback:<why>`` so a degraded run is
  visible in ``blank_junk_classification.reason``.
* **Which junk** (Cover Page, Invoice, Instructions, …) — after the model says
  JUNK, the regex classifier names the subtype. The model's own audit tags
  only cover a few junk types, so without this Cover Page and Instructions
  would never appear. If the regex finds no junk subtype, the model's audit
  tag is mapped instead (generic → Others).
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
    CODE_INVOICE,
    CODE_LETTER_FAX,
    CODE_MAIN,
    CODE_OTHERS,
    CODE_RECORD_REQUEST,
    JUNK_CODES,
    classify_text,
)

logger = logging.getLogger(__name__)

_VENDOR = Path(__file__).resolve().parent / "model"
_WEIGHTS_NAME = "tfidf_flat.joblib"
_CONFIG_NAME = "default.json"


def _model_dir() -> Path:
    """Directory from ``BLANK_JUNK_MODEL_DIR`` (``.env``), resolved like the other weights."""
    from config import BLANK_JUNK_MODEL_DIR

    return Path(BLANK_JUNK_MODEL_DIR)


def _model_file() -> Path:
    return _model_dir() / _WEIGHTS_NAME


def _config_file() -> Path:
    return _model_dir() / _CONFIG_NAME

# Model audit tags → schema junk_subtype codes, used only when the regex
# classifier finds no junk subtype of its own.
_AUDIT_CODE = {
    "JUNK_COVER_REQUEST": CODE_RECORD_REQUEST,
    "JUNK_FAX_TRANSMISSION": CODE_LETTER_FAX,
    "JUNK_SEPARATOR_BARCODE": CODE_OTHERS,
    "JUNK_PRINTER_SYSTEM_TEST": CODE_OTHERS,
    "JUNK_POSTAL_MAIL": CODE_OTHERS,
    "JUNK_INSURANCE_ID": CODE_INVOICE,
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
    status: dict[str, Any] = {
        "ready": False,
        "loaded": _service is not None,
        "path": str(model_file),
        "model_version": _model_version_from_config(),
        "reason": None,
    }
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
        if not model_file.is_file():
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
            model = FlatClassifier.load(str(model_file))
            _service = PageClassifierService(
                model,
                model_version=str(cfg.get("model_version") or "bjc-ocr"),
                decision=decision,
                min_dictionary_words=int(routing.get("min_dictionary_words") or 2),
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


def _junk_subtype(text: str, audit_tag: str | None) -> tuple[int, str]:
    """Subtype for a page the model called JUNK: regex first, then audit tag."""
    code, reason = classify_text(text)
    if code in JUNK_CODES and code != CODE_BLANK:
        return code, f"subtype:regex:{reason}"
    if audit_tag and audit_tag in _AUDIT_CODE and _AUDIT_CODE[audit_tag] != CODE_BLANK:
        return _AUDIT_CODE[audit_tag], f"subtype:audit:{audit_tag}"
    # Regex found no junk type (possibly "clinical_content") — keep the
    # model's verdict but record the disagreement for QC.
    return CODE_OTHERS, f"subtype:default:regex_said_{reason or 'main'}"


def classify_page(text: str) -> tuple[int, str, float | None]:
    """Return ``(code, reason, confidence)``.

    Confidence is the model score when the model decides, else None so the
    caller keeps the regex default.
    """
    service = _load_service()
    if service is None:
        return _regex(text, why="model_unavailable")

    try:
        result = service.predict_one("page", text or "", with_evidence=False)
    except Exception:
        logger.exception("Blank/junk model predict failed — regex fallback")
        return _regex(text, why="predict_error")

    if result.review_required:
        return _regex(text, why=result.decision_reason or "review")

    flag = str(result.flag or "").upper()
    conf = float(result.confidence) if result.confidence is not None else None
    reason = f"model:{result.model_version}:{result.decision_reason}"
    if result.audit_tag:
        reason = f"{reason}:{result.audit_tag}"

    if flag == "BLANK":
        return CODE_BLANK, reason, conf
    if flag == "JUNK":
        code, subtype_reason = _junk_subtype(text, result.audit_tag)
        return code, f"{reason}:{subtype_reason}", conf
    if flag == "KEEP":
        # Model is conservative (KEEP on thin text). Declared/empty blanks
        # stay with the regex rules — those are high-precision and the model
        # often still labels them KEEP.
        blank_code, blank_reason = classify_text(text)
        if blank_code == CODE_BLANK:
            return (
                CODE_BLANK,
                f"regex_fallback:blank_after_model_keep:{blank_reason}",
                None,
            )
        return CODE_MAIN, reason, conf
    return _regex(text, why=f"unknown_flag:{flag}")
