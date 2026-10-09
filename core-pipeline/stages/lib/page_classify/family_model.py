"""Page-family model (TF-IDF + XGBoost).

Weights live in ``PAGE_FAMILY_MODEL_DIR`` (default ``models/page-family/``):
``family.joblib``. The stage names a family when the top probability is at
least 0.50. Otherwise a keyword family whose raw score is above 0.70 is used.
Otherwise a family in both top-3 lists is used. Otherwise the page type is
Others. Keyword rules in
``codeable_canon.json`` name the subtype inside the chosen family, and a page
with no subtype hit keeps the family name as its subtype.

Missing weights, or missing scikit-learn / XGBoost, leave every page unlabeled
here. The stage then chooses the family from the keywords and stamps
``family_source=keywords``.
"""
from __future__ import annotations

import logging
import threading
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_WEIGHTS_NAME = "family.joblib"
# Floors. The saved bundle may name a looser pair; these still apply.
THRESHOLD = 0.50
MARGIN = 0.20
# A keyword family is used, when the model did not commit, if its raw score is above this.
KEYWORD_WIN = 0.70
_lock = threading.Lock()
_bundle: Optional[dict[str, Any]] = None
_failed = False


def _model_dir() -> Path:
    from config import PAGE_FAMILY_MODEL_DIR

    return Path(PAGE_FAMILY_MODEL_DIR)


def _model_file() -> Path:
    return _model_dir() / _WEIGHTS_NAME


def model_status() -> dict[str, Any]:
    """Whether the weights and runtime are present. Does not load the model."""
    path = _model_file()
    if not path.is_file():
        return {"ready": False, "path": str(path), "reason": f"missing {path.name}"}
    missing = [
        name for name in ("sklearn", "xgboost", "joblib") if find_spec(name) is None
    ]
    if missing:
        return {
            "ready": False,
            "path": str(path),
            "reason": "not installed: " + ", ".join(missing),
        }
    return {"ready": True, "path": str(path), "loaded": _bundle is not None}


def decide(
    scores: dict[str, float], threshold: float = THRESHOLD, margin: float = MARGIN
) -> dict[str, Any]:
    """Keep the top family when its probability is at least 0.50."""
    threshold = max(float(threshold), THRESHOLD)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    if not ranked:
        return {"page_family": None, "score": 0.0, "reason": "abstain", "top": []}
    label, top = ranked[0]
    top_names = [
        {"page_family": name, "score": float(score)} for name, score in ranked[:3]
    ]
    if top >= threshold:
        reason = "above threshold"
    else:
        label, reason = None, "abstain"
    return {
        "page_family": label,
        "score": float(top),
        "reason": reason,
        "top": top_names,
    }


def _load() -> Optional[dict[str, Any]]:
    global _bundle, _failed
    if _bundle is not None or _failed:
        return _bundle
    with _lock:
        if _bundle is not None or _failed:
            return _bundle
        status = model_status()
        if not status["ready"]:
            _failed = True
            logger.info("page-family model off — %s", status["reason"])
            return None
        try:
            import joblib

            loaded = joblib.load(_model_file())
            if not isinstance(loaded, dict) or "model" not in loaded:
                raise ValueError("family.joblib has no model")
            _bundle = loaded
        except Exception as exc:
            _failed = True
            logger.warning("page-family model failed to load: %s", exc)
            return None
    return _bundle


def predict_text(text: str) -> dict[str, Any]:
    """One page. ``page_family`` is None when the model abstains or is absent."""
    bundle = _load()
    if bundle is None or not (text or "").strip():
        return {"page_family": None, "score": 0.0, "reason": "abstain"}
    matrix = bundle["vectorizer"].transform([text])
    probabilities = bundle["model"].predict_proba(matrix)[0]
    scores = {
        family: float(score)
        for family, score in zip(bundle["families"], probabilities)
    }
    return decide(scores)


def annotate(pages: list[dict[str, Any]]) -> str:
    """Write ``model_family`` onto pages the model will name.

    Returns ``model`` when the weights ran, ``keywords`` when they did not.
    """
    if _load() is None:
        return "keywords"
    for page in pages:
        decision = predict_text(str(page.get("text") or ""))
        page["model_top"] = list(decision.get("top") or [])
        if decision["page_family"]:
            page["model_family"] = decision["page_family"]
            page["model_confidence"] = decision["score"]
    return "model"
