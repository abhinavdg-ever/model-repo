"""What the optional features can actually do right now.

Every optional dependency degrades a stage rather than failing it — no Azure
Document Intelligence means final2 produces no text, no GLiNER means no page can
be marked `wrong_member`, no blob credentials means `run` works from a local
path and not from a container. The run records that (see the working rules in
CLAUDE.md), but only after it has happened. This module answers the same
question *before* a chart is submitted.

One source, two readers: the startup log and `GET /health`. They disagreed
before this existed — the log named the database and the worker count, /health
named NER and the DOS LLM, and neither mentioned blob at all.

**Configuration, not reachability.** Everything here is local: environment
variables and `find_spec`. Nothing opens a socket, so /health stays fast and
cannot hang on a network that is down. `probe_blob()` is the one exception and
is called only from startup, bounded, once — the same shape as the database
probe next to it.
"""
from __future__ import annotations

from importlib.util import find_spec
from typing import Any

from config import (
    AZURE_CLIENT_ID,
    AZURE_DI_FEATURES,
    AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT,
    AZURE_DOCUMENT_INTELLIGENCE_KEY,
    AZURE_OPENAI_DEPLOYMENT,
    AZURE_OPENAI_ENDPOINT,
    AZURE_PRINCIPAL_ID,
    AZURE_STORAGE_ACCOUNT_KEY,
    AZURE_STORAGE_ACCOUNT_NAME,
    AZURE_STORAGE_AUTH,
    AZURE_STORAGE_CONNECTION_STRING,
    AZURE_STORAGE_CONTAINER,
    DOS_LLM_ENABLED,
    MEMBER_NER_ENABLED,
    SKIP_OCR,
)


def blob_status() -> dict[str, Any]:
    """Can we reach a blob container at all, and as whom?

    Mirrors the precedence in `db.blob_store.get_blob_service_client` — connection
    string, then Entra / managed identity, then account key — so this cannot say
    "ready" for a credential that function would not use.
    """
    status: dict[str, Any] = {
        "container": AZURE_STORAGE_CONTAINER,
        "account": AZURE_STORAGE_ACCOUNT_NAME or None,
        "auth": None,
        "ready": False,
        "client_id": AZURE_CLIENT_ID or None,
        "principal_id": AZURE_PRINCIPAL_ID or None,
    }

    if find_spec("azure.storage.blob") is None:
        status["reason"] = "azure-storage-blob not installed"
        return status

    if AZURE_STORAGE_CONNECTION_STRING:
        status.update(auth="connection_string", ready=True)
        return status

    if not AZURE_STORAGE_ACCOUNT_NAME:
        status["reason"] = (
            "AZURE_STORAGE_ACCOUNT_NAME not set (and no AZURE_STORAGE_CONNECTION_STRING)"
        )
        return status

    from db.blob_store import (
        ENTRA_INTERACTIVE_MODES,
        ENTRA_MODES,
        MANAGED_IDENTITY_MODES,
    )

    if AZURE_STORAGE_AUTH in MANAGED_IDENTITY_MODES:
        if find_spec("azure.identity") is None:
            status["auth"] = "managed_identity"
            status["reason"] = (
                "AZURE_STORAGE_AUTH=managed_identity but azure-identity not installed"
            )
            return status
        status.update(auth="managed_identity", ready=True)
        return status

    if AZURE_STORAGE_AUTH in ENTRA_MODES | ENTRA_INTERACTIVE_MODES:
        interactive = AZURE_STORAGE_AUTH in ENTRA_INTERACTIVE_MODES
        label = "entra_interactive" if interactive else "entra"
        if find_spec("azure.identity") is None:
            status["auth"] = label
            status["reason"] = (
                f"AZURE_STORAGE_AUTH={label} but azure-identity not installed"
            )
            return status
        status.update(auth=label, ready=True)
        if interactive:
            # The probe must not be the thing that opens a browser. It runs at
            # startup with nobody necessarily watching, and a prompt there would
            # block a server start on a human.
            status["probe"] = "skipped — would prompt"
        return status

    if AZURE_STORAGE_ACCOUNT_KEY:
        status.update(auth="key", ready=True)
        return status

    status["reason"] = (
        f"AZURE_STORAGE_AUTH={AZURE_STORAGE_AUTH!r} but no AZURE_STORAGE_ACCOUNT_KEY"
    )
    return status


