"""The encounter model's per-page answer, for the vote (``encounter_classify.page_vote``).

The model is the folder ``train_encounter.py`` writes (``encoder/``, ``head.pt``,
``label_space.json``) in ``ENCOUNTER_MODEL_DIR`` (default ``models/encounter/``). Its
answer counts only where the rules found no evidence on the page, and only for the
classes and confidences ``encounter_canon.json`` → ``model_vote`` lists. A model trained
on the page target (2026-10-25) can answer "No evidence on page": that never votes.

* No folder, or no transformers / torch: no page gets a model answer and the rules
  decide alone (``/health`` → ``encounter_model.ready`` false). A degraded run shows in
  the data: no vote says ``model``.
"""
from __future__ import annotations

import json
import logging
import threading
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

MODEL_FILES = ("label_space.json", "head.pt", "encoder/config.json", "encoder/model.safetensors")
BATCH = 16

_lock = threading.Lock()
_loaded: Optional[dict[str, Any]] = None
_failed: Optional[str] = None


def model_dir() -> Path:
    from config import ENCOUNTER_MODEL_DIR

    return Path(ENCOUNTER_MODEL_DIR)


def _unusable(folder: Path) -> Optional[str]:
    missing = [name for name in MODEL_FILES if not (folder / name).is_file()]
    if missing:
        return f"no model at {folder} ({', '.join(missing)} missing)"
    if (folder / "encoder" / "model.safetensors").stat().st_size < 1024:
        return f"encoder/model.safetensors in {folder} is a placeholder, not the weights"
    return None


def _load() -> Optional[dict[str, Any]]:
    global _loaded, _failed
    if _loaded is not None or _failed is not None:
        return _loaded
    with _lock:
        if _loaded is not None or _failed is not None:
            return _loaded
        folder = model_dir()
        _failed = _unusable(folder)
        if _failed is None and not (find_spec("transformers") and find_spec("torch")):
            _failed = "transformers / torch not installed"
        if _failed:
            logger.warning("Encounter model: %s; rules only", _failed)
            return None
        import torch

        from stages.lib.encounter import encounter_model

        try:
            model, tokenizer, space = encounter_model.load_model(folder)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 — a bad model must not sink the chart
            _failed = f"model at {folder} failed to load: {str(exc).splitlines()[0][:200]}"
            logger.error("Encounter model: %s; rules only", _failed)
            return None
        _loaded = {"model": model, "tokenizer": tokenizer, "space": space, "torch": torch}
        logger.info("Encounter model: %s from %s", space.get("label_version"), folder)
        return _loaded


def predict(texts: list[str]) -> list[Optional[tuple[str, float]]]:
    """Per text: (class, confidence), or None for an empty text or without a model."""
    out: list[Optional[tuple[str, float]]] = [None] * len(texts)
    todo = [i for i, text in enumerate(texts) if (text or "").strip()]
    loaded = _load() if todo else None
    if loaded is None:
        return out
    from stages.lib.encounter import encounter_model

    torch, space, tokenizer = loaded["torch"], loaded["space"], loaded["tokenizer"]
    pad = tokenizer.pad_token_id or 0
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        ids = encounter_model.encode_texts(
            tokenizer, [texts[i] for i in chunk],
            int(space.get("max_length", encounter_model.MAX_LENGTH)),
            float(space.get("head_fraction", encounter_model.HEAD_FRACTION)),
        )
        width = max(len(x) for x in ids)
        input_ids = torch.tensor([x + [pad] * (width - len(x)) for x in ids])
        mask = torch.tensor([[1] * len(x) + [0] * (width - len(x)) for x in ids])
        with _lock, torch.no_grad():
            probs = torch.softmax(loaded["model"](input_ids, mask), -1).tolist()
        for i, row in zip(chunk, probs):
            k = max(range(len(row)), key=row.__getitem__)
            out[i] = (space["classes"][k], round(float(row[k]), 4))
    return out


def describe() -> dict[str, Any]:
    """For /health: what the folder holds, without loading any weights."""
    folder = model_dir()
    unusable = _unusable(folder)
    if unusable:
        return {"ready": False, "reason": f"{unusable}; rules only", "path": str(folder)}
    try:
        space = json.loads((folder / "label_space.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ready": False, "reason": str(exc), "path": str(folder)}
    if not (find_spec("transformers") and find_spec("torch")):
        return {"ready": False, "reason": "transformers / torch not installed; rules only", "path": str(folder)}
    return {"ready": True, "path": str(folder), "label_version": space.get("label_version"),
            "classes": space.get("classes")}
