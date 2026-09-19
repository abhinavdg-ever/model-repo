"""Azure Blob helpers for chart page download and manifest sweep."""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Any, BinaryIO, Optional, Union

from config import (
    AZURE_STORAGE_ACCOUNT_KEY,
    AZURE_STORAGE_ACCOUNT_NAME,
    AZURE_STORAGE_AUTH,
    AZURE_STORAGE_CONNECTION_STRING,
    AZURE_STORAGE_CONTAINER,
    IMAGE_SUFFIXES,
)

logger = logging.getLogger(__name__)
_FILENAME_PARTS = re.compile(r"(\d+)")


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

_INTERACTIVE_CREDENTIAL = None
_INTERACTIVE_LOCK = threading.Lock()


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
        _INTERACTIVE_CREDENTIAL = ChainedTokenCredential(
            ManagedIdentityCredential(),
            AzureCliCredential(process_timeout=30),
            InteractiveBrowserCredential(cache_persistence_options=cache),
        )
        logger.info(
            "Azure Storage: interactive Entra auth — managed identity, then "
            "az CLI, then a browser prompt if neither answers"
        )
        return _INTERACTIVE_CREDENTIAL


def get_blob_service_client():
    from azure.storage.blob import BlobServiceClient

    from azure_retry import azure_sdk_retry_kwargs

    retry = azure_sdk_retry_kwargs()

    if AZURE_STORAGE_CONNECTION_STRING:
        return BlobServiceClient.from_connection_string(
            AZURE_STORAGE_CONNECTION_STRING, **retry
        )

    account = AZURE_STORAGE_ACCOUNT_NAME
    if not account:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT_NAME is required")

    account_url = f"https://{account}.blob.core.windows.net"
    auth = AZURE_STORAGE_AUTH
    if auth in ENTRA_INTERACTIVE_MODES:
        return BlobServiceClient(
            account_url, credential=_interactive_credential(), **retry
        )
    if auth in ENTRA_MODES:
        from azure.identity import DefaultAzureCredential

        return BlobServiceClient(
            account_url, credential=DefaultAzureCredential(), **retry
        )
    if AZURE_STORAGE_ACCOUNT_KEY:
        return BlobServiceClient(
            account_url, credential=AZURE_STORAGE_ACCOUNT_KEY, **retry
        )
    raise RuntimeError("No Azure Storage credentials configured")


def get_container_client(container: Optional[str] = None):
    name = (container or AZURE_STORAGE_CONTAINER).strip()
    if not name:
        raise RuntimeError("blob container name is required")
    return get_blob_service_client().get_container_client(name)


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
