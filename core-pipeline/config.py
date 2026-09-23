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
# Filled in on /run and /batch when the request omits them (blob mode only).
# Leave blank to keep today's behaviour: missing paths are a 400.
AZURE_BLOB_DEFAULT_READ_PATH = (
    os.environ.get("AZURE_BLOB_DEFAULT_READ_PATH") or ""
).strip()
AZURE_BLOB_DEFAULT_WRITE_PATH = (
    os.environ.get("AZURE_BLOB_DEFAULT_WRITE_PATH") or ""
).strip()
# User-assigned managed identity on a VM / App Service / AKS.
# AZURE_CLIENT_ID is what the SDK needs (ManagedIdentityCredential).
# AZURE_PRINCIPAL_ID is the object id used when assigning RBAC roles — stored
# for ops visibility only; it is not passed to the credential.
AZURE_CLIENT_ID = (os.environ.get("AZURE_CLIENT_ID") or "").strip()
AZURE_PRINCIPAL_ID = (os.environ.get("AZURE_PRINCIPAL_ID") or "").strip()
# HTTP connection pools for parallel blob / DI calls (SDK default is ~10).
AZURE_BLOB_CONNECTION_POOL_SIZE = int(
    os.environ.get("AZURE_BLOB_CONNECTION_POOL_SIZE") or "50"
)
AZURE_DI_CONNECTION_POOL_SIZE = int(
    os.environ.get("AZURE_DI_CONNECTION_POOL_SIZE") or "32"
)

AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT = (
    os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT") or ""
).strip()
AZURE_DOCUMENT_INTELLIGENCE_KEY = (
    os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_KEY") or ""
).strip()
AZURE_POLL_TIMEOUT_SECONDS = int(os.environ.get("AZURE_POLL_TIMEOUT_SECONDS") or "180")
# Comma-separated AnalyzeDocument features for final2 (prebuilt-read).
# Empty ⇒ no features= kwarg (text only). Default matches the Azure V2 pack.
AZURE_DI_FEATURES = (
    os.environ.get("AZURE_DI_FEATURES") or "languages,barcodes"
).strip()
# Application-level retries around DI / Blob / OpenAI calls (on top of the SDK).
AZURE_RETRY_ATTEMPTS = int(os.environ.get("AZURE_RETRY_ATTEMPTS") or "5")
AZURE_RETRY_BASE_DELAY = float(os.environ.get("AZURE_RETRY_BASE_DELAY") or "1.0")
AZURE_RETRY_MAX_DELAY = float(os.environ.get("AZURE_RETRY_MAX_DELAY") or "30.0")
# Global cap on concurrent Azure Document Intelligence analyzes across all
# in-flight charts. Independent of BATCH_WORKERS — stage 5 is billed and
# rate-limited per resource, not per chart.
AZURE_DI_MAX_CONCURRENT = int(os.environ.get("AZURE_DI_MAX_CONCURRENT") or "4")

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
# Stage 1 measures orientation, tilt and mirror, and writes a corrected image
# to corrected-pages/ which every later stage reads in place of the original.
#
# ON by default, on measurement. Coarse rotation comes from Tesseract OSD
# (stages/lib/imaging/osd.py), not the geometric detector, which recovered 0 of
# 6 sideways pages while reporting confidence 1.000 on the wrong answers.
#
# Round trip — rotate a page, detect, correct, compare with the original:
#
#     exact pixel recovery   9 of 9 on pages with readable text
#     the 3 non-recoveries are one near-blank page (6 characters) where OSD
#     declines to judge and we leave the page untouched
#
# And what it is worth, as OCR text similarity to the upright page:
#
#     page rotated 270 CW   uncorrected 0.011-0.015   corrected 1.000
#     page rotated  90 CW   uncorrected 0.848-1.000   corrected 1.000
#
# A 270-degree page OCRs to near-total garbage uncorrected. Character COUNT
# hides this — the garbage has more characters than the correct text — which is
# why the check compares the text itself.
#
# Set false to record orientation without rewriting any image.
ROTATION_CORRECTION_ENABLED = (
    os.environ.get("ROTATION_CORRECTION_ENABLED") or "true"
).strip().casefold() in {"1", "true", "yes", "on"}


def _flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().casefold()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# When true, ocr_prelim / ocr_final1 / ocr_final2 reuse existing artifacts if
# the chart's *output folder* ``ocr/`` (or workspace ``ocr/``) already has
# usable files (re-hydrated into ocr_results), OR if ocr_results already has
# rows (materialized back into the three ocr/ files). If neither is available,
# SKIP_OCR is ignored and OCR runs normally. force=True always re-OCRs.
# With skip_ocr + force=False, quality is refreshed and gate-delta reopens
# only pages whose HW/quality/rotation path changed (see stages/gate_delta.py).
SKIP_OCR = _flag("SKIP_OCR", False)

# Batch: charts with this many pages (or more) share a single concurrency slot
# while any smaller chart is still pending. When only large charts remain, the
# normal worker pool applies. Override with LARGE_CHART_MIN_PAGES.
LARGE_CHART_MIN_PAGES = int(os.environ.get("LARGE_CHART_MIN_PAGES") or "100")


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

