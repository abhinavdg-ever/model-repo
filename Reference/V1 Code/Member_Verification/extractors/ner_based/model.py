from __future__ import annotations

import gc
import logging
import os
import sys
import time
import warnings
from pathlib import Path

from .config import MV_ROOT, NER_MODELS_PATH, enabled_model_ids

# The catalog lives beside the weights in Member_Verification/Models, so it is
# imported from there rather than through a re-export package.
if str(NER_MODELS_PATH) not in sys.path:
    sys.path.insert(0, str(NER_MODELS_PATH))

from catalog import MODELS, by_id, model_dir, relink_local_paths  # noqa: E402

logger = logging.getLogger(__name__)

_PERSON_LABELS = frozenset({"person", "per", "name", "people"})

_ENABLED_IDS = enabled_model_ids()
_ACTIVE_ID = _ENABLED_IDS[0] if _ENABLED_IDS else MODELS[0]["id"]
_LOADED: dict[str, object] = {}
_LOAD_FAILED: set[str] = set()
_LOAD_ERRORS: dict[str, str] = {}
_OFFLINE = False

# Retries for a load that fails for a reason that may clear on its own.
_LOAD_ATTEMPTS = 3
_LOAD_RETRY_SECONDS = 2


class ModelLoadError(RuntimeError):
    """An enabled NER model could not be loaded."""


def _force_offline() -> None:
    global _OFFLINE
    if _OFFLINE:
        return
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    os.environ["HF_HUB_DISABLE_SYMLINKS"] = "1"
    _OFFLINE = True


def _looks_like_model(path: Path, kind: str) -> bool:
    if not path.is_dir():
        return False
    names = {item.name.lower() for item in path.iterdir() if item.is_file()}
    has_weights = "pytorch_model.bin" in names or "model.safetensors" in names
    if kind == "gliner":
        return has_weights and "gliner_config.json" in names
    return has_weights and "config.json" in names


def _relink(path: Path) -> None:
    relink_local_paths(path)


def weights_present(model_id: str) -> bool:
    """True when the checkpoint for this model is on disk and loadable."""
    spec = by_id(model_id)
    return _looks_like_model(model_dir(spec), spec["kind"])


def missing_weights(model_ids: list[str]) -> list[str]:
    """Enabled models whose checkpoints are not downloaded."""
    return [model_id for model_id in model_ids if not weights_present(model_id)]


def use_model(model_id: str) -> None:
    """Make this the model predictions use.

    Loaded checkpoints are kept, so switching between the enabled models costs
    nothing after the first load of each. The run walks records in the outer
    loop and models in the inner one, so unloading here meant reloading every
    checkpoint from disk for every record. Call unload() to release them.
    """
    global _ACTIVE_ID
    _ACTIVE_ID = by_id(model_id)["id"]


def unload() -> None:
    """Release every loaded checkpoint."""
    _LOADED.clear()
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # releasing cache is best effort, never fatal
        logger.debug("could not empty the CUDA cache", exc_info=True)


def _load_gliner(path: Path):
    # GLiNER's import calls torch.jit.script, which warns on newer torch. It is
    # torch's own internal call, nothing this code can switch to torch.compile,
    # so the one message is silenced rather than printed on every load.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*torch\.jit\.script.*",
            category=FutureWarning,
        )
        from gliner import GLiNER

        _relink(path)
        return GLiNER.from_pretrained(str(path), local_files_only=True)


def _load_hf_token(path: Path, spec: dict):
    from transformers import AutoModelForTokenClassification, AutoTokenizer, pipeline

    _relink(path)
    tokenizer = AutoTokenizer.from_pretrained(
        str(path),
        local_files_only=True,
        add_prefix_space=bool(spec.get("add_prefix_space")),
    )
    model = AutoModelForTokenClassification.from_pretrained(str(path), local_files_only=True)
    return pipeline(
        "ner",
        model=model,
        tokenizer=tokenizer,
        aggregation_strategy=spec.get("aggregation_strategy") or "simple",
        device=-1,
    )


