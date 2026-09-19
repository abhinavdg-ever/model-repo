"""Azure Blob helpers for chart page download and manifest sweep."""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any, BinaryIO, Optional, Union

from config import (
    AZURE_BLOB_CONNECTION_POOL_SIZE,
    AZURE_CLIENT_ID,
    AZURE_PRINCIPAL_ID,
    AZURE_STORAGE_ACCOUNT_KEY,
    AZURE_STORAGE_ACCOUNT_NAME,
    AZURE_STORAGE_AUTH,
    AZURE_STORAGE_CONNECTION_STRING,
    AZURE_STORAGE_CONTAINER,
    IMAGE_SUFFIXES,
)

logger = logging.getLogger(__name__)
_FILENAME_PARTS = re.compile(r"(\d+)")

# IMDS scope used to warm a managed-identity / Entra token before workers start.
_BLOB_TOKEN_SCOPE = "https://storage.azure.com/.default"


def filename_sort_key(name: str) -> tuple:
    text = str(name or "").casefold()
    parts: list = []
    for part in _FILENAME_PARTS.split(text):
        if not part:
            continue
        if part.isdigit():
            parts.append((0, int(part)))
        else:
            parts.append((1, part))
    return tuple(parts)


def normalize_prefix(path: str) -> str:
    p = (path or "").strip().strip("/")
    return f"{p}/" if p else ""


def chart_name_from_blob_path(blob_path: str) -> str:
    """Last non-empty path segment is the chart folder name."""
    parts = [p for p in blob_path.strip("/").split("/") if p]
    if not parts:
        raise ValueError("blob_path is empty")
    return parts[-1]


ENTRA_MODES = {"entra", "aad", "azuread"}
# Opt-in, and deliberately NOT part of `entra`: this chain can open a browser
# and wait for a human. On a service with no one watching, a hung prompt is
# worse than a clean failure, so it never happens unless asked for by name.
ENTRA_INTERACTIVE_MODES = {"entra_interactive", "entra-interactive", "browser"}
# VM / App Service / AKS managed identity only — no CLI, no browser.
# Pass AZURE_CLIENT_ID for a user-assigned identity; leave blank for system-assigned.
MANAGED_IDENTITY_MODES = {
    "managed_identity",
    "managed-identity",
    "mi",
    "msi",
}

_INTERACTIVE_CREDENTIAL = None
_INTERACTIVE_LOCK = threading.Lock()
_MI_CREDENTIAL = None
_MI_LOCK = threading.Lock()
_BLOB_READY = False
_BLOB_READY_LOCK = threading.Lock()


def _mi_client_id() -> Optional[str]:
    """Client id for a user-assigned managed identity, or None for system-assigned."""
    return AZURE_CLIENT_ID or None


def _managed_identity_credential():
    """ManagedIdentityCredential, optionally scoped to AZURE_CLIENT_ID."""
    global _MI_CREDENTIAL
    if _MI_CREDENTIAL is not None:
        return _MI_CREDENTIAL

    with _MI_LOCK:
        if _MI_CREDENTIAL is not None:
            return _MI_CREDENTIAL

        from azure.identity import ManagedIdentityCredential

        client_id = _mi_client_id()
        if client_id:
            _MI_CREDENTIAL = ManagedIdentityCredential(client_id=client_id)
            logger.info(
                "Azure Storage: managed identity auth "
                "(user-assigned client_id=%s%s)",
                client_id,
                f", principal_id={AZURE_PRINCIPAL_ID}" if AZURE_PRINCIPAL_ID else "",
            )
        else:
            _MI_CREDENTIAL = ManagedIdentityCredential()
            logger.info(
                "Azure Storage: managed identity auth (system-assigned)"
            )
        return _MI_CREDENTIAL


