"""NER model toggles.

Ported from the V1 Member_Verification NER extractor config.

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

    python -m stages.lib.member.extractors.ner_based.model_downloader
"""

from __future__ import annotations

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
# .../stages/lib/member/extractors/ner_based -> .../stages/lib/member
MEMBER_ROOT = HERE.parents[1]
# .../core-pipeline — the service that owns these checkpoints. NOT the repo
# root: the compose file mounts ${NER_MODELS_HOST_PATH:-./models/ner} relative
# to core-pipeline/, so anything else puts the download and the mount in two
# different places.
CORE_ROOT = HERE.parents[4]


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

# Which checkpoint the extractor loads. This is the ONLY model knob.
#
# v7 also had GLINER_LARGE / GLINER_MEDIUM / GLINER_LOW, which fed the
# readiness check while MEMBER_NER_MODEL_ID drove what actually ran. With all
# three true by default, `ready` demanded three checkpoints while extraction
# used one — so a missing model you were never going to load reported the layer
# as unavailable, and the warning named a different model from the error. Two
# knobs for one decision; the flags are gone.
MEMBER_NER_MODEL_ID = (
    os.environ.get("MEMBER_NER_MODEL_ID") or "gliner_medium"
).strip()

# ~2 GB of checkpoints. core-pipeline/models/ is gitignored; keep it that way
# if you change this default.
NER_MODELS_PATH = Path(
    os.environ.get("MEMBER_NER_MODELS_PATH")
    or (CORE_ROOT / "models" / "ner")
)

def enabled_model_ids() -> list[str]:
    """The model ids this run needs, or [] when the NER layer is switched off.

    Exactly one: whichever MEMBER_NER_MODEL_ID names. Kept as a list because
    ner_status() and model.py report several fields in list form.
    """
    if not ner_enabled:
        return []
    return [MEMBER_NER_MODEL_ID]


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
    status["model_id"] = MEMBER_NER_MODEL_ID

    # Switched off wins over everything below it. Reporting "gliner not
    # installed" when the layer is deliberately disabled sends the reader off
    # to install 2.5 GB they do not need.
    if not ner_enabled:
        status["weights_present"] = []
        status["weights_missing"] = []
        status["ready"] = False
        status["reason"] = "MEMBER_NER_ENABLED=false"
        return status

    if not installed:
        status["weights_present"] = []
        status["weights_missing"] = enabled_model_ids()
        status["ready"] = False
        status["reason"] = detail
        return status

    from .model import missing_weights, weights_present

    wanted = enabled_model_ids()
    missing = missing_weights(wanted)
    status["weights_present"] = [m for m in wanted if weights_present(m)]
    status["weights_missing"] = missing
    status["model_id"] = MEMBER_NER_MODEL_ID
    status["ready"] = ner_enabled and not missing and bool(wanted)

    if missing:
        status["reason"] = (
            f"checkpoints missing: {', '.join(missing)}. Download with: "
            "python -m stages.lib.member.extractors.ner_based.model_downloader"
        )
    else:
        status["reason"] = "ok"
    return status
