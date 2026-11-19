"""How review-ui signs in to Cosmos, as core-pipeline (common/db/cosmos_store.py). COSMOS_AUTH:

  key (default)       the account key
  managed_identity    the host's managed identity (2026-11-11, user); COSMOS_CLIENT_ID, else
                      AZURE_CLIENT_ID, names a user-assigned one, blank = system-assigned
  service_principal   an Entra ID app registration (2026-11-14, user): COSMOS_TENANT_ID,
                      COSMOS_CLIENT_ID, COSMOS_CLIENT_SECRET (else the AZURE_* ones)
  entra               Entra ID through DefaultAzureCredential (2026-11-14, user): environment
                      variables, the managed identity, or `az login`
  entra_interactive   (2026-11-19, user) the managed identity, then `az login`, then a browser
                      prompt as the last resort; the sign-in is remembered in ~/.advantmed/ and
                      shared with core-pipeline (the same token cache and record)

Every Entra ID sign-in needs the Cosmos data-plane role "Cosmos DB Built-in Data Contributor"."""
from __future__ import annotations

from typing import Any

MANAGED_IDENTITY = frozenset({"managed_identity", "managed-identity", "managed_entity", "mi", "msi"})
SERVICE_PRINCIPAL = frozenset({"service_principal", "service-principal", "sp", "client_secret", "app"})
ENTRA = frozenset({"entra", "entra_id", "entra-id", "aad", "azure_ad", "default"})
ENTRA_INTERACTIVE = frozenset({"entra_interactive", "entra-interactive", "interactive", "browser"})
# core-pipeline's names (common/db/blob_store.py): one sign-in per machine for both services.
TOKEN_CACHE = "advantmed_blob_storage"
AUTH_RECORD = ".advantmed/azure_auth_record.json"


def mode(auth: str | None) -> str:
    value = (auth or "").strip().lower()
    if value in MANAGED_IDENTITY:
        return "managed_identity"
    if value in SERVICE_PRINCIPAL:
        return "service_principal"
    if value in ENTRA:
        return "entra"
    if value in ENTRA_INTERACTIVE:
        return "entra_interactive"
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
    if kind in {"managed_identity", "entra", "entra_interactive"}:
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
    if kind == "entra_interactive":
        return interactive_chain(cid)
    if kind == "entra":
        from azure.identity import DefaultAzureCredential

        kwargs: dict[str, Any] = {"exclude_interactive_browser_credential": True}
        if cid:
            kwargs["managed_identity_client_id"] = cid
        return DefaultAzureCredential(**kwargs)
    return (key or "").strip()


def interactive_chain(client_id: str = "") -> Any:
    """Managed identity, then the Azure CLI, then a browser prompt (2026-11-19, user). The silent
    ones first, so a VM never prompts. The browser sign-in is remembered (token cache + record)
    so a laptop is asked once, for review-ui and core-pipeline alike."""
    from pathlib import Path

    from azure.identity import (
        AuthenticationRecord,
        AzureCliCredential,
        ChainedTokenCredential,
        InteractiveBrowserCredential,
        ManagedIdentityCredential,
        TokenCachePersistenceOptions,
    )

    record_path = Path.home() / AUTH_RECORD
    record = None
    try:
        if record_path.is_file():
            record = AuthenticationRecord.deserialize(record_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a broken record only means one more prompt
        record = None
    cache = TokenCachePersistenceOptions(name=TOKEN_CACHE, allow_unencrypted_storage=True)
    mi = ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
    return ChainedTokenCredential(
        mi,
        AzureCliCredential(process_timeout=30),
        InteractiveBrowserCredential(cache_persistence_options=cache, authentication_record=record),
    )
