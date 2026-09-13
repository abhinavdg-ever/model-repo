"""NER model toggles.

Ported from Reference/V1 Code/Member_Verification/extractors/ner_based/config.py.

Two adaptations for the pipeline:

  * ``NER_MODELS_PATH`` is configurable (``MEMBER_NER_MODELS_PATH``) instead of
    being hard-wired to ``Member_Verification/Models``. It still defaults to the
    reference location so an existing checkpoint download keeps working.

  * ``MEMBER_NER_ENABLED`` gates the whole NER layer. The reference always ran
    NER and treated a load failure as fatal — deliberately, so a dead model
    could not masquerade as "no member details on the page". That behaviour is
    preserved exactly when the layer is ON. Turning it OFF is an explicit
    rules-only run: the stage records ner_enabled=false on every row and in the
    summary, so a rules-only result is never mistaken for a full one.

The GLiNER checkpoints are not in the repository. Download them with the
reference downloader before enabling:

    python -m Member_Verification.Models.model_downloader
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
# .../stages/lib/member/extractors/ner_based -> .../stages/lib/member
MEMBER_ROOT = HERE.parents[1]
# .../advantmed-imaging-pipeline
REPO_ROOT = HERE.parents[5]


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


# Master switch for the NER layer.
ner_enabled = _env_bool("MEMBER_NER_ENABLED", False)

gliner_large = _env_bool("GLINER_LARGE", True)
gliner_medium = _env_bool("GLINER_MEDIUM", True)
gliner_low = _env_bool("GLINER_LOW", True)

NER_MODELS_PATH = Path(
    os.environ.get("MEMBER_NER_MODELS_PATH")
    or (REPO_ROOT / "Reference" / "V1 Code" / "Member_Verification" / "Models")
)

_MODEL_FLAGS = (
    ("gliner_large", gliner_large),
    ("gliner_medium", gliner_medium),
    ("gliner_low", gliner_low),
)


def enabled_model_ids() -> list[str]:
    """Enabled model ids, or [] when the NER layer is switched off."""
    if not ner_enabled:
        return []
    return [model_id for model_id, on in _MODEL_FLAGS if on]


def deps_installed() -> tuple[bool, str]:
    """(importable, detail) for the GLiNER runtime.

    Separated from the weights check because the two failures need different
    fixes and the reference's ModelLoadError could not tell them apart:
    a missing package needs `pip install -r requirements-ner.txt`, missing
    weights need the downloader.
    """
    try:
        import gliner  # noqa: F401
    except ImportError as exc:
        return False, f"gliner not installed ({exc}); pip install -r requirements-ner.txt"
    try:
        import torch  # noqa: F401
    except ImportError as exc:
        return False, f"torch not installed ({exc}); pip install -r requirements-ner.txt"
    return True, "ok"


def ner_status() -> dict:
    """What the NER layer can actually do right now.

    Reported by GET /health and logged by the member stage, so a rules-only run
    is explainable without reading tracebacks.
    """
    status: dict = {
        "enabled": ner_enabled,
        "models_path": str(NER_MODELS_PATH),
        "enabled_models": enabled_model_ids(),
    }
    installed, detail = deps_installed()
    status["deps_installed"] = installed
    status["deps_detail"] = detail

    if not installed:
        status["weights_present"] = []
        status["weights_missing"] = [m for m, on in _MODEL_FLAGS if on]
        status["ready"] = False
        status["reason"] = detail
        return status

    from .model import missing_weights, weights_present

    wanted = [m for m, on in _MODEL_FLAGS if on]
    missing = missing_weights(wanted)
    status["weights_present"] = [m for m in wanted if weights_present(m)]
    status["weights_missing"] = missing
    status["ready"] = ner_enabled and not missing and bool(wanted)

    if not ner_enabled:
        status["reason"] = "MEMBER_NER_ENABLED=false"
    elif missing:
        status["reason"] = (
            f"checkpoints missing: {', '.join(missing)}. Download with: "
            "python -m stages.lib.member.extractors.ner_based.model_downloader"
        )
    else:
        status["reason"] = "ok"
    return status
