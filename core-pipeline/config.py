"""Core pipeline configuration."""
from __future__ import annotations

import os
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

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/imaging_outputs",
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


def _flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().casefold()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


DOS_LLM_ENABLED = _flag("DOS_LLM_ENABLED", True) and bool(
    AZURE_OPENAI_API_KEY and AZURE_OPENAI_ENDPOINT
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
    or (REPO_ROOT / "Reference" / "advantmed_imaging" / "image_type_classification.pkl")
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


def ensure_chart_dirs(chart_name: str) -> Path:
    root = chart_dir(chart_name)
    for sub in ("pages", "ocr", "imaging"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


# The member NER package reads these from the environment at import time.
os.environ.setdefault("MEMBER_NER_ENABLED", "true" if MEMBER_NER_ENABLED else "false")
if MEMBER_NER_MODELS_PATH:
    os.environ.setdefault("MEMBER_NER_MODELS_PATH", MEMBER_NER_MODELS_PATH)
