"""Cosmos sign-in: the account key, the host's managed identity (2026-11-11), an Entra ID app
registration or DefaultAzureCredential (2026-11-14, user)."""
import sys
import types

from common.db import cosmos_store


def _fake_identity(monkeypatch):
    made = []

    class ManagedIdentityCredential:
        def __init__(self, client_id=None):
            made.append(client_id)

    class ClientSecretCredential:
        def __init__(self, tenant_id, client_id, client_secret):
            made.append(("sp", tenant_id, client_id, bool(client_secret)))

    class DefaultAzureCredential:
        def __init__(self, **kwargs):
            made.append(("default", kwargs.get("managed_identity_client_id")))

    module = types.ModuleType("azure.identity")
    module.ManagedIdentityCredential = ManagedIdentityCredential
    module.ClientSecretCredential = ClientSecretCredential
    module.DefaultAzureCredential = DefaultAzureCredential
    monkeypatch.setitem(sys.modules, "azure.identity", module)
    return made


def test_the_key_by_default(monkeypatch):
    monkeypatch.delenv("COSMOS_AUTH", raising=False)
    monkeypatch.setenv("COSMOS_KEY", "k")
    assert cosmos_store.cosmos_auth() == "key" and cosmos_store.cosmos_credential() == "k"


def test_a_managed_identity_needs_no_key(monkeypatch):
    made = _fake_identity(monkeypatch)
    monkeypatch.setenv("COSMOS_AUTH", "managed_identity")
    monkeypatch.delenv("COSMOS_KEY", raising=False)
    monkeypatch.setenv("COSMOS_CLIENT_ID", "")
    monkeypatch.setenv("AZURE_CLIENT_ID", "abc")
    cosmos_store.cosmos_credential()
    assert made == ["abc"]  # user-assigned: AZURE_CLIENT_ID when COSMOS_CLIENT_ID is blank
    monkeypatch.setenv("COSMOS_AUTH", "managed_entity")  # the spelling the client used
    monkeypatch.setenv("AZURE_CLIENT_ID", "")
    cosmos_store.cosmos_credential()
    assert made[-1] is None  # system-assigned


def test_review_ui_takes_the_same_settings(monkeypatch):
    from app.store.cosmos import credential

    assert credential.configured("https://x", "", "managed_identity")
    assert not credential.configured("https://x", "", "key")
    made = _fake_identity(monkeypatch)
    credential.credential("", "managed_identity", "cid")
    assert made == ["cid"]


def test_entra_id_app_registration_and_default(monkeypatch):
    import pytest

    made = _fake_identity(monkeypatch)
    monkeypatch.delenv("COSMOS_KEY", raising=False)
    for name in ("COSMOS_TENANT_ID", "AZURE_TENANT_ID", "COSMOS_CLIENT_SECRET", "AZURE_CLIENT_SECRET", "AZURE_CLIENT_ID"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("COSMOS_AUTH", "service_principal")
    monkeypatch.setenv("COSMOS_CLIENT_ID", "app")
    with pytest.raises(RuntimeError, match="COSMOS_TENANT_ID"):
        cosmos_store.cosmos_credential()
    monkeypatch.setenv("AZURE_TENANT_ID", "t")  # the AZURE_* setting fills a blank COSMOS_* one
    monkeypatch.setenv("COSMOS_CLIENT_SECRET", "s")
    cosmos_store.cosmos_credential()
    assert made[-1] == ("sp", "t", "app", True)
    monkeypatch.setenv("COSMOS_AUTH", "entra")
    assert cosmos_store.cosmos_auth() == "entra"
    cosmos_store.cosmos_credential()
    assert made[-1] == ("default", "app")


def test_review_ui_entra_id(monkeypatch):
    from app.store.cosmos import credential

    assert credential.configured("https://x", "", "entra")
    assert not credential.configured("https://x", "", "service_principal", "t", "app", "")
    assert credential.configured("https://x", "", "service_principal", "t", "app", "s")
    made = _fake_identity(monkeypatch)
    credential.credential("", "service_principal", "app", "t", "s")
    credential.credential("", "entra", "")
    assert made == [("sp", "t", "app", True), ("default", None)]