PROBE_DEADLINE_SECONDS = 5


def _blob_round_trip(status: dict[str, Any], timeout: int) -> None:
    """One call to the container, with the SDK's own commentary suppressed.

    When no credential is available `DefaultAzureCredential` logs a ~20-line
    WARNING listing all nine sources it tried. That is genuinely useful the
    first time you see it and pure noise on every start after — and it is
    redundant here, because the banner line this probe feeds says the same
    thing in one line, with the reason attached.

    Only for the duration of the probe. A credential failure during real chart
    processing still warns in full, which is where the detail earns its space.
    """
    import logging

    identity = logging.getLogger("azure.identity")
    previous = identity.level
    identity.setLevel(logging.ERROR)
    try:
        from db.blob_store import get_container_client

        client = get_container_client(AZURE_STORAGE_CONTAINER)
        client.get_container_properties(timeout=timeout)
        status["reachable"] = True
    except Exception as exc:
        status["reachable"] = False
        status["reason"] = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
    finally:
        identity.setLevel(previous)


def probe_blob(timeout: int = PROBE_DEADLINE_SECONDS) -> dict[str, Any]:
    """`blob_status()` plus one round trip to the container, under a hard deadline.

    Credentials being present is not the same as the role being assigned: an
    Entra identity without **Storage Blob Data Reader** authenticates and then
    fails with a 403 on the first real call. That is a chart-time failure this
    turns into a startup line.

    **The deadline is the point.** Passing `timeout` to the SDK call bounds only
    the HTTP request; `DefaultAzureCredential` runs first, tries nine credential
    sources with its own retries and backoff, and ignores it completely. On a
    machine with no managed identity and no `az login` that took 96 seconds —
    during which startup blocked, which is precisely what the database probe
    beside it was rewritten to stop doing. A daemon thread we stop waiting on
    cannot hold the server up, whatever the SDK decides to do next.

    Called once, from startup. Never raises.
    """
    status = blob_status()
    if not status["ready"]:
        return status
    if status.get("probe") == "skipped — would prompt":
        # Verifying reachability would trigger the browser prompt this mode
        # exists to allow — at startup, where nobody asked for it. The first
        # real chart does the login instead, which is when a human is present.
        return status

    import threading

    worker = threading.Thread(
        target=_blob_round_trip, args=(status, timeout), daemon=True
    )
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        # Still going. Leave it to finish into a dict nobody reads and move on:
        # the answer is not worth delaying every start for.
        status["reachable"] = None
        status["reason"] = (
            f"reachability unverified — probe exceeded {timeout}s "
            "(credentials are configured; the round trip did not finish in time)"
        )
    return status


def azure_di_status() -> dict[str, Any]:
    """Final OCR 2. Absent, handwritten pages get no pass-2 verdict."""
    ready = bool(
        AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT and AZURE_DOCUMENT_INTELLIGENCE_KEY
    )
    features = [
        p.strip()
        for p in (AZURE_DI_FEATURES or "").split(",")
        if p.strip() and p.strip().casefold() not in {"0", "false", "off", "none", "-"}
    ]
    status: dict[str, Any] = {
        "endpoint": AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT or None,
        "ready": ready,
        "features": features,
    }
    if not ready:
        missing = [
            name
            for name, value in (
                ("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT", AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT),
                ("AZURE_DOCUMENT_INTELLIGENCE_KEY", AZURE_DOCUMENT_INTELLIGENCE_KEY),
            )
            if not value
        ]
        status["reason"] = f"not set: {', '.join(missing)}"
    return status


