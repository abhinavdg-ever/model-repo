"""Semantic similarity of short labels, for the accuracy report's page-type match.

MiniLM (``core-pipeline/models/semantic-model``, the model the section-header
matcher uses) run through ONNX Runtime: tokens -> mean pooling over the
attention mask -> L2 normalise -> cosine. Optional: without ``onnxruntime`` /
``tokenizers`` or the model folder, ``similarity`` returns None and the report
falls back to the exact / substring / accepted-label match.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from threading import Lock
from typing import Any, Optional

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_lock = Lock()
_model: Optional[tuple[Any, Any]] = None
_failed = False


def _load() -> Optional[tuple[Any, Any]]:
    global _model, _failed
    if _model is not None or _failed:
        return _model
    with _lock:
        if _model is not None or _failed:
            return _model
        path = get_settings().resolved_semantic_model_path
        try:
            if path is None:
                raise FileNotFoundError("semantic model folder not found")
            import onnxruntime  # noqa: PLC0415
            from tokenizers import Tokenizer  # noqa: PLC0415

            tokenizer = Tokenizer.from_file(str(path / "tokenizer.json"))
            tokenizer.enable_truncation(max_length=64)
            session = onnxruntime.InferenceSession(
                str(path / "onnx" / "model.onnx"), providers=["CPUExecutionProvider"]
            )
            _model = (tokenizer, session)
        except Exception as exc:  # optional feature: never break the report
            _failed = True
            logger.info("semantic page-type match off: %s", exc)
    return _model


def available() -> bool:
    return _load() is not None


@lru_cache(maxsize=4096)
def _embed(text: str) -> Optional[tuple[float, ...]]:
    model = _load()
    if model is None:
        return None
    import numpy as np  # noqa: PLC0415

    tokenizer, session = model
    encoding = tokenizer.encode(text)
    ids = np.array([encoding.ids], dtype=np.int64)
    mask = np.array([encoding.attention_mask], dtype=np.int64)
    feeds = {"input_ids": ids, "attention_mask": mask}
    if any(i.name == "token_type_ids" for i in session.get_inputs()):
        feeds["token_type_ids"] = np.zeros_like(ids)
    hidden = session.run(None, feeds)[0][0]  # (tokens, dim)
    weights = mask[0][:, None].astype(np.float32)
    vector = (hidden * weights).sum(axis=0) / max(float(weights.sum()), 1.0)
    norm = float(np.linalg.norm(vector)) or 1.0
    return tuple(float(x) for x in vector / norm)


def similarity(a: str, b: str) -> Optional[float]:
    """Cosine similarity of two labels (case-insensitive), or None when off."""
    left, right = (a or "").strip().lower(), (b or "").strip().lower()
    if not left or not right:
        return None
    va, vb = _embed(left), _embed(right)
    if va is None or vb is None:
        return None
    return round(sum(x * y for x, y in zip(va, vb)), 4)


def best_similarity(expected: list[str], actual: list[str]) -> Optional[float]:
    """The highest similarity between any expected and any actual label."""
    scores = [s for e in expected for a in actual if (s := similarity(e, a)) is not None]
    return max(scores) if scores else None
