from __future__ import annotations

import logging
import re
import threading
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError
from azure.identity import ClientSecretCredential, DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from fastapi import HTTPException

from app.core.config import Settings, get_settings

logger = logging.getLogger("review_ui.blob")
# DefaultAzureCredential logs a page of attempts on every failed sign-in; blob_unavailable()
# reports the failure once instead.
logging.getLogger("azure.identity").setLevel(logging.ERROR)

# When signing in to Blob fails (no az login, no managed identity), every image would wait
# for the whole credential chain again. Blob is skipped for this long, the image route uses
# the local copy, and POST /api/blob/retry ends the wait early.
BLOB_RETRY_AFTER_SEC = 300.0
_blob_down: dict[str, float | str] = {"until": 0.0, "reason": ""}
_blob_down_lock = threading.Lock()


def blob_unavailable() -> str | None:
    """Why Blob is being skipped right now, or None."""
    with _blob_down_lock:
        if time.monotonic() < float(_blob_down["until"]):
            return str(_blob_down["reason"])
    return None


def mark_blob_unavailable(exc: BaseException) -> str:
    reason = f"Azure Blob sign-in failed ({type(exc).__name__}); set AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET, or run az login"
    with _blob_down_lock:
        first = time.monotonic() >= float(_blob_down["until"])
        _blob_down.update(until=time.monotonic() + BLOB_RETRY_AFTER_SEC, reason=reason)
    if first:
        logger.warning("%s. Skipping Blob for %d s; images come from the local copy.", reason, BLOB_RETRY_AFTER_SEC)
    return reason


def reset_blob_access() -> None:
    """Try Blob again on the next request (a fresh client and credential)."""
    with _blob_down_lock:
        _blob_down.update(until=0.0, reason="")
    clear_blob_client_cache()


def _skip_if_unavailable() -> None:
    reason = blob_unavailable()
    if reason:
        raise HTTPException(status_code=503, detail=reason)

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


def _connection_string_account(connection_string: str) -> str:
    """AccountName=... from an Azure Storage connection string, else ""."""
    for part in connection_string.split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "accountname":
            return value.strip()
    return ""


def resolved_blob_account_url(settings: Settings | None = None) -> str:
    """BLOB_ACCOUNT_URL, else AZURE_STORAGE_ACCOUNT_NAME, else the connection
    string's account (the same variables as core-pipeline)."""
    cfg = settings or get_settings()
    raw = (
        (cfg.blob_account_url or "").strip()
        or (cfg.azure_storage_account_name or "").strip()
        or _connection_string_account(cfg.azure_storage_connection_string or "")
    )
    return _normalize_account_url(raw)


# The same names core-pipeline takes for AZURE_STORAGE_AUTH.
_MANAGED_IDENTITY_MODES = {"managed_identity", "managed-identity", "mi", "msi"}
# A client secret's ID is a GUID; its Value is not. The ID is a common paste mistake and
# Entra rejects it: "secret should be a Microsoft Entra application's client secret".
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _credential(settings: Settings):
    tenant = settings.azure_tenant_id.strip()
    client_id = settings.azure_client_id.strip()
    secret = settings.azure_client_secret.strip()
    if settings.azure_storage_auth.strip().lower() in _MANAGED_IDENTITY_MODES:
        # The VM's managed identity, as core-pipeline uses it (AZURE_CLIENT_ID = a
        # user-assigned identity; blank = system-assigned). No secret involved.
        from azure.identity import ManagedIdentityCredential

        return ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
    if secret and _GUID.match(secret):
        logger.error(
            "AZURE_CLIENT_SECRET is a GUID: that is the secret's ID, not its Value. Put the "
            "Value in review-ui/.env, or set AZURE_STORAGE_AUTH=managed_identity on the VM."
        )
    if tenant and client_id and secret:
        return ClientSecretCredential(
            tenant_id=tenant,
            client_id=client_id,
            client_secret=secret,
        )
    # Managed Identity / Azure CLI / VS Code / environment credentials
    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


# Requests on the API's worker threads share one client. Its default HTTP pool keeps 10
# connections, so parallel blob reads logged "Connection pool is full, discarding connection".
_POOL_SIZE = 64


