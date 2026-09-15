#!/usr/bin/env python3
"""Does this VM's User-Assigned Managed Identity have blob access?

Standalone. Imports nothing from the pipeline, writes nothing, reads no .env.
No account keys, no connection strings, no SAS, no client secrets — the only
credential is the identity attached to the VM.

    pip install -r requirements.txt

    export AZURE_STORAGE_ACCOUNT=<storage account name, not the URL>
    export AZURE_STORAGE_CONTAINER=<container>        # optional
    export AZURE_CLIENT_ID=ad1f4c35-f64d-4cb8-a7bc-3f97d5f9f767   # has a default
    python3 test_blob_mi.py

Exits 0 when the identity can list, 1 naming the one thing to fix.

Note on the IDs: the CLIENT ID is what the code passes. The PRINCIPAL/OBJECT ID
(8847dea2-3dd2-464b-9ae1-f0505c274f4e) is what you hand to `az role assignment`
— it is not a credential and never appears in a request.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import sys

# The identity the client asked us to test. Overridable, but this is the one.
DEFAULT_CLIENT_ID = "ad1f4c35-f64d-4cb8-a7bc-3f97d5f9f767"

# Data-plane scope for Blob Storage. NOT https://management.azure.com/.default,
# which yields a perfectly valid token that cannot read a single blob.
TOKEN_SCOPE = "https://storage.azure.com/.default"

MAX_BLOBS = 10


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
    """The JWT's payload, for identifying *which* principal Azure handed back.

    Claims only — the token itself is never printed.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def describe(exc: Exception) -> str:
    """Azure's own error text, with the bits that identify the failure first."""
    code = getattr(exc, "error_code", None)
    status = getattr(exc, "status_code", None)
    head = type(exc).__name__
    if status:
        head += f" HTTP {status}"
    if code:
        head += f" {code}"
    return f"{head}\n\n{exc}"


