"""Cosmos sign-in (2026-11-11, user): the account key, or the host's managed identity."""
import sys
import types

from common.db import cosmos_store


def _fake_identity(monkeypatch):
    made = []

    class ManagedIdentityCredential:
        def __init__(self, client_id=None):
            made.append(client_id)

    module = types.ModuleType("azure.identity")
    module.ManagedIdentityCredential = ManagedIdentityCredential
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
