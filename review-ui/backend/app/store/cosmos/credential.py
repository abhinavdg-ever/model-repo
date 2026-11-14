"""How review-ui signs in to Cosmos, as core-pipeline (common/db/cosmos_store.py). COSMOS_AUTH:

  key (default)       the account key
  managed_identity    the host's managed identity (2026-11-11, user); COSMOS_CLIENT_ID, else
                      AZURE_CLIENT_ID, names a user-assigned one, blank = system-assigned
  service_principal   an Entra ID app registration (2026-11-14, user): COSMOS_TENANT_ID,
                      COSMOS_CLIENT_ID, COSMOS_CLIENT_SECRET (else the AZURE_* ones)
  entra               Entra ID through DefaultAzureCredential (2026-11-14, user): environment
                      variables, the managed identity, or `az login`

Every Entra ID sign-in needs the Cosmos data-plane role "Cosmos DB Built-in Data Contributor"."""
from __future__ import annotations

from typing import Any

MANAGED_IDENTITY = frozenset({"managed_identity", "managed-identity", "managed_entity", "mi", "msi"})
SERVICE_PRINCIPAL = frozenset({"service_principal", "service-principal", "sp", "client_secret", "app"})
ENTRA = frozenset({"entra", "entra_id", "entra-id", "aad", "azure_ad", "default"})


def mode(auth: str | None) -> str:
    value = (auth or "").strip().lower()
    if value in MANAGED_IDENTITY:
        return "managed_identity"
    if value in SERVICE_PRINCIPAL:
        return "service_principal"
    if value in ENTRA:
        return "entra"
    return "key"


def uses_managed_identity(auth: str | None) -> bool:
    return mode(auth) == "managed_identity"


def configured(endpoint: str | None, key: str | None, auth: str | None,
               tenant_id: str | None = None, client_id: str | None = None,
               client_secret: str | None = None) -> bool:
    """Enough settings to try: an endpoint, plus the key, or what the Entra ID mode needs."""
    if not (endpoint or "").strip():
        return False
    kind = mode(auth)
    if kind in {"managed_identity", "entra"}:
        return True
    if kind == "service_principal":
        return all((v or "").strip() for v in (tenant_id, client_id, client_secret))
    return bool((key or "").strip())


def credential(key: str | None, auth: str | None, client_id: str | None = None,
               tenant_id: str | None = None, client_secret: str | None = None) -> Any:
    """The credential CosmosClient takes: the key, or an Entra ID token credential."""
    kind = mode(auth)
    cid = (client_id or "").strip()
    if kind == "managed_identity":
        from azure.identity import ManagedIdentityCredential

        return ManagedIdentityCredential(client_id=cid) if cid else ManagedIdentityCredential()
    if kind == "service_principal":
        from azure.identity import ClientSecretCredential

        return ClientSecretCredential(tenant_id=(tenant_id or "").strip(), client_id=cid,
                                      client_secret=(client_secret or "").strip())
    if kind == "entra":
        from azure.identity import DefaultAzureCredential

        kwargs: dict[str, Any] = {"exclude_interactive_browser_credential": True}
        if cid:
            kwargs["managed_identity_client_id"] = cid
        return DefaultAzureCredential(**kwargs)
    return (key or "").strip()
