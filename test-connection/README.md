# Managed-identity connection test

Self-contained. Nothing here imports the pipeline, writes a file, or touches a
database — copy the folder onto the Azure VM, fill in two blanks in `.env`, run
two scripts.

```
.env                 the only thing you edit
requirements.txt     azure-identity, azure-storage-blob, openai
test_blob_mi.py      can the identity list blobs?
test_openai_mi.py    can the identity call the Azure OpenAI deployment?
```

**No account keys, no connection strings, no SAS tokens, no client secrets, no
passwords.** The only credential is the User-Assigned Managed Identity attached
to the VM.

| | |
|---|---|
| Identity | `az-mi-dv-e2-ai-poc` (User-Assigned Managed Identity) |
| **Client ID** | `ad1f4c35-f64d-4cb8-a7bc-3f97d5f9f767` — what the code passes |
| **Principal / Object ID** | `8847dea2-3dd2-464b-9ae1-f0505c274f4e` — what you pass to `az role assignment`. **Not a credential.** It never appears in a request and no script reads it |

Two things must both be true, and they are assigned in different places:

1. the identity is **attached to the VM** (`az vm identity assign`), and
2. the identity holds a **data-plane role on the resource** —
   `Storage Blob Data Reader` (or `…Contributor` for write/delete) on the
   storage account, `Cognitive Services OpenAI User` on the OpenAI resource.

Subscription Owner or Contributor grants **neither**. A control-plane role does
not read a blob.

This must run **on the Azure VM**. The identity endpoint is IMDS at
`169.254.169.254`, which does not exist on a laptop.

---

## 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Or, without the file:

```bash
pip install azure-identity azure-storage-blob
pip install openai            # only for the Azure OpenAI test
```

## 2. Fill in `.env`

```ini
AZURE_CLIENT_ID=ad1f4c35-f64d-4cb8-a7bc-3f97d5f9f767   # already set
AZURE_STORAGE_ACCOUNT=<account name, not the URL>
AZURE_STORAGE_CONTAINER=<container>                    # optional
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
AZURE_OPENAI_DEPLOYMENT=<deployment name, not the model>
```

Real environment variables override the file, so a one-off run is just:

```bash
AZURE_STORAGE_ACCOUNT=otheracct python3 test_blob_mi.py
```

## 3. Run

```bash
python3 test_blob_mi.py        # exits 0 on success, 1 naming what to fix
python3 test_openai_mi.py      # one billed chat completion
```

Expected:

```
=== Azure Blob Managed Identity Test ===

Managed Identity Client ID:
ad1f4c35-f64d-4cb8-a7bc-3f97d5f9f767

Storage Account:
<account>

Container:
<container>

[1/3] Authenticating using User-Assigned Managed Identity...
SUCCESS
      token appid : ad1f4c35-f64d-4cb8-a7bc-3f97d5f9f767
      token oid   : 8847dea2-3dd2-464b-9ae1-f0505c274f4e   (the principal/object ID)
      audience    : https://storage.azure.com

[2/3] Connecting to Blob Storage...
      https://<account>.blob.core.windows.net
SUCCESS

[3/3] Listing blobs in '<container>'...
SUCCESS
      first 10 blob(s):
        ...

Blob access verified successfully.
```

The token's `appid` and `oid` are printed so you can see **which** identity
Azure actually handed back — on a VM with several identities attached, that is
the difference between a real pass and a coincidence.

An empty container still passes: an unauthorized list raises 403 rather than
returning zero rows.

---

## Before the Python: the same checks with the Azure CLI

Run these in order. Each one fails for exactly one reason, so the first failure
tells you which of the five states you are in.

```bash
CLIENT_ID=ad1f4c35-f64d-4cb8-a7bc-3f97d5f9f767
PRINCIPAL_ID=8847dea2-3dd2-464b-9ae1-f0505c274f4e
ACCOUNT=<storage account>
CONTAINER=<container>
```

**A — is the identity attached to this VM?**

```bash
az vm identity show -g <rg> -n <vm> -o json
# userAssignedIdentities must contain .../az-mi-dv-e2-ai-poc

# attach it if not:
az vm identity assign -g <rg> -n <vm> \
  --identities /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.ManagedIdentity/userAssignedIdentities/az-mi-dv-e2-ai-poc
```

