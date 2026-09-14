"""Core pipeline configuration."""
from __future__ import annotations

import os
from importlib.util import find_spec
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = Path(__file__).resolve().parent
REVIEW_UI_ROOT = REPO_ROOT / "review-ui"
DATA_ROOT = Path(
    os.environ.get("DATA_ROOT") or (REVIEW_UI_ROOT / "data" / "folders")
).resolve()
METADATA_ROOT = Path(
    os.environ.get("METADATA_ROOT") or (REVIEW_UI_ROOT / "data" / "metadata")
).resolve()

def _psycopg_url(url: str) -> str:
    """Accept SQLAlchemy-style postgresql+psycopg:// as well as plain postgresql://.

    core-pipeline talks to psycopg directly, which rejects the "+psycopg"
    dialect suffix with an error that does not say so:

        missing "=" after "postgresql+psycopg://..." in connection info string

    review-ui's .env.example uses the SQLAlchemy form, and the two services
    share the variable name, so the wrong one gets copied across constantly.
    Both forms are accepted here, as review-ui already accepts both.
    """
    url = (url or "").strip()
    for prefix in ("postgresql+psycopg://", "postgres+psycopg://",
                   "postgresql+psycopg2://", "postgres+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix):]
    return url


DATABASE_URL = _psycopg_url(
    os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/imaging_outputs",
    )
)

AZURE_STORAGE_AUTH = (os.environ.get("AZURE_STORAGE_AUTH") or "entra").strip().casefold()
AZURE_STORAGE_ACCOUNT_NAME = (os.environ.get("AZURE_STORAGE_ACCOUNT_NAME") or "").strip()
AZURE_STORAGE_ACCOUNT_KEY = (os.environ.get("AZURE_STORAGE_ACCOUNT_KEY") or "").strip()
AZURE_STORAGE_CONNECTION_STRING = (
    os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or ""
).strip()
AZURE_STORAGE_CONTAINER = (
    os.environ.get("AZURE_STORAGE_CONTAINER") or "imaging-pipeline"
).strip()

AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT = (
    os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT") or ""
).strip()
AZURE_DOCUMENT_INTELLIGENCE_KEY = (
    os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_KEY") or ""
).strip()
AZURE_POLL_TIMEOUT_SECONDS = int(os.environ.get("AZURE_POLL_TIMEOUT_SECONDS") or "180")

# --- Azure OpenAI (DOS range extraction — the reference's LLM pass) ---------
# dos_logic.extract_dos_range_with_llm needs these. Without them the DOS stage
# runs rules-only and stamps extraction_method='rules' so the difference is
# visible in the data rather than silent.
AZURE_OPENAI_API_KEY = (os.environ.get("AZURE_OPENAI_API_KEY") or "").strip()
AZURE_OPENAI_ENDPOINT = (os.environ.get("AZURE_OPENAI_ENDPOINT") or "").strip()
AZURE_OPENAI_API_VERSION = (
    os.environ.get("AZURE_OPENAI_API_VERSION") or "2024-08-01-preview"
).strip()
AZURE_OPENAI_DEPLOYMENT = (
    os.environ.get("AZURE_OPENAI_DEPLOYMENT") or "gpt-4o-mini"
).strip()
# key | entra | auto. `auto` means "key if there is one, else Entra ID" — so a
# VM with a managed identity and no key still gets the LLM pass, and a laptop
# with a key is unaffected. See stages/lib/dos/azure_llm.py.
AZURE_OPENAI_AUTH = (os.environ.get("AZURE_OPENAI_AUTH") or "auto").strip().casefold()


# --- Rotation correction ----------------------------------------------------
# Stage 1 always MEASURES orientation, tilt and mirror. This decides whether it
# also WRITES a corrected image to corrected-pages/, which every later stage
# then reads in place of the original.
#
# Default OFF, and that is a measurement, not caution. Round-tripping the demo
# chart through all four orientations (rotate, detect, correct, compare):
#
#     90 CW  -> detected 0 or 180   (never 270)  sideways page left sideways
#     270 CW -> detected 0          (never 90)   sideways page left sideways
#     mirror falsely reported on 3 of 12 cases
#     rotation_confidence was 1.000 on wrong answers, so it cannot gate this
#
# 0 of 6 sideways pages were recovered, and a false mirror actively corrupts a
# page that was fine. Writing corrections on that basis would cost accuracy
# rather than gain it. The plumbing is in place and correct; turn this on once
# the detector recovers a rotated page. See PLAN.md.
ROTATION_CORRECTION_ENABLED = (
    os.environ.get("ROTATION_CORRECTION_ENABLED") or "false"
).strip().casefold() in {"1", "true", "yes", "on"}


