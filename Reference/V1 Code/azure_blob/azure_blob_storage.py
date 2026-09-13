from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
_CLIENT = None
_CREDENTIAL = None
_CLIENT_LOCK = threading.Lock()

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
_FILENAME_PARTS = re.compile(r"(\d+)")


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

AZURE_STORAGE_AUTH = (os.environ.get("AZURE_STORAGE_AUTH") or "entra").strip().casefold()
AZURE_STORAGE_CONNECTION_STRING = (os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or "").strip()
AZURE_STORAGE_ACCOUNT_NAME = (os.environ.get("AZURE_STORAGE_ACCOUNT_NAME") or "").strip()
AZURE_STORAGE_ACCOUNT_KEY = (os.environ.get("AZURE_STORAGE_ACCOUNT_KEY") or "").strip()
AZURE_STORAGE_CONTAINER = (os.environ.get("AZURE_STORAGE_CONTAINER") or "").strip()
AZURE_OCR_RAW_STORAGE_PREFIX = (os.environ.get("AZURE_OCR_RAW_STORAGE_PREFIX") or "").strip().strip("/")
AZURE_OCR_STORAGE_WRITE_PREFIX = (os.environ.get("AZURE_OCR_STORAGE_WRITE_PREFIX") or "").strip().strip("/")

OCR_JSON_SUFFIX = "_final2.json"


def filename_sort_key(name: str) -> tuple:
    """Natural ascending sort for file names (so 2.png comes before 10.png)."""
    text = str(name or "").casefold()
    parts: list = []
    for part in _FILENAME_PARTS.split(text):
        if not part:
            continue
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part))
    return tuple(parts)


def sort_ocr_pages_by_filename(document: dict) -> dict:
    """Sort pages by fileName ascending and renumber pageNumber 1..N."""
    pages = list(document.get("pages") or [])
    pages.sort(key=lambda page: filename_sort_key(str(page.get("fileName") or "")))
    for index, page in enumerate(pages, start=1):
        page["pageNumber"] = index
    document["pages"] = pages
    document["pageCount"] = len(pages)
    return document


def use_entra() -> bool:
    return AZURE_STORAGE_AUTH in {"entra", "aad", "azuread"}


def storage_configured() -> bool:
    if not AZURE_STORAGE_CONTAINER:
        return False
    if use_entra():
        return bool(AZURE_STORAGE_ACCOUNT_NAME)
    if AZURE_STORAGE_CONNECTION_STRING:
        return True
    return bool(AZURE_STORAGE_ACCOUNT_NAME and AZURE_STORAGE_ACCOUNT_KEY)


def require_raw_prefix() -> str:
    if not AZURE_OCR_RAW_STORAGE_PREFIX:
        raise RuntimeError("AZURE_OCR_RAW_STORAGE_PREFIX is required in the repo-root .env")
    return AZURE_OCR_RAW_STORAGE_PREFIX


def require_write_prefix() -> str:
    if not AZURE_OCR_STORAGE_WRITE_PREFIX:
        raise RuntimeError("AZURE_OCR_STORAGE_WRITE_PREFIX is required in the repo-root .env")
    return AZURE_OCR_STORAGE_WRITE_PREFIX


def _entra_credential():
    global _CREDENTIAL
    if _CREDENTIAL is not None:
        return _CREDENTIAL

    from azure.identity import (
        AzureCliCredential,
        ChainedTokenCredential,
        InteractiveBrowserCredential,
        TokenCachePersistenceOptions,
    )

    cache = TokenCachePersistenceOptions(name="ner_blob_storage", allow_unencrypted_storage=True)
    # Browser-first with persistent cache so parallel workers reuse one login.
    # Azure CLI is a fast path when available.
    _CREDENTIAL = ChainedTokenCredential(
        InteractiveBrowserCredential(cache_persistence_options=cache),
        AzureCliCredential(process_timeout=30),
    )
    return _CREDENTIAL


