"""Semantic filter for Final1 ``section_headers`` written into ``*_final1.json``.

Docling (or heuristics) propose heading candidates. Only candidates whose text
is ≥ ``SECTION_HEADER_SEMANTIC_THRESHOLD`` similar to a known clinical section
header are kept — using MiniLM embeddings when ``sentence-transformers`` is
installed, otherwise a normalized lexical ratio (tests / degraded path).

Each kept header gains:
  ``match_score``       — cosine / lexical similarity in [0, 1]
  ``matched_canonical`` — the catalog phrase it matched
"""
from __future__ import annotations

import json
import logging
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CANON_PATH = Path(__file__).with_name("section_header_canon.json")

_lock = threading.Lock()
_model: Any = None
_model_tried = False
_model_reason: Optional[str] = None
_canon: list[str] = []
_canon_norm: list[str] = []
_canon_embeddings: Any = None  # np.ndarray | None


def _normalize(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[:*\-–—|/]+$", "", t)
    t = re.sub(r"[^\w\s/+]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def load_canonical_headers(path: Path | None = None) -> list[str]:
    """Load the editable catalog of clinical section-header phrases."""
    p = path or _CANON_PATH
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"canonical headers must be a JSON list: {p}")
    out = [str(x).strip() for x in raw if str(x).strip()]
    # Dedupe preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for h in out:
        key = _normalize(h)
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(h)
    return uniq


def _ensure_catalog() -> None:
    global _canon, _canon_norm
    if _canon:
        return
    with _lock:
        if _canon:
            return
        _canon = load_canonical_headers()
        _canon_norm = [_normalize(h) for h in _canon]


def _get_model() -> Any:
    """Lazy-load MiniLM once. Returns None when unavailable."""
    global _model, _model_tried, _model_reason, _canon_embeddings
    if _model_tried:
        return _model
    with _lock:
        if _model_tried:
            return _model
        _model_tried = True
        _ensure_catalog()
        try:
            from sentence_transformers import SentenceTransformer

            from config import SECTION_HEADER_MINILM_MODEL

            model_id = SECTION_HEADER_MINILM_MODEL
            logger.info("Loading section-header MiniLM: %s", model_id)
            _model = SentenceTransformer(model_id)
            _canon_embeddings = _model.encode(
                _canon,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            _model_reason = None
            logger.info(
                "Section-header MiniLM ready (%d canonical phrases)", len(_canon)
            )
        except Exception as exc:
            _model = None
            _canon_embeddings = None
            _model_reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Section-header MiniLM unavailable (%s); using lexical fallback",
                _model_reason,
            )
        return _model


def minilm_ready() -> bool:
    return _get_model() is not None


def minilm_reason() -> Optional[str]:
    _get_model()
    return _model_reason


def _lexical_score(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # Containment bonus for short abbreviations inside longer labels
    if a in b or b in a:
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        if len(shorter) >= 3 and len(shorter) / max(len(longer), 1) >= 0.35:
            return max(0.92, SequenceMatcher(None, a, b).ratio())
    return SequenceMatcher(None, a, b).ratio()


def _best_lexical(text_norm: str) -> tuple[float, str]:
    _ensure_catalog()
    best_score = 0.0
    best_label = ""
    for label, label_norm in zip(_canon, _canon_norm):
        score = _lexical_score(text_norm, label_norm)
        if score > best_score:
            best_score = score
            best_label = label
    return best_score, best_label


def _best_minilm(text: str) -> tuple[float, str]:
    import numpy as np

    model = _get_model()
    if model is None or _canon_embeddings is None:
        return _best_lexical(_normalize(text))
    emb = model.encode([text], normalize_embeddings=True, show_progress_bar=False)
    # Cosine with L2-normalized vectors = dot product
    scores = np.asarray(_canon_embeddings) @ np.asarray(emb[0])
    idx = int(np.argmax(scores))
    return float(scores[idx]), _canon[idx]


def best_header_match(text: str) -> tuple[float, str]:
    """Return (score, canonical_label) for ``text`` against the catalog."""
    raw = (text or "").strip()
    if not raw:
        return 0.0, ""
    norm = _normalize(raw)
    if not norm:
        return 0.0, ""
    # Exact / near-exact lexical short-circuit (cheap, deterministic)
    lex_score, lex_label = _best_lexical(norm)
    if lex_score >= 0.98:
        return lex_score, lex_label
    if _get_model() is not None:
        return _best_minilm(raw)
    return lex_score, lex_label


def filter_section_headers(
    candidates: list[dict[str, Any]],
    *,
    threshold: float | None = None,
    enabled: bool | None = None,
) -> list[dict[str, Any]]:
    """Keep only candidates that semantically match a canonical header.

    When filtering is disabled, candidates are returned unchanged (no scores).
    When enabled, each kept item is annotated with ``match_score`` and
    ``matched_canonical``.
    """
    from config import (
        SECTION_HEADER_SEMANTIC_ENABLED,
        SECTION_HEADER_SEMANTIC_THRESHOLD,
    )

    if enabled is None:
        enabled = SECTION_HEADER_SEMANTIC_ENABLED
    if not enabled:
        return list(candidates)
    if threshold is None:
        threshold = SECTION_HEADER_SEMANTIC_THRESHOLD
    threshold = float(threshold)

    kept: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        score, canonical = best_header_match(text)
        if score < threshold:
            continue
        enriched = dict(item)
        enriched["match_score"] = round(float(score), 4)
        enriched["matched_canonical"] = canonical
        kept.append(enriched)
    return kept