def get_backend(model_id: str | None = None):
    """Return (spec, backend), loading the checkpoint on first use.

    A model that will not load raises. Returning None instead meant the run
    carried on detecting nothing and still wrote a full set of CSVs, so a
    dead model looked exactly like a chart with no member details on it.
    """
    spec = by_id(model_id or _ACTIVE_ID)
    model_id = spec["id"]
    if model_id in _LOADED:
        return spec, _LOADED[model_id]
    if model_id in _LOAD_FAILED:
        raise ModelLoadError(f"{model_id} failed to load earlier: {_LOAD_ERRORS.get(model_id, 'unknown error')}")
    _force_offline()
    path = model_dir(spec)
    if not _looks_like_model(path, spec["kind"]):
        message = (
            f"{model_id} is not in {path}. Run: python "
            f"Member_Verification/Models/model_downloader/__main__.py"
        )
        _LOAD_FAILED.add(model_id)
        _LOAD_ERRORS[model_id] = message
        raise ModelLoadError(message)
    logger.info("loading %s from %s", model_id, path)
    backend = None
    last_error: Exception | None = None
    # A load can fail for a reason that clears on its own -- a file lock, an
    # antivirus or Application Control scan of a freshly written DLL -- so a
    # single failure is retried before the model is given up on.
    for attempt in range(1, _LOAD_ATTEMPTS + 1):
        try:
            if spec["kind"] == "gliner":
                backend = _load_gliner(path)
            elif spec["kind"] == "hf_token":
                backend = _load_hf_token(path, spec)
            else:
                raise ValueError(spec["kind"])
            break
        except Exception as exc:
            last_error = exc
            if attempt < _LOAD_ATTEMPTS:
                logger.warning(
                    "%s load attempt %s/%s failed (%s); retrying in %ss",
                    model_id,
                    attempt,
                    _LOAD_ATTEMPTS,
                    exc,
                    _LOAD_RETRY_SECONDS,
                )
                time.sleep(_LOAD_RETRY_SECONDS)
    if backend is None:
        logger.exception("%s could not be loaded from %s", model_id, path)
        _LOAD_FAILED.add(model_id)
        _LOAD_ERRORS[model_id] = f"{type(last_error).__name__}: {last_error}"
        raise ModelLoadError(
            f"{model_id} could not be loaded from {path}: {last_error}"
        ) from last_error
    _LOADED[model_id] = backend
    return spec, backend


def ensure_loadable(model_ids: list[str]) -> None:
    """Load every enabled model once, up front.

    Checking that the weight files exist is not enough: a checkpoint can be
    present and still fail to load. Doing it before the queue starts turns
    that into one clear error instead of a whole batch of CSVs with nothing
    detected in them.
    """
    failures: list[str] = []
    for model_id in model_ids:
        try:
            get_backend(model_id)
        except Exception as exc:
            failures.append(f"{model_id}: {exc}")
    if failures:
        raise ModelLoadError("; ".join(failures))


def _hit(text: str, label: str, score: float, start: int | None, end: int | None) -> dict:
    return {"text": text, "label": label, "score": score, "start": start, "end": end}


def _predict_gliner(model, text: str, labels: list[str], threshold: float) -> list[dict]:
    hits = model.predict_entities(text, labels, threshold=threshold) or []
    out: list[dict] = []
    for hit in hits:
        start = hit.get("start")
        end = hit.get("end")
        out.append(
            _hit(
                str(hit.get("text") or ""),
                str(hit.get("label") or ""),
                float(hit.get("score") or 0),
                int(start) if start is not None else None,
                int(end) if end is not None else None,
            )
        )
    return out


def _wanted_token_label(entity_group: str, labels: list[str]) -> str | None:
    group = entity_group.upper()
    want = {item.casefold() for item in labels}
    if want & _PERSON_LABELS:
        if "PER" in group:
            return "person"
        return None
    if "PER" in group:
        return "person"
    return entity_group or "MISC"


def _predict_hf_token(nlp, text: str, labels: list[str]) -> list[dict]:
    out: list[dict] = []
    for hit in nlp(text) or []:
        group = str(hit.get("entity_group") or hit.get("entity") or "")
        mapped = _wanted_token_label(group, labels)
        if mapped is None:
            continue
        start = hit.get("start")
        end = hit.get("end")
        start_i = int(start) if start is not None else None
        end_i = int(end) if end is not None else None
        span = text[start_i:end_i] if start_i is not None and end_i is not None and end_i > start_i else ""
        word = str(hit.get("word") or span or "")
        out.append(
            _hit(
                span or word,
                mapped,
                float(hit.get("score") or 0),
                start_i,
                end_i,
            )
        )
    return out


def predict_entities(
    text: str,
    labels: list[str],
    threshold: float = 0.25,
    model_id: str | None = None,
    value_source: str = "",
) -> list[dict]:
    snippet = (text or "").strip()
    if not snippet:
        return []
    # get_backend raises if the model will not load, so there is no quiet
    # "no backend, no hits" path here any more.
    spec, backend = get_backend(model_id)
    if spec["kind"] == "gliner":
        hits = _predict_gliner(backend, snippet, labels, threshold)
    else:
        hits = _predict_hf_token(backend, snippet, labels)
    from .log import add

    for hit in hits:
        add(
            sentence=snippet,
            value=str(hit.get("text") or ""),
            value_type=str(hit.get("label") or ""),
            value_start=hit.get("start"),
            value_end=hit.get("end"),
            ner_confidence=hit.get("score"),
            value_source=value_source,
        )
    return hits