# Page sequencing cross-encoder (optional ONNX). Off by default — markers /
# header groups / original order still run. Drop cross_encoder_mini_lm.onnx
# under stages/lib/sequencing/artifacts/ and set true to enable.
SEQUENCING_CROSS_ENCODER = _flag("SEQUENCING_CROSS_ENCODER", False)

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
# lever. Keep STAGE_WORKERS <= DB_POOL_MAX.
STAGE_WORKERS = int(os.environ.get("STAGE_WORKERS") or "4")
# Docling + Torch RapidOCR is not safe/fast under heavy fan-out — default 1.
DOCLING_WORKERS = int(os.environ.get("DOCLING_WORKERS") or "1")
# Per-page wall clock for Docling; on timeout/empty content final1 falls back
# to RapidOCR-onnx so the rest of the chain is not blocked forever.
# 90s default: ACCURATE+cell-matching was measured at ~778s/page on large TIFFs.
DOCLING_PAGE_TIMEOUT_SECONDS = float(
    os.environ.get("DOCLING_PAGE_TIMEOUT_SECONDS") or "90"
)
# Final1 section_headers JSON: keep only candidates ≥ threshold similar to a
# known clinical header (MiniLM when sentence-transformers is installed).
SECTION_HEADER_SEMANTIC_ENABLED = _flag("SECTION_HEADER_SEMANTIC_ENABLED", True)
SECTION_HEADER_SEMANTIC_THRESHOLD = float(
    os.environ.get("SECTION_HEADER_SEMANTIC_THRESHOLD") or "0.90"
)
SECTION_HEADER_MINILM_MODEL = (
    os.environ.get("SECTION_HEADER_MINILM_MODEL")
    or "sentence-transformers/all-MiniLM-L6-v2"
).strip()
# Charts processed concurrently inside one /batch call. Default 4 so a drop of
# small charts saturates the box; set 1 to restore serial behaviour.
# Invariant: BATCH_WORKERS × STAGE_WORKERS + BATCH_POOL_HEADROOM ≤ DB_POOL_MAX.
BATCH_WORKERS = int(os.environ.get("BATCH_WORKERS") or "4")
BATCH_POOL_HEADROOM = int(os.environ.get("BATCH_POOL_HEADROOM") or "2")

TESSERACT_CMD = (os.environ.get("TESSERACT_CMD") or "").strip() or None


def _path_under_core(raw: str | None, default_rel: str) -> Path:
    """Resolve a path relative to core-pipeline/ (where config.py lives).

    Absolute paths and ``~/…`` are accepted as-is. Relative values like
    ``models/hw/….pth`` are always from ``CORE_ROOT``, not the process cwd.
    """
    value = (raw or "").strip() or default_rel
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = CORE_ROOT / path
    return path.resolve()


# ConvNeXt HW weights (not pip-installable). Place the .pth under models/hw/.
# Preferred: handwritten_printed_convnext_tiny.pth; optional prior weights as *_backup.pth.
HW_MODEL_PATH = _path_under_core(
    os.environ.get("HW_MODEL_PATH"),
    "models/hw/handwritten_printed_convnext_tiny.pth",
)
# RapidOCR torch models for Docling final1 (download separately — not on PyPI).
# Expected files: PP-OCRv6_det_small.pth, PP-OCRv6_rec_small.pth,
# ch_ptocr_mobile_v2.0_cls_mobile.pth, ppocrv6_dict.txt
RAPID_MODELS_DIR = _path_under_core(
    os.environ.get("RAPID_MODELS_DIR"),
    "models/rapidocr",
)
# Local MiniLM checkout for section-header filtering. Preferred over Hub id
# when the directory exists. Download:
#   python -m stages.lib.imaging.section_header_match --download
SECTION_HEADER_MINILM_PATH = _path_under_core(
    os.environ.get("SECTION_HEADER_MINILM_PATH"),
    "models/semantic-model",
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

    Exception: TIFF/TIF sources are always written here as ``{stem}.jpg`` so
    later OCR stages (and the review-ui) never have to open a multi-page TIFF.
    """
    return chart_dir(chart_name) / "corrected-pages"


_TIFF_SUFFIXES = {".tif", ".tiff"}


def corrected_page_filename(page_name: str) -> str:
    """Filename under corrected-pages/ — TIFF sources become ``.jpg``."""
    path = Path(page_name)
    if path.suffix.lower() in _TIFF_SUFFIXES:
        return f"{path.stem}.jpg"
    return page_name


def page_image_path(chart_name: str, page_name: str) -> Path:
    """The image a stage should actually read: corrected if one exists.

    Every stage that opens a page image goes through here, so "use the
    corrected page when there is one" is a single rule rather than four copies
    of the same `if`. Pages needing no correction fall through to pages/, which
    is also what happens for a chart processed before corrections existed.

    For TIFF originals, ``corrected-pages/{stem}.jpg`` is preferred when present.
    """
    cdir = corrected_pages_dir(chart_name)
    preferred = cdir / corrected_page_filename(page_name)
    if preferred.is_file():
        return preferred
    same_name = cdir / page_name
    if same_name.is_file():
        return same_name
    return pages_dir(chart_name) / page_name


def page_image_source(chart_name: str, page_name: str) -> tuple[bool, str]:
    """``(use_corrected, chart-relative image_path)`` matching ``page_image_path``.

    Paths use forward slashes: ``pages/1.jpg`` or ``corrected-pages/1.jpg``.
    """
    path = page_image_path(chart_name, page_name)
    root = chart_dir(chart_name)
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        rel = f"pages/{page_name}"
    use_corrected = rel.startswith("corrected-pages/")
    return use_corrected, rel


def ensure_chart_dirs(chart_name: str) -> Path:
    root = chart_dir(chart_name)
    for sub in ("pages", "ocr", "imaging", "corrected-pages"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


# The member NER package reads these from the environment at import time.
os.environ.setdefault("MEMBER_NER_ENABLED", "true" if MEMBER_NER_ENABLED else "false")
os.environ.setdefault("MEMBER_NER_MODEL_ID", MEMBER_NER_MODEL_ID)
if MEMBER_NER_MODELS_PATH:
    os.environ.setdefault("MEMBER_NER_MODELS_PATH", MEMBER_NER_MODELS_PATH)
