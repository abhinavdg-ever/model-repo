"""NER model toggles. Weights live in Member_Verification/Models."""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
# .../Member_Verification/extractors/ner_based -> Member_Verification
MV_ROOT = HERE.parents[1]
REPO_ROOT = HERE.parents[2]


def _load_repo_env() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_bool(key: str, default: bool = False) -> bool:
    value = os.environ.get(key)
    if value is None or not str(value).strip():
        raw = "true" if default else "false"
    else:
        raw = str(value).strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


_load_repo_env()

gliner_large = _env_bool("GLINER_LARGE", True)
gliner_medium = _env_bool("GLINER_MEDIUM", True)
gliner_low = _env_bool("GLINER_LOW", True)

NER_MODELS_PATH = MV_ROOT / "Models"

_MODEL_FLAGS = (
    ("gliner_large", gliner_large),
    ("gliner_medium", gliner_medium),
    ("gliner_low", gliner_low),
)


def enabled_model_ids() -> list[str]:
    return [model_id for model_id, on in _MODEL_FLAGS if on]
