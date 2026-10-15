"""Blank/junk model: BERT first (KEEP / BLANK / six JUNK sub-types), then
TF-IDF, then regex.

The primary model is the b_jnk DistilBERT page classifier
(``BLANK_JUNK_MODEL_DIR/bert``, 8 labels: KEEP, BLANK, JUNK_INVOICE,
JUNK_COVER_PAGE, JUNK_RECORD_REQUEST, JUNK_INSTRUCTIONS, JUNK_LETTER_FAX,
JUNK_OTHERS). Its decision rules come with it (vendored ``src/models/decision.py``
and ``src/inference/predict.py``, settings in ``default.json`` → ``decision``):
BLANK / JUNK only at ≥ 0.85 for one label (min_flag_confidence 0.70) and KEEP probability ≤ 0.10,
clinical-image / demographic retention safeguards, the invoice rule, the
cover / letter-fax rule and the short "end of patient record" rule (v3; these
three mark a page junk with review whatever the model says, never a clinical
image or a page with note content), and the junk sub-type named by trigger
phrases (the model decides mixed or untriggered
pages). A page the model is unsure about stays KEEP with review.

Fallbacks, each stamped in the reason so a degraded run is visible:
TF-IDF (``tfidf_flat.joblib``) when the BERT folder or torch / transformers is
missing; regex (``regex_fallback:<why>``) when no model loads or prediction
raises. The vendored ``src.*`` modules go on ``sys.path`` only while loading.

Wired like b_jnk v3 ``scripts/infer.py``: a Docling page goes in through
``extract_docling_page`` (text + structure meta), plain OCR text through
``clean_ocr_text``; with BERT loaded, TF-IDF is a junk safety net (a page BERT
keeps but TF-IDF scores p_junk >= ``TFIDF_SAFETY_NET_MIN_P_JUNK`` goes to JUNK
with review, never a clinical image).
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from dataclasses import replace
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
_BERT_DIR_NAME = "bert"


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


# b_jnk v3 infer.py: with BERT loaded, TF-IDF p_junk at or above this moves a
# kept page to JUNK with review. None turns the safety net off. v3 ships 0.80;
# lowered to 0.60 here.
TFIDF_SAFETY_NET_MIN_P_JUNK: float | None = 0.60


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
_safety_net_service: Any = None
# Vendored helpers, bound at load time (``src`` leaves sys.path afterwards).
_extract_docling_page: Any = None
_clean_ocr_text: Any = None
_analyze_protocol: Any = None
_assign_junk_subtype: Any = None
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
    net_on = bert_on and TFIDF_SAFETY_NET_MIN_P_JUNK is not None and model_file.is_file()
    bert_version = f"{version}+bert" + ("+tfidf_net" if net_on else "") if version else version
    status: dict[str, Any] = {
        "ready": False,
        "loaded": _service is not None,
        "path": str(_bert_dir() if bert_on else model_file),
        "model_version": bert_version if bert_on else version,
        "reason": None,
    }
    status["engine"] = "bert" if bert_on else None
    if bert_on:
        status["ready"] = True
        return status
    why_not_bert = (
        f"BERT folder missing at {_bert_dir()}" if not _bert_files_present()
        else "torch/transformers not installed"
    )
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
        status["engine"] = "tfidf"
        status["reason"] = f"{why_not_bert} — TF-IDF fallback"
    return status


def _load_service() -> Any:
    global _service, _safety_net_service, _load_failed, _load_error
    global _extract_docling_page, _clean_ocr_text, _analyze_protocol, _assign_junk_subtype
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
            from src.preprocessing.docling_page import extract_docling_page
            from src.preprocessing.page_subclass import assign_junk_subtype
            from src.preprocessing.protocol import analyze_protocol
            from src.preprocessing.tesseract_txt import clean_ocr_text

            cfg: dict[str, Any] = {}
            config_file = _config_file()
            if config_file.is_file():
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
            decision = DecisionConfig(**(cfg.get("decision") or {}))
            routing = cfg.get("routing") or {}
            base_version = version = str(cfg.get("model_version") or "bjc-ocr")
            min_words = int(routing.get("min_dictionary_words") or 2)
            model = None
            safety_net = None
            # BERT was trained on short sheets and decides them; TF-IDF routes
            # short / unreadable pages first (bert.decide_short_pages). Empty OCR
            # is routed before either model, because there is nothing to score.
            route_short_pages = True
            if _bert_files_present():
                try:
                    from src.models.bert_classifier import BertPageClassifier

                    model = BertPageClassifier.load(_bert_dir())
                    version = f"{version}+bert"
                    route_short_pages = not bool(
                        (cfg.get("bert") or {}).get("decide_short_pages", False)
                    )
                except ImportError as exc:
                    logger.warning(
                        "BERT blank/junk needs torch and transformers (%s); using TF-IDF",
                        exc,
                    )
                    model = None
            if model is not None and TFIDF_SAFETY_NET_MIN_P_JUNK is not None and model_file.is_file():
                safety_net = PageClassifierService(
                    FlatClassifier.load(str(model_file)),
                    model_version=f"{base_version}+tfidf",
                    decision=decision,
                    min_dictionary_words=min_words,
                )
                version = f"{version}+tfidf_net"
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
                min_dictionary_words=min_words,
                route_short_pages=route_short_pages,
            )
            _safety_net_service = safety_net
            _extract_docling_page = extract_docling_page
            _clean_ocr_text = clean_ocr_text
            _analyze_protocol = analyze_protocol
            _assign_junk_subtype = assign_junk_subtype
            logger.info(
                "Blank/junk model loaded version=%s path=%s",
                _service.model_version,
                _bert_dir() if "+bert" in version else model_file,
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


def _model_input(text: str, docling: Any) -> tuple[str, dict[str, Any] | None]:
    """What v3 infer.py feeds the model: Docling page → text + structure meta;
    plain OCR text → cleaned text, no meta."""
    if docling is not None:
        content = _extract_docling_page(docling)
        return content.text, content.to_meta()
    return _clean_ocr_text(text or ""), None


def _safety_net(result: Any, text: str, meta: dict[str, Any] | None) -> Any:
    """BERT kept the page; TF-IDF is sure it is junk. A clinical image is never moved.

    Port of v3 ``scripts/infer.py::_safety_net``.
    """
    second = _safety_net_service.predict_one(result.page_id, text, content_meta=meta)
    # p_junk alone decides: TF-IDF's own JUNK call needs 0.85, which would hide a lower cutoff.
    if second.p_junk is None or second.p_junk < TFIDF_SAFETY_NET_MIN_P_JUNK:
        return result
    if _analyze_protocol(text).retain_clinical_image:
        return result
    subtype = (
        second.subclass if second.flag == "JUNK" and second.subclass
        else _assign_junk_subtype(text).subclass
    )
    return replace(
        result,
        flag="JUNK",
        review_required=True,
        audit_tag=subtype,
        subclass=subtype,
        subclass_reason=second.subclass_reason if subtype == second.subclass else "tfidf_safety_net",
        decision_reason=f"{result.decision_reason}+tfidf_safety_net:{second.p_junk:.3f}",
        top_evidence=f"tfidf_p_junk:{second.p_junk:.3f}",
    )


def classify_page(text: str, *, docling: Any = None) -> tuple[int, str, float | None]:
    """Return ``(code, reason, confidence)`` from the model.

    ``docling`` is the page's Docling object (``{"markdown", "document"}``)
    when the text came from Final OCR 1; the model then reads it the way v3
    does. Regex runs only when the model file is missing or prediction raises.
    """
    service = _load_service()
    if service is None:
        return _regex(text, why="model_unavailable")

    try:
        model_text, meta = _model_input(text, docling)
        result = service.predict_one("page", model_text, content_meta=meta)
        if _safety_net_service is not None and result.flag == "KEEP":
            result = _safety_net(result, model_text, meta)
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


def model_scores(text: str, *, docling: Any = None) -> dict[str, Any]:
    """DistilBERT's and TF-IDF's raw probabilities for one page, for the voters in method.py.

    ``{"bert": {"p_keep", "p_junk", "top_junk_label"} | None, "tfidf": {"p_junk"} | None}``.
    p_junk is every JUNK_* label plus BLANK. A model that is not loaded, or that raises, is
    None: its voter abstains and the verdict records it as unavailable.
    """
    from method import bucket

    out: dict[str, Any] = {"bert": None, "tfidf": None}
    service = _load_service()
    if service is None:
        return out
    try:
        model_text, _meta = _model_input(text, docling)
    except Exception:
        logger.exception("Blank/junk model input failed")
        return out
    if not model_text.strip():
        return out
    bert = service.model if "+bert" in str(service.model_version) else None
    tfidf = _safety_net_service.model if _safety_net_service is not None else (
        None if bert is not None else service.model
    )
    for name, model in (("bert", bert), ("tfidf", tfidf)):
        if model is None:
            continue
        try:
            probs = [float(p) for p in model.predict_proba([model_text])[0]]
        except Exception:
            logger.exception("Blank/junk %s predict failed", name)
            continue
        p_keep, p_junk, top = bucket(list(model.labels), probs)
        out[name] = {"p_keep": p_keep, "p_junk": p_junk, "top_junk_label": top}
    return out
