"""Azure Blob for db-utilities: list and download manifest / ground-truth files.

The same settings as core-pipeline/.env (AZURE_STORAGE_*), read from db-utilities/.env:
a connection string or account key when set, else AZURE_STORAGE_AUTH =
managed_identity (the VM; AZURE_CLIENT_ID = the user-assigned identity) or entra
(DefaultAzureCredential: managed identity, then `az login`).
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from dbutil import db  # noqa: F401  (loads db-utilities/.env)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


@lru_cache(maxsize=1)
def service() -> Any:
    from azure.storage.blob import BlobServiceClient

    if _env("AZURE_STORAGE_CONNECTION_STRING"):
        return BlobServiceClient.from_connection_string(_env("AZURE_STORAGE_CONNECTION_STRING"))
    account = _env("AZURE_STORAGE_ACCOUNT_NAME")
    if not account:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_NAME is not set (db-utilities/.env)")
    url = f"https://{account}.blob.core.windows.net"
    if _env("AZURE_STORAGE_ACCOUNT_KEY"):
        return BlobServiceClient(url, credential=_env("AZURE_STORAGE_ACCOUNT_KEY"))
    if _env("AZURE_STORAGE_AUTH").casefold() == "managed_identity":
        from azure.identity import ManagedIdentityCredential

        credential = ManagedIdentityCredential(client_id=_env("AZURE_CLIENT_ID") or None)
    else:
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential(managed_identity_client_id=_env("AZURE_CLIENT_ID") or None)
    return BlobServiceClient(url, credential=credential)


def container_name(container: str | None) -> str:
    name = (container or _env("AZURE_STORAGE_CONTAINER")).strip()
    if not name:
        raise RuntimeError("no container: pass --blob-container or set AZURE_STORAGE_CONTAINER")
    return name


def list_blobs_with_suffixes(container: str, prefix: str, suffixes: tuple[str, ...]) -> list[str]:
    client = service().get_container_client(container_name(container))
    prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
    return sorted(
        b.name for b in client.list_blobs(name_starts_with=prefix)
        if b.name.casefold().endswith(tuple(s.casefold() for s in suffixes))
        and not b.name.rsplit("/", 1)[-1].startswith("._")
    )


def download_blob_bytes(container: str, name: str) -> bytes:
    return service().get_container_client(container_name(container)).download_blob(name).readall()