def dos_llm_status() -> dict[str, Any]:
    """The DOS LLM pass, and which credential it resolved to."""
    status: dict[str, Any] = {
        "enabled": DOS_LLM_ENABLED,
        "endpoint": AZURE_OPENAI_ENDPOINT or None,
        "deployment": AZURE_OPENAI_DEPLOYMENT,
        "ready": DOS_LLM_ENABLED,
    }
    try:
        import sys
        from pathlib import Path

        lib = str(Path(__file__).resolve().parent / "stages" / "lib" / "dos")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        from azure_llm import resolved_auth

        status["auth"] = resolved_auth()
    except Exception:
        status["auth"] = None

    if not DOS_LLM_ENABLED:
        if not AZURE_OPENAI_ENDPOINT:
            status["reason"] = "not set: AZURE_OPENAI_ENDPOINT"
        elif status["auth"] is None:
            status["reason"] = "no usable credential (set AZURE_OPENAI_API_KEY, or AZURE_OPENAI_AUTH=entra)"
        else:
            status["reason"] = "DOS_LLM_ENABLED=false"
    return status


def ner_status() -> dict[str, Any]:
    """The GLiNER layer. Never raises — an optional feature's probe must not 500."""
    try:
        import sys
        from pathlib import Path

        lib = str(Path(__file__).resolve().parent / "stages" / "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        from member import ner_status as _ner_status

        return _ner_status()
    except Exception as exc:
        return {"enabled": MEMBER_NER_ENABLED, "ready": False, "reason": str(exc)}


def all_capabilities(*, probe: bool = False) -> dict[str, Any]:
    """Every optional feature at once. `probe=True` allows one blob round trip."""
    from stages.lib.imaging.docling_ocr import docling_status

    return {
        "blob": probe_blob() if probe else blob_status(),
        "azure_document_intelligence": azure_di_status(),
        "dos_llm": dos_llm_status(),
        "member_ner": ner_status(),
        "docling_final1": docling_status(),
        "skip_ocr": {
            "enabled": SKIP_OCR,
            "ready": True,
            "reason": (
                "reuse ocr/ when present; otherwise OCR still runs"
                if SKIP_OCR
                else "SKIP_OCR=false"
            ),
        },
    }


def _one_line(status: dict[str, Any], on: str) -> str:
    """`on` when ready, otherwise the single precondition to fix."""
    if status.get("ready"):
        if status.get("reachable") is False:
            return f"configured but UNREACHABLE — {status.get('reason')}"
        if status.get("reachable") is None and status.get("reason"):
            # Probe timed out. Not the same as "broken" — say so rather than
            # implying a verdict we never got.
            return f"{on} ({status['reason']})"
        return on
    return f"off — {status.get('reason') or 'not configured'}"


def startup_lines(caps: dict[str, Any]) -> list[tuple[str, str]]:
    """(label, value) pairs for the startup banner, aligned by the caller."""
    blob = caps["blob"]
    di = caps["azure_document_intelligence"]
    llm = caps["dos_llm"]
    ner = caps["member_ner"]
    docling = caps.get("docling_final1") or {}
    skip_ocr = caps.get("skip_ocr") or {}

    blob_on = f"OK — {blob.get('auth')}"
    if blob.get("account"):
        blob_on += f", account={blob['account']}"
    if blob.get("client_id"):
        blob_on += f", client_id={blob['client_id']}"
    blob_on += f", container={blob.get('container')}"

    di_on = f"OK — {di.get('endpoint')}"
    if di.get("features"):
        di_on += f", features={','.join(di['features'])}"

    llm_on = f"OK — deployment={llm.get('deployment')}, auth={llm.get('auth')}"
    ner_on = f"OK — model={ner.get('model_id')}"
    docling_on = f"OK — models={docling.get('models_dir')}"
    skip_on = "ON — reuse ocr/ when present"

    return [
        ("blob", _one_line(blob, blob_on)),
        ("final1 Docling", _one_line(docling, docling_on)),
        ("final2 OCR", _one_line(di, di_on)),
        ("DOS LLM", _one_line(llm, llm_on)),
        ("member NER", _one_line(ner, ner_on)),
        ("skip OCR", _one_line(skip_ocr, skip_on)),
    ]
