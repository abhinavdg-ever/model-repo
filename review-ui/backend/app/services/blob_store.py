from __future__ import annotations

import logging
import os
import re
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import ClientSecretCredential, DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from fastapi import HTTPException

from app.core.config import Settings, get_settings

logger = logging.getLogger("review_ui.blob")

_FILENAME_PARTS = re.compile(r"(\d+)")
_IMAGE_SUFFIXES = {
    ".bmp",
    ".dib",
    ".gif",
    ".j2k",
    ".jfif",
    ".jp2",
    ".jpe",
    ".jpeg",
    ".jpg",
    ".pbm",
    ".pgm",
    ".png",
    ".pnm",
    ".ppm",
    ".tif",
    ".tiff",
    ".webp",
}


def account_name_from_url(account_url: str) -> str:
    host = (urlparse(account_url).hostname or "").strip().lower()
    if not host:
        return ""
    return host.split(".")[0]


def filename_sort_key(name: str) -> tuple:
    """Natural sort matching core-pipeline ingest order for Raw_Input pages."""
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


def normalize_blob_prefix(path: str) -> str:
    p = (path or "").strip().replace("\\", "/").strip("/")
    return f"{p}/" if p else ""


def build_blob_key(
    template: str,
    *,
    folder_id: str,
    filename: str,
    page_number: int,
) -> str:
    tmpl = (template or "{folder}/pages/{filename}").strip()
    if "{folder}" not in tmpl and "{filename}" not in tmpl:
        # Prefix-only (e.g. Raw_Input/Run1/Batch1/DEID_PNGs/) → append folder/file
        base = tmpl.rstrip("/")
        key = f"{base}/{folder_id}/{filename}" if base else f"{folder_id}/{filename}"
    else:
        key = (
            tmpl.replace("{folder}", folder_id)
            .replace("{filename}", filename)
            .replace("{page}", str(page_number))
        )
    return key.lstrip("/")


def _normalize_account_url(raw: str) -> str:
    """Accept a full URL or a bare account name (e.g. "mystorageaccount")."""
    value = raw.strip().rstrip("/")
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"https://{value}.blob.core.windows.net"


def resolved_blob_account_url(settings: Settings | None = None) -> str:
    """BLOB_ACCOUNT_URL, else AZURE_STORAGE_ACCOUNT_NAME (same account as pipeline)."""
    cfg = settings or get_settings()
    raw = (cfg.blob_account_url or "").strip()
    if not raw:
        raw = (os.environ.get("AZURE_STORAGE_ACCOUNT_NAME") or "").strip()
    return _normalize_account_url(raw)


def _credential(settings: Settings):
    tenant = settings.azure_tenant_id.strip()
    client_id = settings.azure_client_id.strip()
    secret = settings.azure_client_secret.strip()
    if tenant and client_id and secret:
        return ClientSecretCredential(
            tenant_id=tenant,
            client_id=client_id,
            client_secret=secret,
        )
    # Managed Identity / Azure CLI / VS Code / environment credentials
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


@lru_cache
def _blob_service_client() -> BlobServiceClient:
    settings = get_settings()
    account_url = resolved_blob_account_url(settings)
    if not account_url:
        raise RuntimeError(
            "BLOB_ACCOUNT_URL (or AZURE_STORAGE_ACCOUNT_NAME) is not configured"
        )
    return BlobServiceClient(account_url=account_url, credential=_credential(settings))


def clear_blob_client_cache() -> None:
    _blob_service_client.cache_clear()


def list_image_blob_keys(container: str, blob_path: str) -> list[str]:
    """Image blob keys under a chart prefix, sorted like pipeline ingest."""
    container = (container or "").strip().strip("/")
    prefix = normalize_blob_prefix(blob_path)
    if not container or not prefix:
        return []
    client = _blob_service_client()
    names: list[str] = []
    for blob in client.get_container_client(container).list_blobs(
        name_starts_with=prefix
    ):
        filename = Path(blob.name).name
        if Path(filename).suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        if filename.startswith("._"):
            continue
        names.append(blob.name)
    names.sort(key=lambda n: filename_sort_key(Path(n).name))
    return names


def download_blob_at(
    *,
    container: str,
    key: str,
    filename: str | None = None,
) -> tuple[bytes, str]:
    """Download one blob by container + key (Entra / DefaultAzureCredential)."""
    container = (container or "").strip().strip("/")
    key = (key or "").lstrip("/")
    if not container or not key:
        raise HTTPException(status_code=400, detail="Blob container and key are required")
    if not resolved_blob_account_url():
        raise HTTPException(
            status_code=400,
            detail="BLOB_ACCOUNT_URL (or AZURE_STORAGE_ACCOUNT_NAME) must be set",
        )
    display_name = filename or Path(key).name

    try:
        client = _blob_service_client()
        blob = client.get_blob_client(container=container, blob=key)

        def _download() -> tuple[bytes, str]:
            downloader = blob.download_blob()
            data = downloader.readall()
            try:
                content_type = downloader.properties.content_settings.content_type
            except Exception:  # noqa: BLE001
                content_type = None
            return data, content_type or _guess_media_type(display_name)

        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                return _download()
            except ResourceNotFoundError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                status = getattr(exc, "status_code", None)
                transient = status in {408, 429, 500, 502, 503, 504} or status is None
                if not transient or attempt >= 3:
                    raise
                time.sleep(0.5 * attempt)
        assert last_exc is not None
        raise last_exc
    except ResourceNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Blob not found: {container}/{key}",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Blob download failed via Entra ID: {exc}",
        ) from exc


def download_blob_bytes(
    *,
    folder_id: str,
    filename: str,
    page_number: int,
) -> tuple[bytes, str]:
    """Download a page image via File Viewer template (Entra).

    Returns (content_bytes, media_type).
    """
    settings = get_settings()
    if not settings.file_viewer_blob_enabled:
        raise HTTPException(status_code=400, detail="Blob viewer is disabled")
    if settings.blob_auth_mode != "entra":
        raise HTTPException(status_code=400, detail="Blob auth mode is not entra")

    container = settings.blob_container.strip().strip("/")
    if not resolved_blob_account_url(settings) or not container:
        raise HTTPException(
            status_code=400,
            detail="BLOB_ACCOUNT_URL and BLOB_CONTAINER must be set for Entra blob access",
        )

    key = build_blob_key(
        settings.blob_path_template,
        folder_id=folder_id,
        filename=filename,
        page_number=page_number,
    )
    return download_blob_at(container=container, key=key, filename=filename)


def _guess_media_type(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith(".webp"):
        return "image/webp"
    if lower.endswith((".tif", ".tiff")):
        return "image/tiff"
    return "application/octet-stream"