def _flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().casefold()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _azure_openai_auth_usable() -> bool:
    """Can we authenticate at all — by key, or by Entra ID with no key?

    `find_spec` only asks whether azure-identity is installed; whether the VM's
    managed identity actually holds the role is answered by the first call, and
    a failure there degrades the stage to regex with a warning.
    """
    if AZURE_OPENAI_AUTH == "key":
        return bool(AZURE_OPENAI_API_KEY)
    if AZURE_OPENAI_AUTH == "entra":
        return find_spec("azure.identity") is not None
    return bool(AZURE_OPENAI_API_KEY) or find_spec("azure.identity") is not None


DOS_LLM_ENABLED = (
    _flag("DOS_LLM_ENABLED", True)
    and bool(AZURE_OPENAI_ENDPOINT)
    and _azure_openai_auth_usable()
)

# --- Member verification ----------------------------------------------------
# The NER layer needs the GLiNER checkpoints, which are not in the repo. With it
# off the pipeline runs the reference's rule pass only — and no page can be
# classified wrong_member, so no document can be Rejected. See
# stages/lib/member/engine.py.
MEMBER_NER_ENABLED = _flag("MEMBER_NER_ENABLED", False)
MEMBER_NER_MODEL_ID = (
    os.environ.get("MEMBER_NER_MODEL_ID") or "gliner_medium"
).strip()
MEMBER_NER_MODELS_PATH = os.environ.get("MEMBER_NER_MODELS_PATH") or ""

# Stage concurrency: pages processed in parallel within one stage. OCR stages
# are IO/CPU bound per page and independent, so this is the main throughput
# lever. Keep <= DB_POOL_MAX.
STAGE_WORKERS = int(os.environ.get("STAGE_WORKERS") or "4")

TESSERACT_CMD = (os.environ.get("TESSERACT_CMD") or "").strip() or None
HW_MODEL_PATH = Path(
    os.environ.get("HW_MODEL_PATH")
    or (Path(__file__).resolve().parent / "stages" / "lib" / "imaging"
        / "image_type_classification.pkl")
)

IMAGE_SUFFIXES = {
    ".bmp", ".dib", ".gif", ".j2k", ".jfif", ".jp2", ".jpe", ".jpeg", ".jpg",
    ".pbm", ".pgm", ".png", ".pnm", ".ppm", ".tif", ".tiff", ".webp",
}

API_HOST = os.environ.get("API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("API_PORT") or "8001")
RUN_PIPELINE_INLINE = (os.environ.get("RUN_PIPELINE_INLINE") or "true").lower() in {
    "1", "true", "yes", "on",
}


def chart_dir(chart_name: str) -> Path:
    return DATA_ROOT / chart_name


def pages_dir(chart_name: str) -> Path:
    return chart_dir(chart_name) / "pages"


def ocr_dir(chart_name: str) -> Path:
    return chart_dir(chart_name) / "ocr"


def imaging_dir(chart_name: str) -> Path:
    return chart_dir(chart_name) / "imaging"


def corrected_pages_dir(chart_name: str) -> Path:
    """Rotation/mirror/tilt-corrected page images, written by stage 1.

    Sparse on purpose: a page that needed no correction is NOT copied here, so
    the folder's contents are exactly the pages that were changed, and the
    workspace does not carry a second copy of every scan.
    """
    return chart_dir(chart_name) / "corrected-pages"


def page_image_path(chart_name: str, page_name: str) -> Path:
    """The image a stage should actually read: corrected if one exists.

    Every stage that opens a page image goes through here, so "use the
    corrected page when there is one" is a single rule rather than four copies
    of the same `if`. Pages needing no correction fall through to pages/, which
    is also what happens for a chart processed before corrections existed.
    """
    corrected = corrected_pages_dir(chart_name) / page_name
    return corrected if corrected.is_file() else pages_dir(chart_name) / page_name


def ensure_chart_dirs(chart_name: str) -> Path:
    root = chart_dir(chart_name)
    for sub in ("pages", "ocr", "imaging", "corrected-pages"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


# The member NER package reads these from the environment at import time.
os.environ.setdefault("MEMBER_NER_ENABLED", "true" if MEMBER_NER_ENABLED else "false")
if MEMBER_NER_MODELS_PATH:
    os.environ.setdefault("MEMBER_NER_MODELS_PATH", MEMBER_NER_MODELS_PATH)
