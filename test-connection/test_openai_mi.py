#!/usr/bin/env python3
"""Does this VM's User-Assigned Managed Identity have Azure OpenAI access?

Same identity, same rules as test_blob_mi.py: no keys, no secrets, nothing
imported from the pipeline. Azure OpenAI is a separate resource with a separate
RBAC role, so blob access passing tells you nothing about this and vice versa.

    pip install -r requirements.txt

    export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
    export AZURE_OPENAI_DEPLOYMENT=<deployment name, not the model name>
    export AZURE_CLIENT_ID=ad1f4c35-f64d-4cb8-a7bc-3f97d5f9f767   # has a default
    python3 test_openai_mi.py

One chat completion is billed. Exits 0 on success, 1 naming what to fix.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys

DEFAULT_CLIENT_ID = "ad1f4c35-f64d-4cb8-a7bc-3f97d5f9f767"

# Data-plane scope for Azure OpenAI — a different resource from Storage, so a
# different scope and a different role assignment.
TOKEN_SCOPE = "https://cognitiveservices.azure.com/.default"

PROMPT = "What is the capital of India? Answer in one word."


def load_env() -> None:
    """Read the .env sitting next to this script, if there is one.

    Deliberately not python-dotenv — this folder must run with nothing but
    azure-identity and azure-storage-blob installed. Real environment variables
    always win over the file, so you can override any line inline.
    """
    path = pathlib.Path(__file__).resolve().parent / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


def fail(step: str, message: str, fix: str = "") -> None:
    print("FAILED\n")
    print(f"{step}: {message}")
    if fix:
        print(f"\nWhat to fix:\n{fix}")
    sys.exit(1)


def token_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def v1_base_url(endpoint: str) -> str:
    """Azure's v1 API surface: <resource>/openai/v1, which the stock OpenAI
    client talks to directly. The path carries the version, so there is no
    api_version setting."""
    base = endpoint.strip().rstrip("/")
    if base.endswith("/openai/v1"):
        return base
    if base.endswith("/openai"):
        return base + "/v1"
    return base + "/openai/v1"


def main() -> None:
    load_env()

    client_id = os.getenv("AZURE_CLIENT_ID", DEFAULT_CLIENT_ID).strip()
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()

    print("=== Azure OpenAI Managed Identity Test ===\n")
    print(f"Managed Identity Client ID:\n{client_id or '(blank)'}\n")
    print(f"Endpoint:\n{endpoint or '(not set — export AZURE_OPENAI_ENDPOINT)'}\n")
    print(f"Deployment:\n{deployment or '(not set — export AZURE_OPENAI_DEPLOYMENT)'}\n")

    if not client_id:
        fail("config", "AZURE_CLIENT_ID is blank.",
             f"export AZURE_CLIENT_ID={DEFAULT_CLIENT_ID}")
    missing = [n for n, v in (("AZURE_OPENAI_ENDPOINT", endpoint),
                              ("AZURE_OPENAI_DEPLOYMENT", deployment)) if not v]
    if missing:
        fail("config", f"{' and '.join(missing)} not set.",
             "export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com\n"
             "export AZURE_OPENAI_DEPLOYMENT=<the deployment name on that resource>")

    try:
        from azure.core.exceptions import ClientAuthenticationError
        from azure.identity import ManagedIdentityCredential
    except ImportError as exc:
        fail("imports", str(exc), "pip install azure-identity")
    try:
        from openai import OpenAI
    except ImportError as exc:
        fail("imports", str(exc), "pip install openai")

    # --- [1/3] authenticate -------------------------------------------------
    print("[1/3] Authenticating using User-Assigned Managed Identity...")
    credential = ManagedIdentityCredential(client_id=client_id)
    try:
        token = credential.get_token(TOKEN_SCOPE)
    except ClientAuthenticationError as exc:
        fail("[1/3] authentication", f"{type(exc).__name__}\n\n{exc}",
             "No managed identity attached to this VM, or not the one with this\n"
             "client ID. Check with:  az vm identity show -g <rg> -n <vm>\n"
             "This must run ON the Azure VM — IMDS does not exist on a laptop.")
    except Exception as exc:
        fail("[1/3] authentication", f"{type(exc).__name__}\n\n{exc}")

    print("SUCCESS")
    claims = token_claims(token.token)
    if claims.get("appid") or claims.get("azp"):
        print(f"      token appid : {claims.get('appid') or claims.get('azp')}")
    if claims.get("oid"):
        print(f"      token oid   : {claims['oid']}   (the principal/object ID)")
    print(f"      audience    : {claims.get('aud', '(unknown)')}")
    print()

    # --- [2/3] connect ------------------------------------------------------
    base_url = v1_base_url(endpoint)
    print(f"[2/3] Connecting to Azure OpenAI...\n      {base_url}")
    # The v1 surface takes the bearer token in the same place a key would go:
    # the client sends `Authorization: Bearer <api_key>` either way.
    client = OpenAI(api_key=token.token, base_url=base_url)
    print("SUCCESS")
    print()

    # --- [3/3] call ---------------------------------------------------------
    print(f"[3/3] Calling deployment '{deployment}' (chat.completions)...")
    try:
        # chat.completions, NOT responses: Azure authorises each API surface
        # separately, so a check against responses can 401 on an identity that
        # runs the real pipeline fine.
        response = client.chat.completions.create(
            model=deployment,
            messages=[{"role": "user", "content": PROMPT}],
            temperature=0.0,
            max_tokens=16,
            timeout=30,
        )
    except Exception as exc:
        text = str(exc)
        status = getattr(exc, "status_code", None)
        hint = ""
        if status == 401 and "data action" in text:
            hint = ("The token is valid but the identity holds no role on this\n"
                    "resource. Assign Cognitive Services OpenAI User:\n\n"
                    "  az role assignment create \\\n"
                    "    --assignee-object-id 8847dea2-3dd2-464b-9ae1-f0505c274f4e \\\n"
                    "    --assignee-principal-type ServicePrincipal \\\n"
                    '    --role "Cognitive Services OpenAI User" \\\n'
                    "    --scope /subscriptions/b8049482-3053-448d-a59e-0a67b3238082/resourceGroups/<rg>/providers"
                    "/Microsoft.CognitiveServices/accounts/<resource>")
        elif status == 401 or status == 403:
            hint = ("Rejected. Either the role is missing (assign 'Cognitive\n"
                    "Services OpenAI User' on the RESOURCE) or the resource has\n"
                    "disabled Entra auth.")
        elif status == 404:
            hint = (f"No deployment named '{deployment}' on this resource — this is\n"
                    "the DEPLOYMENT name, not the model name.\n\n"
                    "  az cognitiveservices account deployment list \\\n"
                    "    -g <rg> -n <resource> -o table")
        elif "APIConnectionError" in type(exc).__name__:
            hint = (f"Could not reach {base_url} — check the endpoint host and\n"
                    "that the VM has network access to it.")
        fail("[3/3] call", f"{type(exc).__name__}"
             + (f" HTTP {status}" if status else "") + f"\n\n{text}", hint)

    answer = ((response.choices[0].message.content if response.choices else "") or "").strip()
    print("SUCCESS")
    print(f"      prompt : {PROMPT}")
    print(f"      answer : {answer or '(empty)'}")
    print()
    if not answer:
        fail("[3/3] call", "the deployment answered with empty content.")
    print("Azure OpenAI access verified successfully.")


if __name__ == "__main__":
    main()