def _interactive_credential():
    """Managed identity, then Azure CLI, then a browser prompt.

    `DefaultAzureCredential` deliberately excludes InteractiveBrowserCredential,
    which is why a developer machine with no managed identity and no `az` on
    PATH fails with a twenty-line list of things it tried. The V1 prototype
    (`azure_blob/azure_blob_storage.py`) chained the browser in explicitly and
    therefore worked on exactly those machines; this restores that, with two
    changes.

    **Order is reversed from V1.** V1 put the browser first, so it prompted even
    where a non-interactive credential existed. Here the silent options are
    tried first, so the Azure VM's managed identity answers and nobody ever sees
    a browser — the prompt is the last resort, not the first.

    **The token cache persists**, so the prompt happens once rather than per
    process, and parallel workers share one login. Same cache name V1 used.

    Each credential in the chain raises CredentialUnavailableError when it
    cannot help, which is what lets ChainedTokenCredential move on — chaining
    DefaultAzureCredential here would NOT work, because its own failure is a
    ClientAuthenticationError and the chain stops on that.
    """
    global _INTERACTIVE_CREDENTIAL
    if _INTERACTIVE_CREDENTIAL is not None:
        return _INTERACTIVE_CREDENTIAL

    with _INTERACTIVE_LOCK:
        if _INTERACTIVE_CREDENTIAL is not None:
            return _INTERACTIVE_CREDENTIAL

        from azure.identity import (
            AzureCliCredential,
            ChainedTokenCredential,
            InteractiveBrowserCredential,
            ManagedIdentityCredential,
            TokenCachePersistenceOptions,
        )

        cache = TokenCachePersistenceOptions(
            name="advantmed_blob_storage", allow_unencrypted_storage=True
        )
        client_id = _mi_client_id()
        mi = (
            ManagedIdentityCredential(client_id=client_id)
            if client_id
            else ManagedIdentityCredential()
        )
        _INTERACTIVE_CREDENTIAL = ChainedTokenCredential(
            mi,
            AzureCliCredential(process_timeout=30),
            InteractiveBrowserCredential(cache_persistence_options=cache),
        )
        logger.info(
            "Azure Storage: interactive Entra auth — managed identity%s, then "
            "az CLI, then a browser prompt if neither answers",
            f" (client_id={client_id})" if client_id else "",
        )
        return _INTERACTIVE_CREDENTIAL


def _blob_client_kwargs() -> dict:
    from azure_retry import azure_sdk_retry_kwargs

    return azure_sdk_retry_kwargs(
        connection_pool_maxsize=AZURE_BLOB_CONNECTION_POOL_SIZE
    )


def get_blob_service_client():
    from azure.storage.blob import BlobServiceClient

    retry = _blob_client_kwargs()

    if AZURE_STORAGE_CONNECTION_STRING:
        return BlobServiceClient.from_connection_string(
            AZURE_STORAGE_CONNECTION_STRING, **retry
        )

    account = AZURE_STORAGE_ACCOUNT_NAME
    if not account:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_NAME is required")

    account_url = f"https://{account}.blob.core.windows.net"
    auth = AZURE_STORAGE_AUTH
    if auth in MANAGED_IDENTITY_MODES:
        return BlobServiceClient(
            account_url, credential=_managed_identity_credential(), **retry
        )
    if auth in ENTRA_INTERACTIVE_MODES:
        return BlobServiceClient(
            account_url, credential=_interactive_credential(), **retry
        )
    if auth in ENTRA_MODES:
        from azure.identity import DefaultAzureCredential

        client_id = _mi_client_id()
        # When a user-assigned MI is configured, DefaultAzureCredential must
        # be told which one — otherwise it only tries the system-assigned.
        dac_kwargs: dict = {}
        if client_id:
            dac_kwargs["managed_identity_client_id"] = client_id
        return BlobServiceClient(
            account_url,
            credential=DefaultAzureCredential(**dac_kwargs),
            **retry,
        )
    if AZURE_STORAGE_ACCOUNT_KEY:
        return BlobServiceClient(
            account_url, credential=AZURE_STORAGE_ACCOUNT_KEY, **retry
        )
    raise RuntimeError(
        f"No Azure Storage credentials configured "
        f"(AZURE_STORAGE_AUTH={AZURE_STORAGE_AUTH!r})"
    )


def get_container_client(container: Optional[str] = None):
    name = (container or AZURE_STORAGE_CONTAINER).strip()
    if not name:
        raise RuntimeError("blob container name is required")
    return get_blob_service_client().get_container_client(name)


