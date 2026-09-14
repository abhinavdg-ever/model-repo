"""FastAPI surface for core-pipeline: ingest, status, rerun, manifest sweep.

Deployed on its own (see core-pipeline/docker-compose.yml). The review UI is a
separate deployment and does not call this service — the two share the Postgres
database and the `data/folders` volume, not an HTTP boundary.

Long work runs in a BackgroundTask: every mutating endpoint returns 202 with a
chart_id, and progress is read back from GET /api/charts/{id}. See
docs/API.md for the full request/response reference.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from config import (
    API_HOST,
    API_PORT,
    DOS_LLM_ENABLED,
    MEMBER_NER_ENABLED,
    STAGE_WORKERS,
)
from db import close_pool, connect, get_chart, get_chart_by_name, list_pages, list_stages
from db.chart_status import refresh_chart_status
from jobs.manifest_sweeper import run_load
from jobs.export_chart import SKIP_ORIG_PAGES, WRITE_MODES
from orchestrator.runner import (
    STAGE_NAMES,
    ingest_and_run,
    resolve_stage,
    run_pipeline_for_chart,
)

from logging_setup import configure_logging

# Root at INFO for our own per-page progress lines; the Azure SDKs quieted to
# WARNING, or they log every request and response header at INFO and bury them.
# AZURE_LOG_LEVEL=INFO puts the dump back when debugging a 403 or a throttle.
configure_logging(logging.INFO)
logger = logging.getLogger("core-pipeline")

app = FastAPI(
    title="Advantmed Core Pipeline",
    version="0.2.0",
    description=(
        "Chart intake and the imaging pipeline. Mutating endpoints are "
        "asynchronous: they return 202 and work continues in the background."
    ),
)


def _safe_db_url(url: str) -> str:
    """DATABASE_URL with the password replaced by ***, for logging.

    Worth logging at all because the commonest failure is pointing at the wrong
    database and not knowing it — but the password must never reach a log file.
    """
    import re

    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url or "")


@app.on_event("startup")
def _startup() -> None:
    from config import DATA_ROOT, DATABASE_URL, METADATA_ROOT, STAGE_WORKERS

    logger.info("core-pipeline starting")
    logger.info("  database    : %s", _safe_db_url(DATABASE_URL))
    logger.info("  data root   : %s", DATA_ROOT)
    logger.info("  metadata    : %s", METADATA_ROOT)
    logger.info("  workers     : %s", STAGE_WORKERS)
    # Short-timeout probe, NOT the pool: the pool retries for 30 seconds, which
    # would hold up startup — and block /docs and /health, the two endpoints
    # whose whole job is to work when the database does not.
    try:
        import psycopg

        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM pipeline_stage")
                count = cur.fetchone()[0]
        logger.info("  schema      : OK, %d stage(s) registered", count)
    except Exception as exc:
        logger.warning("  database    : UNREACHABLE — %s", str(exc).splitlines()[0])
        logger.warning(
            "  Mutating endpoints will return 503 until this is fixed. "
            "DATABASE_URL is read once at startup, so restart after editing .env."
        )

    # Which optional features are actually on. Each of these degrades a stage
    # rather than failing it, so without this line the first sign is a chart
    # that completed with less in it than expected. probe=True allows one
    # bounded round trip to the blob container: credentials being present is
    # not the same as the role being assigned.
    try:
        from capabilities import all_capabilities, startup_lines

        for label, value in startup_lines(all_capabilities(probe=True)):
            logger.info("  %-11s : %s", label, value)
    except Exception as exc:  # a status probe must never stop the server
        logger.warning("  capabilities: probe failed — %s", exc)


@app.on_event("shutdown")
def _shutdown() -> None:
    close_pool()


# --- request models ---------------------------------------------------------


_STAGE_HELP = (
    "Stage name, 'name' for pass 1 or 'name:2' for pass 2. "
    f"Known: {', '.join(STAGE_NAMES)}"
)


class StageSelection(BaseModel):
    """The two ways to run less than the whole chain.

    They answer different questions. ``through`` is "take it this far and
    stop" — the chain from the top, bounded. ``only`` is "just do this bit" —
    whatever already happened before. A stage named in ``only`` runs against
    whatever its inputs are on disk, so it is the right tool when an earlier
    stage's output is good and the last step changed; it is the wrong tool on
    a chart that has never run.
    """

    through: Optional[str] = Field(
        None,
        description=f"Run the chain and stop after this stage. {_STAGE_HELP}",
        examples=["ocr_final2"],
    )
    only: Optional[list[str]] = Field(
        None,
        description=f"Run only these stages, in chain order. {_STAGE_HELP}",
        examples=[["dos_extract"]],
    )


class RunRequest(StageSelection):
    """One chart: read it, run the chain, and optionally write the results out.

    A source is a **read path plus a folder name**, which resolve together:

        blob_read_path  Raw_Input/Run1/Batch1/DEID_PNGs
        blob_read_folder_name              52754737_48221214
        -> reads  Raw_Input/Run1/Batch1/DEID_PNGs/52754737_48221214/

    **The folder name is the chart name.** It is what appears in `chart_list`,
    names every output CSV, and is the key the member manifest joins on
    (`record_id`), so it is given rather than inferred.

    The write path resolves the same way, with the same folder name:

        blob_write_path Processed/Run1
        -> writes Processed/Run1/52754737_48221214/

    so a chart keeps its identity on both sides and two charts written to one
    destination cannot merge.

    Give a blob source or a local source, not both. A write path is optional —
    without one the chart still runs and the results stay in the workspace,
    where `POST /api/charts/write` can send them later. **Read and write use
    the same backend:** blob in, blob out; local in, local out.
    """

    # --- blob ---
    blob_container: Optional[str] = Field(
        None, description="Azure Blob container. Used for both read and write."
    )
    blob_read_path: Optional[str] = Field(
        None,
        description="Prefix holding the chart folder",
        examples=["Raw_Input/Run1/Batch1/DEID_PNGs"],
    )
    blob_read_folder_name: Optional[str] = Field(
        None,
        description="The chart folder under blob_read_path. Becomes the chart name.",
        examples=["52754737_48221214"],
    )
    blob_write_path: Optional[str] = Field(
        None,
        description="Prefix to write results to. Omit to run without writing.",
        examples=["Processed/Run1"],
    )

    # --- local ---
    local_read_path: Optional[str] = Field(
        None,
        description=(
            "Directory ON THE SERVER holding the chart folder. Under Docker "
            "this must be a path inside the container."
        ),
        examples=["/data/inbox"],
    )
    local_folder_name: Optional[str] = Field(
        None,
        description="The chart folder under local_read_path. Becomes the chart name.",
        examples=["52754737_48221214"],
    )
    local_write_path: Optional[str] = Field(
        None, description="Directory to write results to. Omit to run without writing."
    )

    # --- write options ---
    write_mode: str = Field(
        SKIP_ORIG_PAGES,
        description=(
            "skip_orig_pages (default) omits pages/ — the originals came from "
            "the source you are writing back to, so re-sending them doubles "
            "storage and transfer. corrected-pages/, ocr/ and imaging/ are "
            "still written. all_files sends everything."
        ),
    )
    overwrite: bool = Field(
        False, description="Replace files already at the write destination"
    )

    # --- both ---
    run_id: Optional[str] = None
    batch_id: Optional[str] = None
    force: bool = Field(
        False,
        description="Reprocess pages already completed. Default resumes instead.",
    )


class BatchRequest(StageSelection):
    """Every chart under one read path, run one at a time, optionally written.

    The same vocabulary as RunRequest **minus the folder name**: here every
    sub-folder holding images IS a chart, so each supplies its own. A chart read
    from `<read_path>/52754737_48221214/` is written to
    `<write_path>/52754737_48221214/`.

    Each chart goes through the same call a single `/run` makes, so an option
    means the same thing in both places.
    """

    # --- blob ---
    blob_container: Optional[str] = Field(
        None, description="Azure Blob container. Used for both read and write."
    )
    blob_read_path: Optional[str] = Field(
        None,
        description="Prefix whose sub-folders are charts",
        examples=["Raw_Input/Run1/Batch1/DEID_PNGs"],
    )
    blob_write_path: Optional[str] = Field(
        None, description="Prefix to write each chart to. Omit to run without writing."
    )

    # --- local ---
    local_read_path: Optional[str] = Field(
        None,
        description=(
            "Parent directory ON THE SERVER; each sub-folder holding images is "
            "one chart. Under Docker this must be a path inside the container."
        ),
    )
    local_write_path: Optional[str] = Field(
        None, description="Directory to write each chart to. Omit to run without writing."
    )

    # --- write options ---
    write_mode: str = Field(
        SKIP_ORIG_PAGES,
        description="skip_orig_pages (default) omits pages/; all_files sends everything",
    )
    overwrite: bool = Field(
        False, description="Replace files already at the write destination"
    )

    # --- both ---
    force: bool = False
    limit: Optional[int] = Field(
        None, description="Only the first N charts — use for a dry run first"
    )
    run_id: Optional[str] = None
    batch_id: Optional[str] = None


class WriteRequest(BaseModel):
    """Write an already-run chart out, without reprocessing it.

    Same write vocabulary as `/run`. Use this to send a chart to a second
    destination, or to export one that ran before a write path was given.
    """

    chart_name: str = Field(
        ...,
        description="Folder name under data/folders — the chart to write",
        examples=["52743839_44976074"],
    )
    local_write_path: Optional[str] = Field(
        None, description="Destination directory ON THE SERVER"
    )
    blob_container: Optional[str] = None
    blob_write_path: Optional[str] = Field(
        None, description="Destination prefix inside the container"
    )
    write_mode: str = Field(
        SKIP_ORIG_PAGES,
        description="skip_orig_pages (default) omits pages/; all_files sends everything",
    )
    overwrite: bool = Field(
        False,
        description=(
            "Replace files already at the destination. Without it a "
            "non-empty destination is an error rather than a silent merge."
        ),
    )


class RerunRequest(StageSelection):
    """Re-run an existing chart. Same stage vocabulary as run and batch."""

    force: bool = Field(
        False, description="Reprocess completed pages instead of resuming"
    )


class ManifestSweepRequest(BaseModel):
    """Load a batch manifest into manifest_member_list (upsert)."""

    local_path: Optional[str] = Field(
        None, description="Local CSV/XLSX file or a directory of them"
    )
    blob_container: Optional[str] = None
    blob_prefix: Optional[str] = None
    run_id: Optional[str] = Field(None, description="Overrides the R# in the filename")
    batch_id: Optional[str] = Field(None, description="Overrides the B# in the filename")
    mirror_local: bool = Field(
        True, description="Copy blob manifests into review-ui/data/metadata"
    )


# --- background wrappers ----------------------------------------------------


# A BackgroundTask runs after the 202 has been sent, so its outcome can only
# ever reach the operator through the log. Logging failures alone left a
# successful run indistinguishable from one that never started.
def _is_credential_failure(exc: BaseException) -> bool:
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


def _join(prefix: Optional[str], folder: str) -> str:
    """`<prefix>/<folder>`, tolerant of a missing or slash-wrapped prefix."""
    clean = (prefix or "").strip().strip("/")
    return f"{clean}/{folder}" if clean else folder


def _write_summary(
    body: "RunRequest", folder: str, write_to: Optional[str]
) -> Optional[dict[str, Any]]:
    """What the run will write, echoed back so the caller can see it was read."""
    if not write_to:
        return None
    destination = (
        f"{body.blob_container}/{_join(body.blob_write_path, folder)}"
        if body.blob_write_path
        else str(Path(write_to) / folder)
    )
    return {
        "destination": destination,
        "write_mode": body.write_mode,
        "overwrite": body.overwrite,
    }


def _write_after_run(payload: "RunRequest", chart_name: str) -> None:
    """Write the chart out, if a write path was given.

    Deliberately separate from the pipeline call: a write failure must not
    make a completed run look failed. The chart is in the workspace either
    way, and POST /api/charts/write can retry without reprocessing.
    """
    if not (payload.blob_write_path or payload.local_write_path):
        return
    from jobs.export_chart import write_chart

    try:
        result = write_chart(
            chart_name,
            local_path=payload.local_write_path,
            blob_container=payload.blob_container,
            blob_path=payload.blob_write_path,
            overwrite=payload.overwrite,
            write_mode=payload.write_mode,
        )
        logger.info(
            "Wrote %s -> %s (%d file(s), %s)",
            chart_name, result["destination"], result["files_written"],
            result["write_mode"],
        )
    except Exception:
        logger.exception(
            "Chart %s ran, but writing it out FAILED. The results are in the "
            "workspace; POST /api/charts/write can retry without reprocessing.",
            chart_name,
        )


def _bg_pipeline_then_write(
    chart_id: int, chart_name: str, payload: "RunRequest"
) -> None:
    _bg_pipeline(chart_id, payload.force, payload.only, payload.through)
    _write_after_run(payload, chart_name)


def _bg_run(payload: "RunRequest") -> None:
    folder = payload.blob_read_folder_name or ""
    blob_path = _join(payload.blob_read_path, folder)
    source = f"{payload.blob_container}/{blob_path}"
    logger.info("Background run starting: %s", source)
    try:
        result = ingest_and_run(
            blob_container=payload.blob_container,
            blob_path=blob_path,
            chart_name=folder,
            run_id=payload.run_id,
            batch_id=payload.batch_id,
            force=payload.force,
            only=payload.only,
            through=payload.through,
        )
        logger.info(
            "Background run finished: %s -> chart_id=%s",
            source, (result or {}).get("chart_id"),
        )
        _write_after_run(payload, result.get("chart_name") or folder)
    except Exception as exc:
        if _is_credential_failure(exc):
            # The traceback is fifteen frames of SDK plumbing and the message
            # is the same nine-source list already in the log. Neither tells
            # the operator the one thing to do.
            logger.error(
                "Background run FAILED for %s — no usable Azure Storage "
                "credential. Set AZURE_STORAGE_AUTH=key with "
                "AZURE_STORAGE_ACCOUNT_KEY, or make a managed identity / "
                "`az login` available to THIS process (it reads PATH at "
                "start, so a terminal opened before installing the CLI will "
                "not see it). Local runs with local_path are unaffected.",
                source,
            )
        else:
            logger.exception("Background run FAILED for %s", source)


def _bg_write(payload: "WriteRequest") -> None:
    from jobs.export_chart import write_chart

    logger.info("Background write starting: %s", payload.chart_name)
    try:
        result = write_chart(
            payload.chart_name,
            local_path=payload.local_write_path,
            blob_container=payload.blob_container,
            blob_path=payload.blob_write_path,
            overwrite=payload.overwrite,
            write_mode=payload.write_mode,
        )
        logger.info(
            "Background write finished: %s -> %s (%d file(s))",
            payload.chart_name, result["destination"], result["files_written"],
        )
    except Exception:
        logger.exception("Background write FAILED for %s", payload.chart_name)


def _bg_pipeline(
    chart_id: int,
    force: bool,
    only: Optional[list[str]],
    through: Optional[str] = None,
) -> None:
    logger.info(
        "Background pipeline starting: chart_id=%s force=%s only=%s through=%s",
        chart_id, force, only or "all stages", through or "end of chain",
    )
    try:
        run_pipeline_for_chart(chart_id, force=force, only=only, through=through)
        logger.info("Background pipeline finished: chart_id=%s", chart_id)
    except Exception:
        logger.exception("Background pipeline FAILED for chart %s", chart_id)


def _bg_batch(payload: "BatchRequest") -> None:
    from jobs.batch_intake import run_batch

    source = (
        payload.local_read_path
        or f"{payload.blob_container}/{payload.blob_read_path}"
    )
    logger.info("Background batch starting: %s", source)
    try:
        result = run_batch(
            local_read_path=payload.local_read_path,
            blob_container=payload.blob_container,
            blob_read_path=payload.blob_read_path,
            local_write_path=payload.local_write_path,
            blob_write_path=payload.blob_write_path,
            write_mode=payload.write_mode,
            overwrite=payload.overwrite,
            force=payload.force,
            only=payload.only,
            through=payload.through,
            limit=payload.limit,
            run_id=payload.run_id,
            batch_id=payload.batch_id,
        )
        logger.info(
            "Background batch finished: %s -> %d/%d completed, %d failed in %.1fs",
            source, result["completed"], result["charts_found"],
            result["failed"], result["duration_seconds"],
        )
        for chart in result["charts"]:
            if chart["status"] == "failed":
                logger.warning("  failed: %s — %s", chart["name"], chart.get("error"))
    except Exception:
        logger.exception("Background batch FAILED: %s", source)


def _bg_manifest(payload: ManifestSweepRequest) -> None:
    source = payload.local_path or f"{payload.blob_container}/{payload.blob_prefix}"
    logger.info("Background manifest sweep starting: %s", source)
    try:
        result = run_load(
            local_path=payload.local_path,
            blob_container=payload.blob_container,
            blob_prefix=payload.blob_prefix,
            run_id=payload.run_id,
            batch_id=payload.batch_id,
            mirror_local=payload.mirror_local,
        )
        logger.info(
            "Background manifest sweep finished: %s -> %s file(s), "
            "+%s inserted, ~%s updated, %s skipped%s",
            source,
            result["files"], result["inserted"], result["updated"], result["skipped"],
            f", {len(result['errors'])} error(s)" if result.get("errors") else "",
        )
        for err in result.get("errors") or []:
            logger.warning("  manifest error: %s", err)
    except Exception:
        logger.exception("Background manifest sweep FAILED: %s", source)


def _require_db() -> None:
    """Fail fast if Postgres is unreachable, instead of accepting work we cannot do.

    The mutating endpoints return 202 and hand off to a BackgroundTask. Without
    this check a bad DATABASE_URL produced a cheerful 202, then a PoolTimeout 30
    seconds later that only ever appeared in the server log — so the caller saw
    "accepted" and no rows, with nothing connecting the two. A short-timeout
    probe turns that into an immediate 503 naming the real error.
    """
    import psycopg

    from config import DATABASE_URL

    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"database unavailable: {exc} "
                "(check DATABASE_URL in core-pipeline/.env, and restart the API "
                "after editing it — the value is read once at startup)"
            ),
        ) from exc


# --- endpoints --------------------------------------------------------------


@app.get("/health", tags=["ops"])
def health() -> dict[str, Any]:
    """Liveness plus the toggles that change what a run actually does."""
    # Same source as the startup banner, so the two cannot disagree. No network
    # here: /health must stay fast and must not hang when Azure is down.
    try:
        from capabilities import all_capabilities

        caps = all_capabilities()
    except Exception as exc:  # never let a probe fail on an optional feature
        caps = {"member_ner": {"enabled": MEMBER_NER_ENABLED, "ready": False,
                               "reason": str(exc)}}

    return {
        "status": "ok",
        # member_ner.ready=false means member verification runs rules-only: no
        # page can be marked wrong_member, so no document can be Rejected.
        # blob.ready=false means run/batch/write work locally but not from blob.
        **caps,
        "dos_llm_enabled": DOS_LLM_ENABLED,
        "stage_workers": STAGE_WORKERS,
    }


@app.get("/ready", tags=["ops"])
def ready() -> dict[str, Any]:
    """Readiness — verifies the database is reachable and schema v7 is applied."""
    try:
        with connect() as conn:
            stages = list_stages(conn)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}")
    if not stages:
        raise HTTPException(
            status_code=503,
            detail="pipeline_stage is empty — apply schema/schema.sql",
        )
    return {"status": "ready", "stages": len(stages)}


@app.get("/api/stages", tags=["ops"])
def get_stages() -> dict[str, Any]:
    """The pipeline's shape, straight from the pipeline_stage table."""
    with connect() as conn:
        return {"stages": list_stages(conn, phase1_only=False)}


