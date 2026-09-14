#!/usr/bin/env python3
"""Ad-hoc: does our Azure OpenAI endpoint answer?

Fill in the values below and run it. Nothing else is read — no .env, no
environment variables, no pipeline imports.

    pip install -r requirements.txt
    python check_azure_openai.py

Prints the answer and exits 0, or names what to fix and exits 1. One billed
call. The pytest version, wired to the real DOS prompt and skipped when
credentials are absent, is tests/test_azure_openai.py.

This uses Azure's **v1 API surface**: the stock `OpenAI` client pointed at
`<resource>/openai/v1`, not `AzureOpenAI`. That surface takes no
`api_version` — the path carries the version — which is why there is no such
setting here any more.

Two ways to authenticate, set by AUTH below:

    "key"     paste a resource key into API_KEY.
    "entra"   no key at all — the VM's managed identity, or whatever
              `az login` left behind. Needs `pip install azure-identity`.
    "auto"    key if API_KEY is filled in, otherwise entra.

On an Azure VM `entra` is usually the one that works, and on a resource with
local auth disabled it is the only one. It needs the identity to hold the
**Cognitive Services OpenAI User** role on the resource — being in the
subscription is not enough, and a missing role assignment returns a 401 that
looks exactly like a bad key.
"""
from __future__ import annotations

import sys

# --- fill these in ----------------------------------------------------------
ENDPOINT = "https://openai-algodel.services.ai.azure.com/openai/v1"   # must end /openai/v1
AUTH = "auto"                  # "key" | "entra" | "auto"
API_KEY = ""                   # key auth only; paste the resource key here
DEPLOYMENT = "gpt-4o"          # the DEPLOYMENT name on the resource, not the model
# Leave API_KEY blank in git. Paste the key locally to run, then clear it again.
# ----------------------------------------------------------------------------

PROMPT = "What is the capital of India? Answer in one word."

# Data-plane scope for Azure OpenAI. NOT https://management.azure.com/.default,
# which gets a valid token that cannot call a deployment.
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def resolve_auth() -> str:
    """Which mode to actually use, or exit naming what is missing."""
    mode = AUTH.strip().casefold()
    if mode not in {"key", "entra", "auto"}:
        fail(f'AUTH must be "key", "entra" or "auto" — got {AUTH!r}')
    if mode == "auto":
        mode = "key" if API_KEY.strip() else "entra"
    if mode == "key" and not API_KEY.strip():
        fail('AUTH is "key" but API_KEY is blank at the top of this file')
    return mode


def entra_token() -> str:
    """A bearer token for this resource, or exit explaining which step failed."""
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError:
        fail(
            "azure-identity not installed — pip install azure-identity, "
            "or set AUTH to 'key' and paste a key"
        )

    try:
        # Tries managed identity, then env vars, then the `az` CLI login. On a
        # VM with no identity assigned this is where it stops.
        return DefaultAzureCredential().get_token(TOKEN_SCOPE).token
    except Exception as exc:
        fail(
            f"could not get an Entra token ({type(exc).__name__}: {exc}). "
            "On a VM, check a managed identity is assigned; on a laptop, run "
            "`az login`."
        )


def main() -> None:
    mode = resolve_auth()

    print(f"endpoint   : {ENDPOINT or '(blank)'}")
    print(f"deployment : {DEPLOYMENT or '(blank)'}")
    print(f"auth       : {mode}" + (f" (AUTH={AUTH})" if AUTH.strip().casefold() == "auto" else ""))
    if mode == "key":
        print(f"api_key    : set ({len(API_KEY.strip())} chars)")
    print()

    blank = [
        name
        for name, value in (("ENDPOINT", ENDPOINT), ("DEPLOYMENT", DEPLOYMENT))
        if not value.strip()
    ]
    if blank:
        fail(f"{' and '.join(blank)} still blank at the top of this file")

    if not ENDPOINT.rstrip("/").endswith("/openai/v1"):
        fail(f"ENDPOINT must end with /openai/v1 — got {ENDPOINT!r}")

    try:
        from openai import OpenAI
    except ImportError:
        fail("openai not installed — pip install -r requirements.txt")

    # The v1 surface takes the bearer token in the same place as the key: the
    # client sends `Authorization: Bearer <api_key>` either way.
    secret = API_KEY.strip() if mode == "key" else entra_token()
    if mode == "entra":
        print("token      : acquired")
    client = OpenAI(api_key=secret, base_url=ENDPOINT.strip().rstrip("/"))

    print(f"asking     : {PROMPT}")
    try:
        response = client.responses.create(
            model=DEPLOYMENT.strip(),
            input=PROMPT,
            timeout=30,
        )
    except Exception as exc:
        # 401 = bad key, or — under entra — a token whose identity lacks the
        #       Cognitive Services OpenAI User role on this resource.
        # 404 = no deployment by that name — check the resource's deployment list.
        # APIConnectionError = the endpoint host is wrong.
        fail(f"{type(exc).__name__}: {exc}")

    answer = (response.output_text or "").strip()
    print(f"answer     : {answer or '(empty)'}")
    print()
    if not answer:
        fail("the deployment answered with empty content")
    print(f"OK: Azure OpenAI is reachable via {mode} auth and the deployment responds.")


if __name__ == "__main__":
    main()
