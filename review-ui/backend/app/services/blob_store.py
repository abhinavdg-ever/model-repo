from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse

from azure.core.exceptions import ResourceNotFoundError
from azure.identity import ClientSecretCredential, DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from fastapi import HTTPException

from app.core.config import Settings, get_settings


def account_name_from_url(account_url: str) -> str:
    host = (urlparse(account_url).hostname or "").strip().lower()
    if not host:
        return ""
    return host.split(".")[0]


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
    """Accept full URL or bare account name (azsadve2aipoc)."""
    value = raw.strip().rstrip("/")
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return f"https://{value}.blob.core.windows.net"


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
    account_url = _normalize_account_url(settings.blob_account_url)
    if not account_url:
        raise RuntimeError("BLOB_ACCOUNT_URL is not configured")
    return BlobServiceClient(account_url=account_url, credential=_credential(settings))


def clear_blob_client_cache() -> None:
    _blob_service_client.cache_clear()


def download_blob_bytes(
    *,
    folder_id: str,
    filename: str,
    page_number: int,
) -> tuple[bytes, str]:
    """Download a page image from Azure Blob using Entra ID credentials.

    Returns (content_bytes, media_type).
    """
    settings = get_settings()
    if not settings.file_viewer_blob_enabled:
        raise HTTPException(status_code=400, detail="Blob viewer is disabled")
    if settings.blob_auth_mode != "entra":
        raise HTTPException(status_code=400, detail="Blob auth mode is not entra")

    container = settings.blob_container.strip().strip("/")
    if not settings.blob_account_url.strip() or not container:
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

    try:
        client = _blob_service_client()
        blob = client.get_blob_client(container=container, blob=key)
        downloader = blob.download_blob()
        data = downloader.readall()
        try:
            content_type = downloader.properties.content_settings.content_type
        except Exception:  # noqa: BLE001
            content_type = None
        return data, content_type or _guess_media_type(filename)
    except ResourceNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Blob not found: {container}/{key}",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — surface Azure auth/network errors to API clients
        raise HTTPException(
            status_code=502,
            detail=f"Blob download failed via Entra ID: {exc}",
        ) from exc


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