def _validate_stages(body: "StageSelection") -> None:
    """Reject an unknown stage name with a 400 naming the known ones.

    Without this a typo reaches the background task, where it becomes a log
    line the caller never sees — the run just does nothing.
    """
    for token in ([body.through] if body.through else []) + list(body.only or []):
        try:
            resolve_stage(token)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/charts/run", status_code=202, tags=["charts"])
def run_chart(body: RunRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Read one chart, run the stage chain, and optionally write the results.

    Source is a read path plus a folder name, which resolve together; the
    folder name becomes the chart name. The write path, when given, resolves
    the same way — `<write_path>/<folder_name>/`.

    Blob and local are separate worlds: give one or the other, and read and
    write stay on the same backend.

    Returns immediately. Poll GET /api/charts/by-name/{folder_name}.
    """
    has_blob = bool(body.blob_container or body.blob_read_path or body.blob_read_folder_name)
    has_local = bool(body.local_read_path or body.local_folder_name)
    if has_blob and has_local:
        raise HTTPException(
            status_code=400, detail="Give a blob source or a local source, not both"
        )
    if not has_blob and not has_local:
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide blob_container + blob_read_path + blob_read_folder_name, "
                "or local_read_path + local_folder_name"
            ),
        )
    if body.write_mode not in WRITE_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"write_mode must be one of {', '.join(WRITE_MODES)}",
        )

    if has_blob:
        missing = [
            name
            for name, value in (
                ("blob_container", body.blob_container),
                ("blob_read_path", body.blob_read_path),
                ("blob_read_folder_name", body.blob_read_folder_name),
            )
            if not value
        ]
        if missing:
            raise HTTPException(
                status_code=400, detail=f"blob mode also needs: {', '.join(missing)}"
            )
        if body.local_write_path:
            raise HTTPException(
                status_code=400,
                detail="A blob source writes to blob_write_path, not local_write_path",
            )
        folder = body.blob_read_folder_name
    else:
        if not (body.local_read_path and body.local_folder_name):
            raise HTTPException(
                status_code=400,
                detail="local mode needs both local_read_path and local_folder_name",
            )
        if body.blob_write_path:
            raise HTTPException(
                status_code=400,
                detail="A local source writes to local_write_path, not blob_write_path",
            )
        folder = body.local_folder_name

    _validate_stages(body)
    _require_db()

    write_to = body.blob_write_path or body.local_write_path
    if has_local:
        # Resolve the intake now: a bad path should be a 400 the caller sees,
        # not a background failure in a log. The chain still runs in the
        # background — that is the part that takes minutes.
        from stages.download_blob import import_local_folder

        source = str(Path(body.local_read_path) / folder)
        try:
            result = import_local_folder(
                source,
                chart_name=folder,
                force=body.force,
                run_id=body.run_id,
                batch_id=body.batch_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        background_tasks.add_task(
            _bg_pipeline_then_write, result["chart_id"], folder, body
        )
        return {
            "status": "accepted",
            "mode": "local",
            "chart_id": result["chart_id"],
            "chart_name": folder,
            "source": source,
            "imported": result["imported"],
            "manifest": result["manifest"],
            "page_count": result["page_count"],
            "through": body.through,
            "only": body.only,
            "write": _write_summary(body, folder, write_to),
            "poll": f"/api/charts/{result['chart_id']}",
        }

    background_tasks.add_task(_bg_run, body)
    return {
        "status": "accepted",
        "mode": "blob",
        "chart_name": folder,
        "source": f"{body.blob_container}/{_join(body.blob_read_path, folder)}",
        "through": body.through,
        "only": body.only,
        "write": _write_summary(body, folder, write_to),
        "poll": f"/api/charts/by-name/{folder}",
    }


@app.post("/api/charts/write", status_code=202, tags=["charts"])
def write_chart_out(
    body: WriteRequest, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Write a finished chart's folder back out to blob or local disk.

    The reverse of run's intake step. The chart name is appended to the
    destination, exactly as `/run` does, so `<write_path>/<chart_name>/`.

    `write_mode` defaults to skip_orig_pages: corrected-pages/, ocr/ and
    imaging/ go, pages/ does not, because the originals came from the source
    you are usually writing back to.

    Returns immediately; a large chart is a lot of bytes. Watch the server log.
    """
    from config import chart_dir

    if bool(body.local_write_path) == bool(body.blob_write_path):
        raise HTTPException(
            status_code=400,
            detail="Provide exactly one destination: local_write_path, or blob_container + blob_write_path",
        )
    if body.blob_write_path and not body.blob_container:
        raise HTTPException(
            status_code=400,
            detail="blob_container must be given with blob_write_path",
        )
    # Check the source exists now rather than in the background, so a typo in
    # the chart name is a 404 the caller sees.
    root = chart_dir(body.chart_name)
    if not root.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"No chart workspace at {root} — has '{body.chart_name}' been run?",
        )

    # A local destination can be checked here for free, and a clash is the most
    # likely mistake — writing a second chart over the first. Checking it in the
    # background would hand the caller a 202 and put the refusal in a log they
    # never read. The blob equivalent is a network round trip, so it stays in
    # write_chart, where the same guard runs before a single byte is uploaded.
    if body.local_write_path and not body.overwrite:
        dest = (Path(body.local_write_path).expanduser() / body.chart_name)
        clash = [q for q in dest.rglob("*") if q.is_file()] if dest.is_dir() else []
        if clash:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"{dest} already holds {len(clash)} file(s). "
                    "Pass overwrite=true to replace them."
                ),
            )

    if body.write_mode not in WRITE_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"write_mode must be one of {', '.join(WRITE_MODES)}",
        )

    background_tasks.add_task(_bg_write, body)
    destination = (
        f"{body.blob_container}/{_join(body.blob_write_path, body.chart_name)}"
        if body.blob_write_path
        else str(Path(body.local_write_path) / body.chart_name)
    )
    return {
        "status": "accepted",
        "mode": "local" if body.local_write_path else "blob",
        "chart_name": body.chart_name,
        "source": str(root),
        "destination": destination,
        "write_mode": body.write_mode,
        "note": "runs in the background; watch the server log",
    }