def ensure_blob_ready(container: Optional[str] = None) -> None:
    """Warm the credential and touch the container once before parallel work.

    Interactive Entra can open a browser; doing that from N workers at once
    races the prompt. Managed identity / DefaultAzureCredential also benefit:
    the first IMDS call is done here so chart workers inherit a cached token.

    Safe to call repeatedly — only the first call per process talks to Azure.
    """
    global _BLOB_READY
    if _BLOB_READY:
        return
    with _BLOB_READY_LOCK:
        if _BLOB_READY:
            return
        client = get_container_client(container)
        # Force a token acquisition for Entra modes before list/download.
        credential = getattr(client, "credential", None)
        if credential is not None and hasattr(credential, "get_token"):
            try:
                credential.get_token(_BLOB_TOKEN_SCOPE)
            except Exception:
                # Fall through to get_container_properties, which surfaces the
                # same failure with a clearer Storage error.
                logger.debug("blob token warm-up failed; probing container", exc_info=True)
        client.get_container_properties()
        _BLOB_READY = True
        logger.info(
            "Azure Storage ready: container=%s auth=%s",
            (container or AZURE_STORAGE_CONTAINER),
            AZURE_STORAGE_AUTH,
        )


def list_image_blobs(container: str, blob_path: str) -> list[str]:
    """Image blobs under a chart folder prefix, including sub-folders.

    Sub-folders are included because the local side searches them: dropping the
    `recursive` switch from `import_local_folder` made "look in sub-folders" the
    only local behaviour, and a chart folder that keeps its scans in `pages/` is
    a common shape. Blob intake kept an immediate-children-only rule, so the
    same chart imported from a local path and found nothing from a container —
    two sources that are supposed to converge, diverging on layout.

    The cost of recursing is that pointing this at a PARENT of several chart
    folders pulls all of their pages into one chart. That is true of the local
    side too. `/api/charts/batch` is the tool for a folder of charts; this one
    is for a folder that IS a chart.
    """
    from azure_retry import call_with_retry
    from config import AZURE_RETRY_ATTEMPTS, AZURE_RETRY_BASE_DELAY, AZURE_RETRY_MAX_DELAY

    prefix = normalize_prefix(blob_path)

    def _list() -> list[str]:
        client = get_container_client(container)
        names: list[str] = []
        nested = 0
        for blob in client.list_blobs(name_starts_with=prefix):
            filename = Path(blob.name).name
            if Path(filename).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            if filename.startswith("._"):  # macOS AppleDouble stubs, as on disk
                continue
            relative = (
                blob.name[len(prefix) :] if blob.name.startswith(prefix) else blob.name
            )
            if "/" in relative.strip("/"):
                nested += 1
            names.append(blob.name)
        if nested:
            # Worth saying: it explains a page count that does not match the
            # container's top level, and it is how a parent prefix goes wrong.
            logger.info(
                "%s/%s: %d of %d image(s) came from sub-folders",
                container, prefix, nested, len(names),
            )
        names.sort(key=lambda n: filename_sort_key(Path(n).name))
        return names

    return call_with_retry(
        _list,
        attempts=AZURE_RETRY_ATTEMPTS,
        base_delay=AZURE_RETRY_BASE_DELAY,
        max_delay=AZURE_RETRY_MAX_DELAY,
        label=f"blob.list:{container}/{prefix}",
    )


def download_blob_to_path(container: str, blob_name: str, dest: Path) -> Path:
    from azure_retry import call_with_retry
    from config import AZURE_RETRY_ATTEMPTS, AZURE_RETRY_BASE_DELAY, AZURE_RETRY_MAX_DELAY

    dest.parent.mkdir(parents=True, exist_ok=True)

    def _download() -> Path:
        client = get_container_client(container)
        data = client.download_blob(blob_name).readall()
        dest.write_bytes(data)
        return dest

    return call_with_retry(
        _download,
        attempts=AZURE_RETRY_ATTEMPTS,
        base_delay=AZURE_RETRY_BASE_DELAY,
        max_delay=AZURE_RETRY_MAX_DELAY,
        label=f"blob.download:{blob_name}",
    )


