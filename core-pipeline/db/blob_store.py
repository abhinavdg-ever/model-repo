"""Azure Blob helpers for chart page download and manifest sweep."""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from config import (
    AZURE_STORAGE_ACCOUNT_KEY,
    AZURE_STORAGE_ACCOUNT_NAME,
    AZURE_STORAGE_AUTH,
    AZURE_STORAGE_CONNECTION_STRING,
    AZURE_STORAGE_CONTAINER,
    IMAGE_SUFFIXES,
)

logger = logging.getLogger(__name__)
_FILENAME_PARTS = re.compile(r"(\d+)")


def filename_sort_key(name: str) -> tuple:
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


def normalize_prefix(path: str) -> str:
    p = (path or "").strip().strip("/")
    return f"{p}/" if p else ""


def chart_name_from_blob_path(blob_path: str) -> str:
    """Last non-empty path segment is the chart folder name."""
    parts = [p for p in blob_path.strip("/").split("/") if p]
    if not parts:
        raise ValueError("blob_path is empty")
    return parts[-1]


def get_blob_service_client():
    from azure.storage.blob import BlobServiceClient

    if AZURE_STORAGE_CONNECTION_STRING:
        return BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)

    account = AZURE_STORAGE_ACCOUNT_NAME
    if not account:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_NAME is required")

    account_url = f"https://{account}.blob.core.windows.net"
    auth = AZURE_STORAGE_AUTH
    if auth in {"entra", "aad", "azuread"}:
        from azure.identity import DefaultAzureCredential

        return BlobServiceClient(account_url, credential=DefaultAzureCredential())
    if AZURE_STORAGE_ACCOUNT_KEY:
        return BlobServiceClient(
            account_url, credential=AZURE_STORAGE_ACCOUNT_KEY
        )
    raise RuntimeError("No Azure Storage credentials configured")


def get_container_client(container: Optional[str] = None):
    name = (container or AZURE_STORAGE_CONTAINER).strip()
    if not name:
        raise RuntimeError("blob container name is required")
    return get_blob_service_client().get_container_client(name)


def list_image_blobs(container: str, blob_path: str) -> list[str]:
    """List image blob names under a chart folder prefix."""
    prefix = normalize_prefix(blob_path)
    client = get_container_client(container)
    names: list[str] = []
    for blob in client.list_blobs(name_starts_with=prefix):
        filename = Path(blob.name).name
        if Path(filename).suffix.lower() not in IMAGE_SUFFIXES:
            continue
        # only immediate children of the chart folder
        relative = blob.name[len(prefix) :] if blob.name.startswith(prefix) else blob.name
        if "/" in relative.strip("/"):
            continue
        names.append(blob.name)
    names.sort(key=lambda n: filename_sort_key(Path(n).name))
    return names


def download_blob_to_path(container: str, blob_name: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    client = get_container_client(container)
    data = client.download_blob(blob_name).readall()
    dest.write_bytes(data)
    return dest


def list_blobs_with_suffixes(
    container: str, prefix: str, suffixes: tuple[str, ...]
) -> list[str]:
    pref = normalize_prefix(prefix)
    client = get_container_client(container)
    out: list[str] = []
    for blob in client.list_blobs(name_starts_with=pref):
        name = blob.name.casefold()
        if any(name.endswith(s.casefold()) for s in suffixes):
            out.append(blob.name)
    return sorted(out)


def download_blob_bytes(container: str, blob_name: str) -> bytes:
    client = get_container_client(container)
    return client.download_blob(blob_name).readall()