def _chart_payload(conn: Any, chart_id: int, include_pages: bool) -> dict[str, Any]:
    progress = refresh_chart_status(conn, chart_id)
    chart = get_chart(conn, chart_id)
    summary = conn.execute(
        "SELECT * FROM member_verification_summary WHERE chart_id = %s", (chart_id,)
    ).fetchone()
    payload: dict[str, Any] = {
        "chart": chart,
        "progress": progress,
        "member_verification": summary,
    }
    if include_pages:
        payload["pages"] = list_pages(conn, chart_id)
    return payload


@app.post("/api/charts/batch", status_code=202, tags=["charts"])
def batch_intake(
    body: BatchRequest, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Run every chart under a read path, sequentially, and optionally write each.

    Same fields as `/run` without the folder name — each sub-folder holding
    images is one chart and names itself.

    Charts run one at a time on purpose: each already parallelises across pages,
    and stage 5 is billed per page, so overlapping charts multiplies memory and
    spend without finishing sooner. One bad folder does not stop the batch, and
    neither does one unwritable destination.

    Returns 202 immediately — a batch can run for hours. Watch the server log
    for `[n/total]` progress, or poll GET /api/charts/by-name/{chart_name}.
    """
    from jobs.batch_intake import find_local_chart_folders, run_batch

    has_blob = bool(body.blob_container or body.blob_read_path)
    has_local = bool(body.local_read_path)
    if has_blob == has_local:
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide either local_read_path, or both blob_container and "
                "blob_read_path"
            ),
        )
    if has_blob and not (body.blob_container and body.blob_read_path):
        raise HTTPException(
            status_code=400,
            detail="blob_container and blob_read_path must be given together",
        )
    if has_blob and body.local_write_path:
        raise HTTPException(
            status_code=400,
            detail="A blob source writes to blob_write_path, not local_write_path",
        )
    if has_local and body.blob_write_path:
        raise HTTPException(
            status_code=400,
            detail="A local source writes to local_write_path, not blob_write_path",
        )
    if body.write_mode not in WRITE_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"write_mode must be one of {', '.join(WRITE_MODES)}",
        )
    _validate_stages(body)
    _require_db()

    # Resolve the chart list up front so the caller learns immediately that the
    # path is wrong, instead of getting 202 and an empty batch an hour later.
    found: Optional[int] = None
    if has_local:
        try:
            found = len(find_local_chart_folders(body.local_read_path))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not found:
            raise HTTPException(
                status_code=400,
                detail=f"No chart folders with images under {body.local_read_path}",
            )

    background_tasks.add_task(_bg_batch, body)
    write_to = body.blob_write_path or body.local_write_path
    return {
        "status": "accepted",
        "mode": "local" if has_local else "blob",
        "source": body.local_read_path or f"{body.blob_container}/{body.blob_read_path}",
        "charts_found": found,
        "limit": body.limit,
        "through": body.through,
        "only": body.only,
        "write": (
            {
                "destination": (
                    f"{body.blob_container}/{body.blob_write_path}"
                    if body.blob_write_path
                    else body.local_write_path
                ),
                "write_mode": body.write_mode,
                "note": "each chart is written under its own folder name",
            }
            if write_to
            else None
        ),
        "note": "runs sequentially; watch the server log for [n/total] progress",
    }


@app.get("/api/charts/{chart_id}", tags=["charts"])
def get_chart_status(
    chart_id: int,
    include_pages: bool = Query(True, description="Include the page_list rows"),
) -> dict[str, Any]:
    """Chart row, per-stage progress, and the member verification outcome."""
    with connect() as conn:
        if not get_chart(conn, chart_id):
            raise HTTPException(status_code=404, detail="chart not found")
        return _chart_payload(conn, chart_id, include_pages)


@app.get("/api/charts/by-name/{chart_name}", tags=["charts"])
def get_chart_status_by_name(
    chart_name: str,
    include_pages: bool = Query(True),
) -> dict[str, Any]:
    """Same as GET /api/charts/{id} keyed on the folder name.

    Useful right after ingest, when the caller knows the blob folder but not the
    id the database assigned.
    """
    with connect() as conn:
        chart = get_chart_by_name(conn, chart_name)
        if not chart:
            raise HTTPException(status_code=404, detail="chart not found")
        return _chart_payload(conn, chart["id"], include_pages)


@app.post("/api/charts/{chart_id}/rerun", status_code=202, tags=["charts"])
def rerun_chart(
    chart_id: int, body: RerunRequest, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Re-run the chain for a chart.

    By default this *resumes*: pages already completed are not redone, so a
    chart that failed part-way finishes without repeating paid OCR calls. Pass
    force=true to reprocess everything, `through` to stop after a stage, or
    `only` to re-run named stages.
    """
    _validate_stages(body)
    with connect() as conn:
        if not get_chart(conn, chart_id):
            raise HTTPException(status_code=404, detail="chart not found")
    background_tasks.add_task(
        _bg_pipeline, chart_id, body.force, body.only, body.through
    )
    return {
        "status": "accepted",
        "chart_id": chart_id,
        "force": body.force,
        "through": body.through,
        "only": body.only,
    }


@app.post("/api/manifest/sweep", status_code=202, tags=["manifest"])
def manifest_sweep(
    body: ManifestSweepRequest, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Load a batch manifest (local path or blob prefix) with upsert semantics.

    Independent of chart ingest: a manifest can be swept before the charts it
    describes exist. Rows are keyed on record_id and linked to a chart when that
    chart is ingested.
    """
    if not body.local_path and not (body.blob_container and body.blob_prefix):
        raise HTTPException(
            status_code=400,
            detail="Provide local_path, or both blob_container and blob_prefix",
        )
    if body.local_path and (body.blob_container or body.blob_prefix):
        raise HTTPException(
            status_code=400, detail="Pass either local_path or blob_*, not both"
        )
    _require_db()
    background_tasks.add_task(_bg_manifest, body)
    return {
        "status": "accepted",
        "mode": "local" if body.local_path else "blob",
        "local_path": body.local_path,
        "blob_container": body.blob_container,
        "blob_prefix": body.blob_prefix,
        "run_id": body.run_id,
        "batch_id": body.batch_id,
    }


@app.get("/api/manifest/{record_id}", tags=["manifest"])
def get_manifest(record_id: str) -> dict[str, Any]:
    """Manifest rows for one record id (= chart folder name)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM manifest_member_list WHERE record_id = %s ORDER BY id",
            (record_id,),
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="no manifest rows for record")
    return {"record_id": record_id, "members": rows}


@app.get("/api/jobs", tags=["ops"])
def list_jobs(
    chart_id: Optional[int] = Query(None),
    limit: int = Query(50, le=500),
) -> dict[str, Any]:
    """Recent pipeline_jobs rows — the run log for a chart or the whole service."""
    with connect() as conn:
        if chart_id is not None:
            rows = conn.execute(
                """
                SELECT * FROM pipeline_jobs WHERE chart_id = %s
                 ORDER BY id DESC LIMIT %s
                """,
                (chart_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pipeline_jobs ORDER BY id DESC LIMIT %s", (limit,)
            ).fetchall()
    return {"jobs": rows}


def main() -> None:
    import uvicorn

    uvicorn.run("api.main:app", host=API_HOST, port=API_PORT, reload=False)


if __name__ == "__main__":
    main()