def blob_service_client():
    """Return a process-wide BlobServiceClient (thread-safe singleton)."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT

        from azure.core.pipeline.transport import RequestsTransport
        from azure.storage.blob import BlobServiceClient

        # Parallel OCR downloads/uploads need more than the default pool of 10.
        transport = RequestsTransport(connection_pool_maxsize=50)

        if use_entra():
            if not AZURE_STORAGE_ACCOUNT_NAME:
                raise RuntimeError("AZURE_STORAGE_ACCOUNT_NAME is required for Entra auth")
            logger.info("blob auth=microsoft_entra account=%s", AZURE_STORAGE_ACCOUNT_NAME)
            _CLIENT = BlobServiceClient(
                account_url=f"https://{AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
                credential=_entra_credential(),
                transport=transport,
            )
            return _CLIENT

        if AZURE_STORAGE_CONNECTION_STRING:
            logger.info("blob auth=connection_string")
            _CLIENT = BlobServiceClient.from_connection_string(
                AZURE_STORAGE_CONNECTION_STRING,
                transport=transport,
            )
            return _CLIENT

        logger.info("blob auth=account_key account=%s", AZURE_STORAGE_ACCOUNT_NAME)
        _CLIENT = BlobServiceClient(
            account_url=f"https://{AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
            credential=AZURE_STORAGE_ACCOUNT_KEY,
            transport=transport,
        )
        return _CLIENT


def ensure_blob_ready() -> None:
    """Authenticate once before parallel workers start.

    Prevents multiple InteractiveBrowserCredential tabs / state-mismatch errors.
    """
    client = blob_service_client()
    if use_entra():
        logger.info("blob auth: acquiring token (browser may open once)...")
        # Force a single token fetch before any worker threads run.
        credential = _entra_credential()
        credential.get_token("https://storage.azure.com/.default")
        logger.info("blob auth: token ready")
    # Touch the container so the first network call is also single-threaded.
    container = client.get_container_client(AZURE_STORAGE_CONTAINER)
    next(container.list_blobs(name_starts_with="", results_per_page=1), None)
    logger.info("blob client ready container=%s", AZURE_STORAGE_CONTAINER)


def container_client():
    if not storage_configured():
        raise RuntimeError(
            "Azure Blob Storage is not configured. Set AZURE_STORAGE_CONTAINER and "
            "ACCOUNT_NAME (Entra) or CONNECTION_STRING / ACCOUNT_KEY in .env"
        )
    return blob_service_client().get_container_client(AZURE_STORAGE_CONTAINER)


def record_ocr_blob_name(record_id: str) -> str:
    prefix = require_write_prefix()
    return f"{prefix}/{record_id}/{record_id}{OCR_JSON_SUFFIX}"


def _list_record_ids_under(prefix: str) -> list[str]:
    client = container_client()
    base = f"{prefix}/" if prefix else ""
    names: list[str] = []
    for item in client.walk_blobs(name_starts_with=base, delimiter="/"):
        name = str(getattr(item, "name", "") or "")
        if not name.endswith("/") and type(item).__name__ != "BlobPrefix":
            continue
        relative = name[len(base) :] if base and name.startswith(base) else name
        folder = relative.strip("/")
        if folder and "/" not in folder:
            names.append(folder)
    if not names:
        for blob in client.list_blobs(name_starts_with=base):
            relative = blob.name[len(base) :] if base and blob.name.startswith(base) else blob.name
            relative = relative.strip("/")
            if not relative:
                continue
            folder = relative.split("/", 1)[0]
            if folder:
                names.append(folder)
    return sorted(set(names))


def list_raw_record_ids() -> list[str]:
    """Record folders under AZURE_OCR_RAW_STORAGE_PREFIX."""
    prefix = require_raw_prefix()
    record_ids = _list_record_ids_under(prefix)
    logger.info(
        "blob raw records container=%s prefix=%s count=%s",
        AZURE_STORAGE_CONTAINER,
        prefix,
        len(record_ids),
    )
    return record_ids


def list_ocr_record_ids() -> list[str]:
    """Record folders under AZURE_OCR_STORAGE_WRITE_PREFIX."""
    prefix = require_write_prefix()
    record_ids = _list_record_ids_under(prefix)
    logger.info(
        "blob OCR records container=%s prefix=%s count=%s",
        AZURE_STORAGE_CONTAINER,
        prefix,
        len(record_ids),
    )
    return record_ids


def list_raw_page_blobs(record_id: str) -> list[tuple[str, str]]:
    """Return sorted (file_name, blob_name) image pages for a raw record folder."""
    client = container_client()
    base = f"{require_raw_prefix()}/{record_id}/"
    pages: list[tuple[str, str]] = []
    for blob in client.list_blobs(name_starts_with=base):
        name = str(blob.name or "")
        relative = name[len(base) :] if name.startswith(base) else name
        relative = relative.strip("/")
        if not relative or "/" in relative:
            continue
        suffix = Path(relative).suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            continue
        pages.append((relative, name))
    pages.sort(key=lambda item: filename_sort_key(item[0]))
    return pages


def upload_json(blob_name: str, data: dict) -> None:
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    client = container_client()
    logger.info("blob upload %s (%s bytes)", blob_name, len(payload))
    client.upload_blob(name=blob_name, data=payload, overwrite=True, content_type="application/json")


def download_bytes(blob_name: str) -> bytes:
    client = container_client()
    blob = client.get_blob_client(blob_name)
    if not blob.exists():
        raise FileNotFoundError(f"Blob not found: {blob_name}")
    return blob.download_blob().readall()


def download_json(blob_name: str) -> dict | None:
    client = container_client()
    blob = client.get_blob_client(blob_name)
    if not blob.exists():
        return None
    raw = blob.download_blob().readall()
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, dict) else None


def save_ocr_document(record_id: str, document: dict) -> str:
    sort_ocr_pages_by_filename(document)
    blob_name = record_ocr_blob_name(record_id)
    upload_json(blob_name, document)
    return blob_name


def load_ocr_document(record_id: str) -> dict | None:
    return download_json(record_ocr_blob_name(record_id))


def load_ocr_pages(record_id: str) -> list[dict]:
    data = load_ocr_document(record_id)
    if not data:
        return []
    pages = data.get("pages")
    return list(pages) if isinstance(pages, list) else []