def list_blobs_with_suffixes(
    container: str, prefix: str, suffixes: tuple[str, ...]
) -> list[str]:
    from azure_retry import call_with_retry
    from config import AZURE_RETRY_ATTEMPTS, AZURE_RETRY_BASE_DELAY, AZURE_RETRY_MAX_DELAY

    pref = normalize_prefix(prefix)

    def _list() -> list[str]:
        client = get_container_client(container)
        out: list[str] = []
        for blob in client.list_blobs(name_starts_with=pref):
            name = blob.name.casefold()
            if any(name.endswith(s.casefold()) for s in suffixes):
                out.append(blob.name)
        return sorted(out)

    return call_with_retry(
        _list,
        attempts=AZURE_RETRY_ATTEMPTS,
        base_delay=AZURE_RETRY_BASE_DELAY,
        max_delay=AZURE_RETRY_MAX_DELAY,
        label=f"blob.list_suffix:{container}/{pref}",
    )


def list_chart_prefixes(container: str, prefix: str) -> list[str]:
    """Chart folders directly under `prefix` — those holding image blobs.

    One listing pass over the whole prefix, grouping by the segment after it,
    rather than a listing per candidate folder. Prefixes with no images (stray
    manifest-only or thumbnail folders) are left out, so the caller does not
    have to ingest something that would fail on "no pages".
    """
    from azure_retry import call_with_retry
    from config import AZURE_RETRY_ATTEMPTS, AZURE_RETRY_BASE_DELAY, AZURE_RETRY_MAX_DELAY

    pref = normalize_prefix(prefix)

    def _list() -> list[str]:
        client = get_container_client(container)
        charts: set[str] = set()
        for blob in client.list_blobs(name_starts_with=pref):
            rest = blob.name[len(pref):] if blob.name.startswith(pref) else blob.name
            parts = [p for p in rest.split("/") if p]
            if len(parts) < 2:
                continue  # a file sitting directly in the prefix, not in a folder
            if Path(parts[-1]).suffix.lower() not in IMAGE_SUFFIXES:
                continue
            charts.add(f"{pref}{parts[0]}")
        return sorted(charts)

    return call_with_retry(
        _list,
        attempts=AZURE_RETRY_ATTEMPTS,
        base_delay=AZURE_RETRY_BASE_DELAY,
        max_delay=AZURE_RETRY_MAX_DELAY,
        label=f"blob.list_charts:{container}/{pref}",
    )


def download_blob_bytes(container: str, blob_name: str) -> bytes:
    from azure_retry import call_with_retry
    from config import AZURE_RETRY_ATTEMPTS, AZURE_RETRY_BASE_DELAY, AZURE_RETRY_MAX_DELAY

    def _download() -> bytes:
        client = get_container_client(container)
        return client.download_blob(blob_name).readall()

    return call_with_retry(
        _download,
        attempts=AZURE_RETRY_ATTEMPTS,
        base_delay=AZURE_RETRY_BASE_DELAY,
        max_delay=AZURE_RETRY_MAX_DELAY,
        label=f"blob.download:{blob_name}",
    )


def upload_blob(
    container: str,
    blob_name: str,
    data: Union[bytes, BinaryIO],
    *,
    overwrite: bool = True,
) -> None:
    """Upload one blob with the same retry policy as downloads."""
    from azure_retry import call_with_retry
    from config import AZURE_RETRY_ATTEMPTS, AZURE_RETRY_BASE_DELAY, AZURE_RETRY_MAX_DELAY

    def _upload() -> None:
        client = get_container_client(container)
        # File handles need a seek(0) on retry or the second attempt uploads
        # an empty body after the first partial read.
        if hasattr(data, "seek"):
            data.seek(0)
        client.upload_blob(name=blob_name, data=data, overwrite=overwrite)

    call_with_retry(
        _upload,
        attempts=AZURE_RETRY_ATTEMPTS,
        base_delay=AZURE_RETRY_BASE_DELAY,
        max_delay=AZURE_RETRY_MAX_DELAY,
        label=f"blob.upload:{blob_name}",
    )
