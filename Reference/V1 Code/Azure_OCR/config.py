from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
if str(REPO_ROOT / "azure_blob") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "azure_blob"))

# Local progress + queue file only. Images + OCR JSON live in blob storage.
PROGRESS_FILE = HERE / "azure_ocr_progress.json"

OCR_JSON_SUFFIX = "_final2.json"

AZURE_POLL_TIMEOUT_SECONDS = 180


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


_load_repo_env()

AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT = (os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT") or "").strip()
AZURE_DOCUMENT_INTELLIGENCE_KEY = (os.environ.get("AZURE_DOCUMENT_INTELLIGENCE_KEY") or "").strip()
AZURE_POLL_TIMEOUT_SECONDS = int((os.environ.get("AZURE_POLL_TIMEOUT_SECONDS") or str(AZURE_POLL_TIMEOUT_SECONDS)).strip() or 180)
AZURE_STORAGE_CONTAINER = (os.environ.get("AZURE_STORAGE_CONTAINER") or "").strip()
