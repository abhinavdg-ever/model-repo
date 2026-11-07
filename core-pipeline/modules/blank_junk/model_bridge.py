"""Blank/junk model: DistilBERT's raw probabilities for the vote in method.py.

The model is the b_jnk DistilBERT page classifier (``BLANK_JUNK_MODEL_DIR/bert``,
8 labels: KEEP, BLANK, JUNK_INVOICE, JUNK_COVER_PAGE, JUNK_RECORD_REQUEST,
JUNK_INSTRUCTIONS, JUNK_LETTER_FAX, JUNK_OTHERS). method.py turns its softmax into a
ballot and a confidence; nothing here decides a page.

Without the BERT folder or torch / transformers there is no model: its voter abstains,
the ballot says ``unavailable`` and the vote runs on the keyword canon alone. /health
reports ``engine: keywords`` so a degraded run is visible.

Wired like b_jnk v3 ``scripts/infer.py``: a Docling page goes in through
``extract_docling_page``, plain OCR text through ``clean_ocr_text``, and the page is
judged by v3's ``PageClassifierService`` (2026-11-03, user: the teammate's routing,
retention, keep veto, thresholds and word rules), configured from the vendored
``model/configs/default.json``. The vendored ``src.*`` modules go on ``sys.path`` only
while loading.
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
    from common.config import BLANK_JUNK_MODEL_DIR

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
_infer_lock = threading.Lock()
_model: Any = None
_service: Any = None
_model_version: str | None = None
# Vendored helpers, bound at load time (``src`` leaves sys.path afterwards).
_extract_docling_page: Any = None
_clean_ocr_text: Any = None
_load_failed = False
_load_error: str | None = None


# The decision settings travel with the code (in git), so every host judges alike.
_SERVICE_CONFIG = _VENDOR / "configs" / "default.json"


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
    global _model, _service, _model_version, _load_failed, _load_error
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
            from src.inference.predict import PageClassifierService
            from src.models.decision import DecisionConfig

            model = BertPageClassifier.load(_bert_dir())
            cfg = json.loads(_SERVICE_CONFIG.read_text(encoding="utf-8"))
            _model_version = f"{cfg.get('model_version') or _model_version_from_config() or 'bjc-ocr'}+bert"
            # As scripts/infer.py builds it.
            _service = PageClassifierService(
                model,
                model_version=_model_version,
                decision=DecisionConfig(**cfg["decision"]),
                min_dictionary_words=cfg["routing"]["min_dictionary_words"],
                route_short_pages=not cfg.get("bert", {}).get("decide_short_pages", False),
            )
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


# 2026-11-04 (user): a lab results table ("Oxygen Saturation 99 % 90- 100% <date>") is not an
# index: v3's admin rule (contents_page) reads "label + range" / "label + date" rows as index
# entries. Any admin-rule junk on a page with lab results is overruled to KEEP.
import re as _re

_LAB_RESULT = _re.compile(
    r"(?<![a-z])(?:reference\s+(?:range|interval)|ref\.?\s+range|normal\s+range)(?![a-z])"
    r"|\b\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\s*%"
    r"|\b\d+(?:\.\d+)?\s?(?:mg/dl|g/dl|mmol/l|meq/l|u/l|iu/l|uiu/ml|ng/ml|pg/ml|k/ul|m/ul|fl|pg)\b",
    _re.IGNORECASE,
)


def lab_result_signs(text: str) -> bool:
    """Lab results on the page: a reference range / interval, lab units, or a 'n - m %' range."""
    return bool(_LAB_RESULT.search(text or ""))


# 2026-11-06 (user): a problem / medication / immunization list ("Anemia  Active  11/06/2025")
# is not an index or an empty header either. When the admin rule says contents_page or header_only
# (a short list's "name  date" rows read as header lines), any of these keeps the page:
# a status column (Active / Inactive / Resolved / Completed / Chronic / Historical / Discontinued)
# on 3+ lines, an ICD-10 code, a medication dose, or a list heading (Problem List, Diagnoses ...).
_STATUS_LINE = _re.compile(
    r"(?<![A-Za-z])(?:active|inactive|resolved|completed|chronic|historical|discontinued)(?![A-Za-z])",
    _re.IGNORECASE,
)
_CLINICAL_LIST = _re.compile(
    r"\b[A-TV-Z]\d{2}\.\d{1,4}\b"
    r"|\bicd-?10\b"
    r"|\b\d+(?:\.\d+)?\s?(?:mg|mcg|ml|units?)\b"
    r"|(?<![A-Za-z])(?:problem\s+list|problems|diagnos[ie]s|medications?|immunizations?|allergies"
    r"|surgical\s+history|medical\s+history|procedures)(?![A-Za-z])",
    _re.IGNORECASE,
)


def clinical_list_signs(text: str) -> bool:
    """A clinical list read as an index: 3+ lines with a status word, or a code, dose or list heading."""
    status_lines = sum(1 for line in (text or "").splitlines() if _STATUS_LINE.search(line))
    return status_lines >= 3 or bool(_CLINICAL_LIST.search(text or ""))


def _model_input(text: str, docling: Any) -> tuple[str, dict[str, Any] | None]:
    """What v3 infer.py feeds the service: a Docling page's text and its structure,
    else cleaned OCR text with no structure."""
    if docling is not None:
        content = _extract_docling_page(docling)
        return content.text, content.to_meta()
    return _clean_ocr_text(text or ""), None


def model_scores(text: str, *, docling: Any = None) -> dict[str, Any]:
    """DistilBERT's raw probabilities for one page, for the voters in method.py.

    ``{"bert": {"p_keep", "p_junk", "p_blank", "top_junk_label", "service"} | None}``. p_junk is
    the JUNK_* labels only, BLANK apart in p_blank (2026-11-04, user); ``service`` is v3's
    ``PageClassifierService`` verdict as infer.py writes it: flag, subclass, confidence,
    review_required, decision_reason, and p_keep / p_blank / p_junk with JUNK alone.
    A page the service routes before the model (empty or unreadable OCR) has no
    probabilities. A model that is not loaded, or that raises, is None: its voter
    abstains and the verdict records it as unavailable.
    """
    from method import blank_probability, bucket

    out: dict[str, Any] = {"bert": None}
    model = _load_model()
    if model is None:
        return out
    try:
        model_text, meta = _model_input(text, docling)
        if not model_text.strip():
            return out
        # One call at a time: the shared tokenizer is not safe across threads (two
        # charts' blank / junk passes at once; 2026-11-01).
        with _infer_lock:
            result = _service.predict_one("page", model_text, content_meta=meta) if _service is not None else None
            if result is not None and result.p_keep is not None:
                p_blank = float(result.p_blank or 0.0)
                p_keep, p_junk = float(result.p_keep), float(result.p_junk or 0.0)
                # The service's junk subtype (a KEEP page has none, and needs none).
                top = result.subclass if (result.subclass or "").startswith("JUNK_") else ""
            else:
                # Routed before the model (or no service): the model's own numbers still
                # feed R4 / R6 and blank_junk.json.
                probs = [float(p) for p in model.predict_proba([model_text])[0]]
                p_keep, p_junk, top = bucket(list(model.labels), probs)
                p_blank = blank_probability(list(model.labels), probs)
    except Exception:
        logger.exception("Blank/junk BERT predict failed")
        return out
    out["bert"] = {"p_keep": p_keep, "p_junk": p_junk, "p_blank": p_blank, "top_junk_label": top, "service": None}
    admin = result is not None and result.flag == "JUNK" and "admin_rule" in (result.decision_reason or "")
    why = ("lab_results" if admin and lab_result_signs(model_text)
           else "clinical_list" if admin and _re.search(r"admin_rule:(?:contents_page|header_only)",
                                                         result.decision_reason or "")
           and clinical_list_signs(model_text) else "")
    if why:
        from dataclasses import replace

        result = replace(
            result, flag="KEEP", subclass="", review_required=False,
            confidence=float(result.p_keep if result.p_keep is not None else result.confidence),
            decision_reason=f"{result.decision_reason}+overruled:{why}",
        )
        top = ""
    if result is not None:
        out["bert"]["service"] = {
            "flag": result.flag,
            "subclass": result.subclass or "",
            "confidence": float(result.confidence),
            "review_required": bool(result.review_required),
            "decision_reason": result.decision_reason or "",
            "audit_tag": result.audit_tag or "",
            "p_keep": result.p_keep,
            "p_blank": result.p_blank,
            "p_junk": result.p_junk,
        }
    return out