def main() -> None:
    load_env()

    client_id = os.getenv("AZURE_CLIENT_ID", DEFAULT_CLIENT_ID).strip()
    account = os.getenv("AZURE_STORAGE_ACCOUNT", "").strip()
    container = os.getenv("AZURE_STORAGE_CONTAINER", "").strip()

    print("=== Azure Blob Managed Identity Test ===\n")
    print(f"Managed Identity Client ID:\n{client_id or '(blank)'}\n")
    print(f"Storage Account:\n{account or '(not set — export AZURE_STORAGE_ACCOUNT)'}\n")
    print(f"Container:\n{container or '(not set — will list containers instead)'}\n")

    if not client_id:
        fail("config", "AZURE_CLIENT_ID is blank.",
             f"export AZURE_CLIENT_ID={DEFAULT_CLIENT_ID}")
    if not account:
        fail("config", "AZURE_STORAGE_ACCOUNT is blank.",
             "export AZURE_STORAGE_ACCOUNT=<storage account name>\n"
             "The NAME only — 'mystorageacct', not the https:// URL.")

    try:
        from azure.core.exceptions import (
            ClientAuthenticationError,
            HttpResponseError,
            ResourceNotFoundError,
            ServiceRequestError,
        )
        from azure.identity import ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient
    except ImportError as exc:
        fail("imports", str(exc),
             "pip install azure-identity azure-storage-blob")

    # --- [1/3] authenticate -------------------------------------------------
    print("[1/3] Authenticating using User-Assigned Managed Identity...")
    credential = ManagedIdentityCredential(client_id=client_id)
    try:
        token = credential.get_token(TOKEN_SCOPE)
    except ClientAuthenticationError as exc:
        fail("[1/3] authentication", describe(exc),
             "Either no managed identity is attached to this VM, or the one\n"
             "attached is not the identity with this client ID.\n\n"
             "  az vm identity show -g <rg> -n <vm> -o json\n"
             "  az vm identity assign -g <rg> -n <vm> \\\n"
             "      --identities /subscriptions/<sub>/resourceGroups/<rg>"
             "/providers/Microsoft.ManagedIdentity/userAssignedIdentities/"
             "az-mi-dv-e2-ai-poc\n\n"
             "This must run ON the Azure VM — the identity endpoint (IMDS,\n"
             "169.254.169.254) does not exist on a laptop.")
    except Exception as exc:
        fail("[1/3] authentication", describe(exc))

    print("SUCCESS")
    claims = token_claims(token.token)
    got_appid = claims.get("appid") or claims.get("azp") or ""
    if got_appid:
        print(f"      token appid : {got_appid}")
        if got_appid.casefold() != client_id.casefold():
            print("      WARNING: Azure returned a token for a DIFFERENT identity "
                  "than the one requested.")
    if claims.get("oid"):
        print(f"      token oid   : {claims['oid']}   (the principal/object ID)")
    print(f"      audience    : {claims.get('aud', '(unknown)')}")
    print()

    # --- [2/3] connect ------------------------------------------------------
    account_url = f"https://{account}.blob.core.windows.net"
    print(f"[2/3] Connecting to Blob Storage...\n      {account_url}")
    service = BlobServiceClient(account_url=account_url, credential=credential)

    # Constructing the client hits nothing, so make one real call. A 403 here
    # is not a failure: Storage Blob Data Reader scoped to a single container
    # cannot list the account's containers, which is a correct configuration.
    containers: list[str] = []
    account_listing_denied = False
    try:
        for i, c in enumerate(service.list_containers(results_per_page=50)):
            containers.append(c.name)
            if i >= 199:
                break
    except ServiceRequestError as exc:
        fail("[2/3] connection", describe(exc),
             f"Could not reach {account_url}.\n"
             f"Usually the storage account name is wrong (DNS does not resolve),\n"
             "or the VM has no route/DNS to *.blob.core.windows.net (private\n"
             "endpoint or firewall).\n\n"
             f"  nslookup {account}.blob.core.windows.net\n"
             "  az storage account list --query \"[].name\" -o tsv")
    except ClientAuthenticationError as exc:
        fail("[2/3] connection", describe(exc),
             "The token was rejected by Storage. Check the scope is\n"
             "https://storage.azure.com/.default and that the storage account\n"
             "has not disabled Entra (OAuth) access.")
    except HttpResponseError as exc:
        code = (getattr(exc, "error_code", "") or "").casefold()
        if exc.status_code == 403 or "authorizationpermissionmismatch" in code \
                or "authorizationfailure" in code:
            account_listing_denied = True
        elif exc.status_code == 404:
            fail("[2/3] connection", describe(exc),
                 f"Storage account '{account}' answered 404 — check the name.")
        else:
            fail("[2/3] connection", describe(exc))

    print("SUCCESS")
    if account_listing_denied:
        print("      Account-level container listing is denied (403). The endpoint\n"
              "      is reachable and the token is accepted — the role is simply\n"
              "      scoped to a container, not the account. Fine if so.")
    else:
        shown = ", ".join(containers[:20]) or "(none — the account has no containers)"
        print(f"      containers visible: {len(containers)}")
        print(f"      {shown}")
    print()

    # --- [3/3] list ---------------------------------------------------------
    if not container:
        print("[3/3] Listing blobs...")
        if account_listing_denied:
            fail("[3/3] listing",
                 "No container given, and container listing is denied.",
                 "Set AZURE_STORAGE_CONTAINER=<container> and run again, or grant\n"
                 "the identity Storage Blob Data Reader at ACCOUNT scope.")
        print("SKIPPED — no AZURE_STORAGE_CONTAINER set; container listing above\n"
              "          already proves authentication and RBAC work.\n")
        print("Blob access verified successfully (account-level listing).")
        return

    print(f"[3/3] Listing blobs in '{container}'...")
    names: list[str] = []
    try:
        client = service.get_container_client(container)
        for i, blob in enumerate(client.list_blobs()):
            names.append(f"{blob.name}  ({blob.size} bytes)")
            if i + 1 >= MAX_BLOBS:
                break
    except ResourceNotFoundError as exc:
        fail("[3/3] listing", describe(exc),
             f"Container '{container}' does not exist on account '{account}'.\n"
             "Authentication and authorization are FINE — only the name is wrong.\n\n"
             f"  az storage container list --account-name {account} "
             "--auth-mode login -o table")
    except HttpResponseError as exc:
        code = (getattr(exc, "error_code", "") or "").casefold()
        if exc.status_code == 403 or "authorizationpermissionmismatch" in code:
            fail("[3/3] listing", describe(exc),
                 "The identity authenticated but has NO blob data role on this\n"
                 "container or account. Subscription Owner/Contributor does not\n"
                 "grant data-plane access — the role must be one of the 'Storage\n"
                 "Blob Data ...' roles.\n\n"
                 "  az role assignment create \\\n"
                 "    --assignee-object-id 8847dea2-3dd2-464b-9ae1-f0505c274f4e \\\n"
                 "    --assignee-principal-type ServicePrincipal \\\n"
                 '    --role "Storage Blob Data Reader" \\\n'
                 "    --scope /subscriptions/<sub>/resourceGroups/<rg>/providers"
                 f"/Microsoft.Storage/storageAccounts/{account}\n\n"
                 "Use 'Storage Blob Data Contributor' if the pipeline must also\n"
                 "write. Role assignments can take a few minutes to take effect.")
        if "authenticationfailed" in code or "invalidauthenticationinfo" in code:
            fail("[3/3] listing", describe(exc),
                 "Storage rejected the token itself. Check the VM clock and that\n"
                 "the account allows Entra authorization.")
        fail("[3/3] listing", describe(exc))
    except Exception as exc:
        fail("[3/3] listing", describe(exc))

    print("SUCCESS")
    if names:
        print(f"      first {len(names)} blob(s):")
        for name in names:
            print(f"        {name}")
    else:
        print("      the container is EMPTY — listing was permitted, there is\n"
              "      simply nothing in it. That is a pass: an unauthorized list\n"
              "      raises 403 rather than returning zero rows.")
    print()
    print("Blob access verified successfully.")


if __name__ == "__main__":
    main()
