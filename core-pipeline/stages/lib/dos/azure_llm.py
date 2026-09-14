"""Azure OpenAI client for imaging-pipeline LLM calls.

Two ways to authenticate, because the two places this runs have different
credentials available:

* **API key** — a key pasted into `.env`. What a laptop normally has.
* **Entra ID** — no key at all: a managed identity on the VM, or whatever
  `az login` left behind. What an Azure VM normally has, and the only option
  on a resource with `disableLocalAuth` set.

`AZURE_OPENAI_AUTH` picks between them and defaults to `auto`, which means
"key if there is one, otherwise Entra". Nothing needs to change on a machine
that was already using a key.

Entra needs `azure-identity` installed (`pip install -r requirements.txt`
brings it) and an identity the resource has granted **Cognitive Services
OpenAI User** — membership in the subscription is not enough, and a missing
role assignment is a 401 that reads exactly like a bad key.
"""

from __future__ import annotations

import os
from typing import Any

# The resource-level scope for Azure OpenAI data-plane calls. Not the
# management-plane scope (`https://management.azure.com/.default`), which
# authenticates but cannot call a deployment.
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"

_KEY = "key"
_ENTRA = "entra"
_AUTO = "auto"


def auth_mode() -> str:
    """Which of `key` / `entra` / `auto` was asked for. Unknown values are `auto`."""
    raw = (os.getenv("AZURE_OPENAI_AUTH") or _AUTO).strip().casefold()
    return raw if raw in {_KEY, _ENTRA, _AUTO} else _AUTO


def entra_available() -> bool:
    """Is `azure-identity` importable? Not whether a credential will work."""
    from importlib.util import find_spec

    return find_spec("azure.identity") is not None


def resolved_auth(api_key: str | None = None) -> str | None:
    """The mode that will actually be used, or None when none can be.

    Kept separate from client construction so the config gate and the health
    endpoint can ask the question without building a client or fetching a
    token.
    """
    if api_key is None:
        api_key = (os.getenv("AZURE_OPENAI_API_KEY") or "").strip()
    mode = auth_mode()

    if mode == _KEY:
        return _KEY if api_key else None
    if mode == _ENTRA:
        return _ENTRA if entra_available() else None
    if api_key:
        return _KEY
    return _ENTRA if entra_available() else None


def _token_provider() -> Any:
    """A callable returning a fresh bearer token; the SDK re-calls it on expiry.

    `DefaultAzureCredential` tries managed identity, environment variables and
    the `az` CLI login in turn, so the same code works on a VM and on a
    developer machine that has run `az login`.
    """
    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    return get_bearer_token_provider(DefaultAzureCredential(), TOKEN_SCOPE)


def get_azure_openai_client() -> Any | None:
    """
    Build AzureOpenAI client from env:

      AZURE_OPENAI_ENDPOINT      (required) e.g. https://YOUR.openai.azure.com/
      AZURE_OPENAI_AUTH          key | entra | auto   (default auto)
      AZURE_OPENAI_API_KEY       required for key auth; ignored by entra
      AZURE_OPENAI_API_VERSION   (default 2024-08-01-preview)
      AZURE_OPENAI_DEPLOYMENT    (deployment name used as model=)

    Returns None when the endpoint or the credentials are missing, so the DOS
    stage degrades to regex-only rather than raising mid-chart. Constructing
    the client fetches no token — the first call does.
    """
    api_key = (os.getenv("AZURE_OPENAI_API_KEY") or "").strip()
    endpoint = (os.getenv("AZURE_OPENAI_ENDPOINT") or "").strip()
    api_version = (
        os.getenv("AZURE_OPENAI_API_VERSION") or "2024-08-01-preview"
    ).strip()

    if not endpoint:
        return None

    mode = resolved_auth(api_key)
    if mode is None:
        return None

    try:
        from openai import AzureOpenAI
    except ImportError as exc:
        raise SystemExit(
            "Install openai: pip install 'openai>=1.40'"
        ) from exc

    if mode == _KEY:
        return AzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint.rstrip("/"),
            api_version=api_version,
        )

    return AzureOpenAI(
        azure_ad_token_provider=_token_provider(),
        azure_endpoint=endpoint.rstrip("/"),
        api_version=api_version,
    )


def azure_deployment() -> str:
    return (os.getenv("AZURE_OPENAI_DEPLOYMENT") or "gpt-4o-mini").strip()
