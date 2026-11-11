"""How review-ui signs in to Cosmos (2026-11-11, user): the account key (COSMOS_AUTH=key, the
default) or the host's managed identity (COSMOS_AUTH=managed_identity; COSMOS_CLIENT_ID, else
AZURE_CLIENT_ID, names a user-assigned identity; blank = system-assigned). A managed identity
needs the Cosmos data-plane role "Cosmos DB Built-in Data Contributor" on the account."""
from __future__ import annotations

from typing import Any

MANAGED_IDENTITY = frozenset({"managed_identity", "managed-identity", "managed_entity", "mi", "msi"})


def uses_managed_identity(auth: str | None) -> bool:
    return (auth or "").strip().lower() in MANAGED_IDENTITY


def configured(endpoint: str | None, key: str | None, auth: str | None) -> bool:
    """Enough settings to try: an endpoint, and a key unless signing in as a managed identity."""
    return bool((endpoint or "").strip() and (uses_managed_identity(auth) or (key or "").strip()))


def credential(key: str | None, auth: str | None, client_id: str | None = None) -> Any:
    """The credential CosmosClient takes: the key, or a ManagedIdentityCredential."""
    if uses_managed_identity(auth):
        from azure.identity import ManagedIdentityCredential

        cid = (client_id or "").strip()
        return ManagedIdentityCredential(client_id=cid) if cid else ManagedIdentityCredential()
    return (key or "").strip()