def _pooled_session():
    import requests
    from requests.adapters import HTTPAdapter

    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=_POOL_SIZE, pool_maxsize=_POOL_SIZE)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


@lru_cache
def _blob_service_client() -> BlobServiceClient:
    settings = get_settings()
    account_url = resolved_blob_account_url(settings)
    if not account_url:
        raise RuntimeError(
            "BLOB_ACCOUNT_URL (or AZURE_STORAGE_ACCOUNT_NAME) is not configured"
        )
    connection_string = settings.azure_storage_connection_string.strip()
    if connection_string:
        return BlobServiceClient.from_connection_string(connection_string, session=_pooled_session())
    account_key = settings.azure_storage_account_key.strip()
    if account_key:
        return BlobServiceClient(account_url=account_url, credential=account_key, session=_pooled_session())
    return BlobServiceClient(
        account_url=account_url, credential=_credential(settings), session=_pooled_session()
    )


def blob_auth_label(settings: Settings) -> str:
    """Which credential _blob_service_client uses, for the startup banner."""
    if settings.blob_auth_mode == "sas":
        return "sas token" if settings.blob_sas_token.strip() else "sas (no BLOB_SAS_TOKEN)"
    if settings.azure_storage_connection_string.strip():
        return "connection string"
    if settings.azure_storage_account_key.strip():
        return "account key"
    if settings.azure_tenant_id.strip() and settings.azure_client_id.strip() and settings.azure_client_secret.strip():
        return "entra app (client secret)"
    return "entra (managed identity / az login)"


def check_blob_access(settings: Settings) -> str:
    """Reach the default container once: 'OK', or why not. Never raises."""
    container = settings.blob_container.strip().strip("/")
    if not resolved_blob_account_url(settings):
        return "off — AZURE_STORAGE_ACCOUNT_NAME (or BLOB_ACCOUNT_URL) not set"
    if settings.blob_auth_mode == "sas":
        return "not checked (sas redirect)"
    if not container:
        return "not checked — no default container (AZURE_STORAGE_CONTAINER); each chart's blob_container is used"
    try:
        client = _blob_service_client().get_container_client(container)
        return "OK" if client.exists(timeout=10) else f"container {container!r} not found"
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        return f"FAILED — {type(exc).__name__}: {str(exc).splitlines()[0][:200]}"


def clear_blob_client_cache() -> None:
    _blob_service_client.cache_clear()


def list_image_blobs(container: str, blob_path: str) -> list[tuple[str, str]]:
    """``(key, etag)`` for image blobs under a chart prefix, sorted like ingest."""
    container = (container or "").strip().strip("/")
    prefix = normalize_blob_prefix(blob_path)
    if not container or not prefix:
        return []
    _skip_if_unavailable()
    client = _blob_service_client()
    out: list[tuple[str, str]] = []
    try:
        blobs = list(client.get_container_client(container).list_blobs(name_starts_with=prefix))
    except ClientAuthenticationError as exc:
        raise HTTPException(status_code=503, detail=mark_blob_unavailable(exc)) from exc
    for blob in blobs:
        filename = Path(blob.name).name
        if Path(filename).suffix.lower() not in _IMAGE_SUFFIXES:
            continue
        if filename.startswith("._"):
            continue
        out.append((blob.name, str(blob.etag or "").strip('"')))
    out.sort(key=lambda item: filename_sort_key(Path(item[0]).name))
    return out


def list_image_blob_keys(container: str, blob_path: str) -> list[str]:
    """Image blob keys under a chart prefix, sorted like pipeline ingest."""
    return [key for key, _ in list_image_blobs(container, blob_path)]


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
    _skip_if_unavailable()

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
            except (ResourceNotFoundError, ClientAuthenticationError):
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
    except ClientAuthenticationError as exc:
        raise HTTPException(status_code=503, detail=mark_blob_unavailable(exc)) from exc
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
            detail="BLOB_ACCOUNT_URL (or AZURE_STORAGE_ACCOUNT_NAME) and BLOB_CONTAINER "
            "(or AZURE_STORAGE_CONTAINER) must be set for blob access",
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
