"""BERT page classifier: predicts ``model_type`` with a confidence.

The model is a Hugging Face sequence classifier in ``PAGE_FAMILY_MODEL_DIR``
(default ``models/page-family/``), written by ``training/bert-training``. Its
``id2label`` names must all be model types from ``page_taxonomy.json``;
``skipped_classes.csv`` (when present) lists the model types it was not
trained on, which the decision ladder needs to know.

* No folder, or no transformers / torch: ``status()["ready"]`` is False and
  ``predict`` returns None. The ladder then runs on keywords alone, and every
  page records ``bert_model_type = NULL`` — a degraded run visible in the data.
* A folder whose labels are not taxonomy model types raises at load, naming
  the bad labels: a model trained on another label set must not run silently.
"""
from __future__ import annotations

import csv
import logging
import threading
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Optional

from stages.lib.page_classify import taxonomy

logger = logging.getLogger(__name__)

MAX_LENGTH = 512
TOP_N = 3

_lock = threading.Lock()
_loaded: Optional[dict[str, Any]] = None
_failed: Optional[str] = None


class ModelLabelsError(RuntimeError):
    """The model folder's labels are not taxonomy model types."""


@dataclass(frozen=True)
class BertResult:
    model_type: str
    page_type: str
    confidence: float
    # Top probability minus the second: how clearly BERT prefers its answer.
    lead: float = 0.0
    # Page types of its top 3 model types, best first, without repeats.
    top_page_types: tuple[str, ...] = ()


def model_dir() -> Path:
    from config import PAGE_FAMILY_MODEL_DIR

    return Path(PAGE_FAMILY_MODEL_DIR)


def _skipped(folder: Path) -> frozenset[str]:
    path = folder / "skipped_classes.csv"
    if not path.is_file():
        return frozenset()
    with path.open(newline="", encoding="utf-8") as handle:
        return frozenset(
            (row.get("model_type") or "").strip()
            for row in csv.DictReader(handle)
            if (row.get("model_type") or "").strip()
        )


def _load() -> Optional[dict[str, Any]]:
    global _loaded, _failed
    if _loaded is not None or _failed is not None:
        return _loaded
    with _lock:
        if _loaded is not None or _failed is not None:
            return _loaded
        folder = model_dir()
        if not (folder / "config.json").is_file():
            _failed = f"no model at {folder} (config.json missing)"
            logger.warning("Page classifier: %s; keywords only", _failed)
            return None
        check_labels(folder)
        if not (find_spec("transformers") and find_spec("torch")):
            _failed = "transformers / torch not installed"
            logger.warning("Page classifier: %s; keywords only", _failed)
            return None
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        model = AutoModelForSequenceClassification.from_pretrained(folder)
        labels = {int(k): v for k, v in model.config.id2label.items()}
        model.eval()
        _loaded = {
            "model": model,
            "tokenizer": AutoTokenizer.from_pretrained(folder),
            "labels": labels,
            "trained": frozenset(labels.values()),
            "skipped": _skipped(folder),
            "torch": torch,
        }
        logger.info("Page classifier: BERT with %d classes from %s", len(labels), folder)
        return _loaded


def check_labels(folder: Path) -> frozenset[str]:
    """The model's labels from config.json; raises unless all are taxonomy model types."""
    import json

    try:
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelLabelsError(f"{folder}/config.json is unreadable: {exc}") from exc
    labels = frozenset((config.get("id2label") or {}).values())
    known = taxonomy.load().model_types
    unknown = sorted(label for label in labels if label not in known)
    if not labels or unknown:
        raise ModelLabelsError(
            f"{folder} predicts labels that are not model types in page_taxonomy.json: "
            + (", ".join(unknown) or "(no id2label)")
        )
    return labels


def trained_classes() -> frozenset[str]:
    """Model types the loaded model can predict; empty when there is none."""
    loaded = _load()
    return loaded["trained"] if loaded else frozenset()


def predict(text: str) -> Optional[BertResult]:
    loaded = _load()
    if loaded is None or not (text or "").strip():
        return None
    torch = loaded["torch"]
    tokens = loaded["tokenizer"](
        text, truncation=True, max_length=MAX_LENGTH, return_tensors="pt"
    )
    with _lock, torch.no_grad():
        logits = loaded["model"](**tokens).logits[0]
    probabilities = torch.softmax(logits, dim=-1)
    best = torch.topk(probabilities, k=min(TOP_N, probabilities.shape[-1]))
    names = taxonomy.load()
    model_types = [loaded["labels"][int(i)] for i in best.indices]
    page_types: list[str] = []
    for name in model_types:
        page_type = names.page_type_of_model(name) or name
        if page_type not in page_types:
            page_types.append(page_type)
    second = float(best.values[1]) if best.values.shape[-1] > 1 else 0.0
    return BertResult(
        model_type=model_types[0],
        page_type=page_types[0],
        confidence=round(float(best.values[0]), 4),
        lead=round(float(best.values[0]) - second, 4),
        top_page_types=tuple(page_types),
    )


def describe() -> dict[str, Any]:
    """For /health: what the model folder holds, without loading any weights."""
    folder = model_dir()
    if not (folder / "config.json").is_file():
        return {"ready": False, "reason": f"no model at {folder}; keywords only", "path": str(folder)}
    try:
        labels = check_labels(folder)
    except ModelLabelsError as exc:
        return {"ready": False, "reason": str(exc), "path": str(folder)}
    if not (find_spec("transformers") and find_spec("torch")):
        return {"ready": False, "reason": "transformers / torch not installed; keywords only",
                "path": str(folder)}
    known = set(taxonomy.load().model_types)
    return {
        "ready": True,
        "path": str(folder),
        "classes": len(labels),
        "untrained_model_types": sorted(known - labels),
    }


def status() -> dict[str, Any]:
    """For /health: whether BERT runs, and which model types it cannot reach."""
    try:
        loaded = _load()
    except ModelLabelsError as exc:
        return {"ready": False, "reason": str(exc), "path": str(model_dir())}
    if loaded is None:
        return {"ready": False, "reason": _failed, "path": str(model_dir())}
    all_types = set(taxonomy.load().model_types)
    return {
        "ready": True,
        "path": str(model_dir()),
        "classes": len(loaded["trained"]),
        "untrained_model_types": sorted(all_types - loaded["trained"]),
    }
