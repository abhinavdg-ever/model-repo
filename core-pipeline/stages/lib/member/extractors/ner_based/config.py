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


def _core_pipeline_root() -> Path:
    """Locate ``core-pipeline/`` without hard-coding ``parents[N]``.

    ``HERE.parents[4]`` is correct only when this file lives at the usual
    depth under core-pipeline. Shallow checkouts, alternate layouts, and
    Windows drive-root edge cases raise ``IndexError`` — walk up instead and
    recognize the service root by ``cli.py`` + ``api/``.
    """
    for parent in (HERE, *HERE.parents):
        if (parent / "cli.py").is_file() and (parent / "api").is_dir():
            return parent
    # Intact monorepo layout: ner_based → extractors → member → lib → stages → core-pipeline
    if len(HERE.parents) > 4:
        return HERE.parents[4]
    raise RuntimeError(
        f"Cannot locate core-pipeline root from {HERE} "
        "(expected a parent containing cli.py and api/)"
    )


def _load_env() -> None:
    """Load core-pipeline/.env, if it exists and python-dotenv is installed.

    This module reads os.environ once, at import time, so whoever imports it
    first decides what it sees — and the entrypoints did not agree. cli.py and
    api/main.py load .env; the documented downloader command,

        python -m stages.lib.member.extractors.ner_based.model_downloader

    did not. So MEMBER_NER_MODEL_ID=gliner_low in .env meant the pipeline
    demanded gliner_low while the downloader silently fetched the gliner_medium
    default: a ~2 GB download of a model nothing would load, and then

        NER layer INACTIVE (checkpoints missing: gliner_low)

    naming a model the downloader was never going to fetch. MEMBER_NER_MODELS_PATH
    split the same way — weights written to one directory, looked for in another.

    Loading it here rather than in the downloader is what makes that class of
    mismatch impossible: this is the module that reads the variables, so every
    entrypoint agrees by construction rather than by remembering.

    ``override=True`` so a leftover shell export (common after copying the old
    docs' ``export MEMBER_NER_MODEL_ID=gliner_low``) cannot beat the value in
    core-pipeline/.env when running under local uvicorn / CLI.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # optional; the environment may be set directly
        return
    env_file = _core_pipeline_root() / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=True)


_load_env()
# .../stages/lib/member/extractors/ner_based -> .../stages/lib/member
MEMBER_ROOT = HERE.parents[1] if len(HERE.parents) > 1 else HERE
# .../core-pipeline — the service that owns these checkpoints. NOT the repo
# root: compose mounts ${MODELS_HOST_PATH:-./models} at
# /app/core-pipeline/models, so ner/ lives next to hw/ and rapidocr/.
CORE_ROOT = _core_pipeline_root()


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
