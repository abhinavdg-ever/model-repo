"""One chart run: what POST /api/charts/run does, callable from the API or a queue worker.

The API (RUN_MODE=inline) calls these from a BackgroundTask and only logs a failure.
The queue worker (RUN_MODE=queue, apps/worker/queue_worker.py) calls ``execute_run``,
which raises, so the Service Bus message is abandoned and delivered again.

``payload`` is an ``apps.orchestrator_api.schemas.RunRequest`` (duck-typed here: only its
properties are read), so this module imports no FastAPI and no ML at import time.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("core-pipeline")

CREDENTIAL_HINT = (
    "no usable Azure Storage credential. Set AZURE_STORAGE_AUTH=managed_identity with "
    "AZURE_CLIENT_ID (user-assigned MI on a VM), or AZURE_STORAGE_AUTH=key with "
    "AZURE_STORAGE_ACCOUNT_KEY, or make a managed identity / `az login` available to THIS "
    "process (it reads PATH at start, so a terminal opened before installing the CLI will "
    "not see it). Local runs with local_path are unaffected."
)


def is_credential_failure(exc: BaseException) -> bool:
    """Azure could not authenticate at all, as opposed to anything else.

    Matched by name rather than by import so this works whether or not the
    identity package is installed.
    """
    seen: BaseException | None = exc
    while seen is not None:
        if type(seen).__name__ == "ClientAuthenticationError":
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def join(prefix: Optional[str], folder: str) -> str:
    """`<prefix>/<folder>`, tolerant of missing / slash-wrapped / Windows ``\\``."""
    clean = (prefix or "").replace("\\", "/").strip().strip("/")
    name = (folder or "").replace("\\", "/").strip().strip("/")
    return f"{clean}/{name}" if clean else name


def write_after_run(payload: Any, chart_name: str) -> None:
    """Write the chart out, if a write path was given.

    Deliberately separate from the pipeline call: a write failure must not
    make a completed run look failed. Retry by calling /run again with
    chart_id/chart_name and the write path (sync skips files already there).
    """
    if not (payload.blob_write_path or payload.local_write_path):
        return

    write = payload.blob_write_path or payload.local_write_path
    try:
        from common.db import connect, get_chart_by_name, set_chart_output_path
        from common.db.path_ids import resolve_output_path

        out_path = resolve_output_path(chart_name, write_path=write)
        if out_path:
            with connect() as conn:
                row = get_chart_by_name(conn, chart_name)
                if row:
                    # Explicit write path wins as-is (no Raw_Input→Processed remap).
                    set_chart_output_path(conn, int(row["id"]), out_path)
    except Exception:
        logger.debug(
            "could not persist output_path for %s", chart_name, exc_info=True
        )

    from apps.worker.jobs.export_chart import write_chart

    # After skip_ocr, quality refreshed corrected-pages/; push outputs over
    # whatever is already on Processed (ocr / corrected-pages / imaging).
    overwrite = bool(payload.overwrite) or bool(payload.skip_ocr)

    try:
        result = write_chart(
            chart_name,
            local_path=payload.local_write_path,
            blob_container=payload.blob_container,
            blob_path=payload.blob_write_path,
            overwrite=overwrite,
            write_mode=payload.write_mode,
        )
        logger.info(
            "Wrote %s -> %s (%d written, %d skipped, %s)",
            chart_name,
            result["destination"],
            result["files_written"],
            result.get("files_skipped", 0),
            result["write_mode"],
        )
    except Exception:
        logger.exception(
            "Chart %s ran, but writing it out FAILED. The results are in the "
            "workspace; retry with POST /api/charts/run "
            '{"chart_name":"%s","local_write_path"|"blob_write_path":...}.',
            chart_name,
            chart_name,
        )


def run_blob_chart(payload: Any) -> dict[str, Any]:
    """``<container>/<input_path>/<chart_name>`` → ingest + pipeline → write. Raises."""
    from apps.worker.runner import ingest_and_run

    folder = payload.chart_name
    result = ingest_and_run(
        blob_container=payload.blob_container,
        blob_path=join(payload.blob_read_path, folder),
        chart_name=folder,
        force=payload.force,
        only=payload.only,
        through=payload.through,
        skip_ocr=payload.skip_ocr,
        redownload_pages=payload.redownload_pages,
    )
    write_after_run(payload, (result or {}).get("chart_name") or folder)
    return result or {}


def import_local_chart(payload: Any) -> dict[str, Any]:
    """Copy ``<input_path>/<chart_name>`` into the workspace and register it. Raises."""
    from apps.worker.stages.utilities.download_blob import import_local_folder

    return import_local_folder(
        str(Path(payload.input_path) / payload.chart_name),
        chart_name=payload.chart_name,
        force=payload.force,
        redownload_pages=payload.redownload_pages,
    )


def run_imported_chart(chart_id: int, payload: Any) -> None:
    """The stage chain on an already imported chart, then write. Raises."""
    from apps.worker.runner import run_pipeline_for_chart

    run_pipeline_for_chart(
        chart_id,
        force=payload.force,
        only=payload.only,
        through=payload.through,
        skip_ocr=payload.skip_ocr,
        redownload_pages=payload.redownload_pages,
    )
    write_after_run(payload, payload.chart_name)


def execute_run(
    payload: Any, on_chart: Optional[Callable[[int], None]] = None
) -> dict[str, Any]:
    """The whole of one /api/charts/run, for the queue worker. Raises on failure.

    ``on_chart(chart_id)`` is called as soon as the chart id is known (local input:
    after the import, before the stages).
    """
    if payload.is_blob:
        result = run_blob_chart(payload)
        chart_id = result.get("chart_id")
    else:
        imported = import_local_chart(payload)
        chart_id = int(imported["chart_id"])
        if on_chart:
            on_chart(chart_id)
        run_imported_chart(chart_id, payload)
    return {"chart_id": chart_id, "chart_name": payload.chart_name}