**B — can the VM get a token for it?** (IMDS directly, no CLI, no Python)

```bash
curl -s -H Metadata:true \
  "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fstorage.azure.com%2F&client_id=$CLIENT_ID" \
  | head -c 400
```

`"access_token"` in the response = authentication works.
`identity_not_found` = the identity is not attached to this VM.
No response at all = not an Azure VM, or IMDS is blocked.

**C — log the CLI in as the identity:**

```bash
az login --identity --username "$CLIENT_ID"
az account show -o table
```

**D — what roles does the principal actually hold?**

```bash
az role assignment list --assignee "$PRINCIPAL_ID" --all \
  --query "[].{role:roleDefinitionName, scope:scope}" -o table
```

**E — list containers, then blobs, as the identity** (`--auth-mode login` is
what forces Entra; without it the CLI reaches for an account key):

```bash
az storage container list --account-name "$ACCOUNT" --auth-mode login -o table
az storage blob list -c "$CONTAINER" --account-name "$ACCOUNT" --auth-mode login --num-results 10 -o table
```

**F — does the account name even resolve?**

```bash
nslookup "$ACCOUNT.blob.core.windows.net"
az storage account list --query "[].name" -o tsv
```

### Reading the result

| # | State | What you see | Fix |
|---|---|---|---|
| 1 | Identity **not attached** to the VM | **A** omits the identity; **B** returns `identity_not_found` or nothing | `az vm identity assign …` (above), then reboot is not needed but the token cache is per-process |
| 2 | Attached, **authentication fails** | **B** returns an error other than `identity_not_found`; **C** fails | Wrong client ID, IMDS blocked by a firewall/proxy, or VM clock skew. Confirm the client ID against `az identity show -g <rg> -n az-mi-dv-e2-ai-poc --query clientId` |
| 3 | Authenticated, **blob RBAC missing** | **B**/**C** fine; **E** returns `AuthorizationPermissionMismatch` / 403 | Assign a data role (below). Wait a few minutes — assignments are not instant |
| 4 | RBAC fine, **wrong name** | **E** returns `ContainerNotFound`, or **F** does not resolve | Check the account and container names; container listing in **E** shows the real ones |
| 5 | **Everything works** | **E** lists blobs | Run the Python tests |

State 3 is the usual one, and this is the fix:

```bash
az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Reader" \
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.Storage/storageAccounts/$ACCOUNT
```

`Storage Blob Data Contributor` instead if the pipeline must write or delete.
Use `--assignee-object-id` with `--assignee-principal-type ServicePrincipal`,
not `--assignee` — the latter makes the CLI do a Graph lookup the identity may
not be allowed to perform.

For Azure OpenAI the role is different and so is the scope — the resource, not
the storage account:

```bash
az role assignment create \
  --assignee-object-id "$PRINCIPAL_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" \
  --scope /subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<resource>

az cognitiveservices account deployment list -g <rg> -n <resource> -o table
```

Blob access passing tells you nothing about OpenAI access, or the reverse: two
resources, two role assignments.

---

## Errors, and what each one means

| Azure error | Means |
|---|---|
| `ClientAuthenticationError` on step 1 | The identity is not attached, or not this client ID. State 1 |
| 403 `AuthorizationPermissionMismatch` | Authenticated, no data role. State 3 — a control-plane role does not count |
| 403 on **containers** but blobs list fine | Correct: the role is scoped to one container, not the account. Not a failure |
| 404 `ContainerNotFound` | Auth and RBAC are fine, the container name is wrong. State 4 |
| `ServiceRequestError` / DNS failure | The account name is wrong, or a private endpoint/firewall blocks the VM. State 4 |
| `AuthenticationFailed` / `InvalidAuthenticationInfo` | Storage rejected the token — check VM clock, and that the account allows Entra authorization |
| 401 `… lacks the required data action` (OpenAI) | Token valid, no role on the OpenAI resource |
| 404 from OpenAI | No deployment by that name — it is the *deployment* name, not the model |
