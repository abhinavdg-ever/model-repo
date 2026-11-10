"""FastAPI surface for core-pipeline: ingest, status, rerun (manifests: db-utilities).

Deployed on its own (see core-pipeline/docker-compose.yml). The review UI is a
separate deployment and does not call this service — the two share the Postgres
database and the `outputs/charts` volume, not an HTTP boundary.

Long work runs in a BackgroundTask (RUN_MODE=inline, the default) or on the
``chart-runs`` Service Bus queue, one message per chart, processed by
``python -m apps.worker.queue_worker`` (RUN_MODE=queue). Every mutating endpoint
returns 202; progress is read back from GET /api/charts/{id}, and in queue mode
from GET /api/runs/{run_id}. With API_KEY_REQUIRED=true every POST needs
``X-API-Key: <ORCHESTRATOR_API_KEY>``. See docs/API.md for the full reference.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    # .env is the source of truth for local uvicorn. Without override=True a
    # leftover shell export (e.g. MEMBER_NER_MODEL_ID=gliner_low from the docs)
    # silently wins over the value in the file.
    load_dotenv(ROOT / ".env", override=True)
except ImportError:
    pass

import secrets
import time

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from common.config import (
    API_HOST,
    API_KEY_REQUIRED,
    API_PORT,
    MEMBER_NER_ENABLED,
    ORCHESTRATOR_API_KEY,
    RUN_MODE,
    SERVICEBUS_QUEUE,
    STAGE_WORKERS,
    DB_BACKEND,
    DB_BACKEND_ACTIVE,
)
from common.db import (
    close_pool, connect, create_job, get_chart, get_chart_by_name, get_member_summary,
    list_jobs as db_list_jobs, list_manifest_members, list_pages, list_stages, update_job,
)
from common.db.chart_status import refresh_chart_status
from apps.orchestrator_api.schemas import (  # noqa: F401 - re-exported: tests and callers import them from here
    BatchRequest,
    ChartRunOptions,
    RunRequest,
    WriteRequest,
)
from apps.worker import chart_run, run_stats
from apps.worker.runner import resolve_stage, run_pipeline_for_chart

from common.logging_setup import configure_logging

# Root at INFO for our own per-page progress lines; Azure + Docling quieted to
# WARNING, or they bury [batch#] [chart#] under convert/HTTP spam.
# AZURE_LOG_LEVEL=INFO puts that dump back when debugging.
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

# Methods that change something. GET / HEAD / OPTIONS (/health, /ready, /docs, status)
# stay open even when the key is required.
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _api_key_enforced() -> bool:
    return bool(API_KEY_REQUIRED and ORCHESTRATOR_API_KEY)


@app.middleware("http")
async def _require_api_key(request: Request, call_next):
    """X-API-Key on every mutating route when API_KEY_REQUIRED=true and ORCHESTRATOR_API_KEY is set."""
    if request.method in _MUTATING_METHODS and _api_key_enforced():
        given = request.headers.get("x-api-key") or ""
        if not secrets.compare_digest(given.encode("utf-8"), ORCHESTRATOR_API_KEY.encode("utf-8")):
            return JSONResponse(
                status_code=401,
                content={"detail": "missing or wrong X-API-Key header"},
            )
    return await call_next(request)


# Set once at startup in queue mode (no network on /health): can the API reach the queue?
_QUEUE_STATUS: dict[str, Any] = {}


def _probe_queue() -> dict[str, Any]:
    """Queue reachability, checked once at startup. Never raises; never logs the key."""
    from common import queue as q

    conn_str, queue_name = q.settings()
    status: dict[str, Any] = {"queue": queue_name, "namespace": q.mask(conn_str)}
    if not conn_str:
        return {**status, "reachable": False, "reason": "SERVICEBUS_CONNECTION_STRING not set"}
    try:
        from azure.servicebus.management import ServiceBusAdministrationClient

        admin = ServiceBusAdministrationClient.from_connection_string(conn_str)
        with admin:
            props = admin.get_queue_runtime_properties(queue_name)
        return {
            **status,
            "reachable": True,
            "active_messages_at_start": props.active_message_count,
            "dead_letter_messages_at_start": props.dead_letter_message_count,
        }
    except Exception as exc:  # a status probe must never stop the server
        return {**status, "reachable": False, "reason": str(exc).splitlines()[0][:300]}


def _mirror_failures() -> int:
    from common.db.cosmos_store import mirror_failures

    return mirror_failures()


class _StartupDone(Exception):
    """Ends the startup probe early (the Cosmos backend needs no schema check)."""


def _safe_db_url(url: str) -> str:
    """DATABASE_URL with the password replaced by ***, for logging.

    Worth logging at all because the commonest failure is pointing at the wrong
    database and not knowing it — but the password must never reach a log file.
    """
    import re

    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url or "")


_CODE_VERSION: dict[str, Any] = {}


def _code_version() -> dict[str, Any]:
    """Worked out once (git + a file walk), then reused by /health."""
    if not _CODE_VERSION:
        from common.capabilities import code_version

        _CODE_VERSION.update(code_version())
    return dict(_CODE_VERSION)


@app.on_event("startup")
def _startup() -> None:
    from common.config import DATA_ROOT, DATABASE_URL, STAGE_WORKERS

    logger.info("core-pipeline starting")
    from common.capabilities import code_version_line

    logger.info("pipeline")
    logger.info("  code changed    : %s", code_version_line(_code_version()))
    # `kill -USR1 <pid>` prints every thread's stack to stderr (docker logs):
    # the way to see where a run that looks stuck is waiting.
    import faulthandler
    import signal

    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    # The banner (2026-11-10, user): pipeline change, database, Blob, how charts run, then the
    # models in the order the stages run.
    from common import resources as _resources
    from common.capabilities import all_capabilities, blob_line, probe_blob, startup_lines

    logger.info("database")
    backend_line = {"postgres": "postgres (Postgres only)",
                    "cosmos": "cosmos (Cosmos only)",
                    "both": "both (Postgres, with a copy of every write in Cosmos)"}[DB_BACKEND]
    logger.info("  db backend      : %s", backend_line)
    if DB_BACKEND != "cosmos":
        logger.info("  postgres        : %s", _safe_db_url(DATABASE_URL))
    if DB_BACKEND in ("cosmos", "both"):
        endpoint = (os.environ.get("COSMOS_ENDPOINT") or "").strip() or "COSMOS_ENDPOINT not set"
        logger.info("  cosmos          : %s, database %s", endpoint,
                    (os.environ.get("COSMOS_DATABASE") or "imaging").strip())
    # Short-timeout probe, NOT the pool: the pool retries for 30 seconds, which
    # would hold up startup — and block /docs and /health, the two endpoints
    # whose whole job is to work when the database does not.
    try:
        import psycopg

        if DB_BACKEND_ACTIVE == "cosmos":
            from common.db.cosmos_store import get_cosmos_store

            logger.info("  cosmos check    : container %s reachable", get_cosmos_store().ping())
            raise _StartupDone
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM pipeline_stage")
                count = cur.fetchone()[0]
        logger.info("  schema          : OK, %d stage(s) registered", count)
    except _StartupDone:
        pass
    except Exception as exc:
        logger.warning("  database        : UNREACHABLE — %s", str(exc).splitlines()[0])
        logger.warning(
            "  Mutating endpoints will return 503 until this is fixed. "
            "DATABASE_URL is read once at startup, so restart after editing .env."
        )

    logger.info("blob")
    try:
        logger.info("  storage         : %s", blob_line(probe_blob()))
    except Exception as exc:  # a status probe must never stop the server
        logger.warning("  storage         : probe failed — %s", exc)

    res = _resources.settings()
    logger.info("run")
    logger.info("  run mode        : %s", "queue (Service Bus + workers run the charts)" if RUN_MODE == "queue"
                else "inline (this API runs the charts itself)")
    logger.info("  api key         : %s", "required on POST" if _api_key_enforced() else "not required")
    logger.info("  workers         : %s per stage", STAGE_WORKERS)
    logger.info("  data root       : %s", DATA_ROOT)
    logger.info("  models from     : %s", f"Blob container {res['container']}" if res["enabled"]
                else "disk (copied by hand)")
    logger.info("  canon from      : %s", "Blob" if res["enabled"] and res["canon"] else "git (resources/canon)")
    if RUN_MODE == "queue":
        # Charts run in the workers, which fetch their own models: the API needs none.
        _QUEUE_STATUS.clear()
        _QUEUE_STATUS.update(_probe_queue())
        logger.info(
            "  queue           : %s on %s, %s", _QUEUE_STATUS.get("queue"), _QUEUE_STATUS.get("namespace"),
            "reachable" if _QUEUE_STATUS.get("reachable") else f"UNREACHABLE — {_QUEUE_STATUS.get('reason')}",
        )
    else:
        # Phase 8: models (and optionally canon) from Blob, before anything loads them.
        # Off unless RESOURCES_FROM_BLOB=true; never raises (Blob down = use what is on disk).
        from common.resources import ensure_resources

        ensure_resources()

    # The models, in stage order: OK / off only — no path dumps.
    try:
        caps = all_capabilities()
        logger.info("models (stage order)")
        for label, value in startup_lines(caps):
            logger.info("  %-15s : %s", label, value)
    except Exception as exc:  # a status probe must never stop the server
        logger.warning("  models: probe failed — %s", exc)


@app.on_event("shutdown")
def _shutdown() -> None:
    close_pool()


# --- background wrappers ----------------------------------------------------


# A BackgroundTask runs after the 202 has been sent, so its outcome can only
# ever reach the operator through the log. The run itself lives in
# apps/worker/chart_run.py, shared with the queue worker (RUN_MODE=queue).
_is_credential_failure = chart_run.is_credential_failure
_join = chart_run.join
_write_after_run = chart_run.write_after_run


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


def _bg_pipeline_then_write(
    chart_id: int, chart_name: str, payload: "RunRequest"
) -> None:
    try:
        _bg_pipeline(
            chart_id,
            payload.force,
            payload.only,
            payload.through,
            skip_ocr=payload.skip_ocr,
            redownload_pages=payload.redownload_pages,
        )
        _write_after_run(payload, chart_name)
    finally:
        if getattr(payload, "wants_offline", lambda: False)():
            from common.db import disable_skip_db_write

            disable_skip_db_write()


def _bg_run(payload: "RunRequest") -> None:
    folder = payload.chart_name
    source = f"{payload.blob_container}/{_join(payload.blob_read_path, folder)}"
    logger.info("Background run starting: %s", source)
    try:
        result = chart_run.run_blob_chart(payload)
        logger.info(
            "Background run finished: %s -> chart_id=%s",
            source, (result or {}).get("chart_id"),
        )
    except Exception as exc:
        if _is_credential_failure(exc):
            # The traceback is fifteen frames of SDK plumbing and the message
            # is the same nine-source list already in the log. Neither tells
            # the operator the one thing to do.
            logger.error("Background run FAILED for %s — %s", source, chart_run.CREDENTIAL_HINT)
        else:
            logger.exception("Background run FAILED for %s", source)


def _bg_pipeline(
    chart_id: int,
    force: bool,
    only: Optional[list[str]],
    through: Optional[str] = None,
    skip_ocr: Optional[bool] = None,
    redownload_pages: bool = False,
) -> None:
    logger.info(
        "Background pipeline starting: chart_id=%s force=%s only=%s through=%s "
        "skip_ocr=%s redownload_pages=%s",
        chart_id,
        force,
        only or "all stages",
        through or "end of chain",
        skip_ocr if skip_ocr is not None else "env",
        redownload_pages,
    )
    try:
        run_pipeline_for_chart(
            chart_id,
            force=force,
            only=only,
            through=through,
            skip_ocr=skip_ocr,
            redownload_pages=redownload_pages,
        )
        logger.info("Background pipeline finished: chart_id=%s", chart_id)
    except Exception:
        logger.exception("Background pipeline FAILED for chart %s", chart_id)


def _bg_batch(payload: "BatchRequest") -> None:
    from apps.worker.jobs.batch_intake import run_batch

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
            skip_ocr=payload.skip_ocr,
            redownload_pages=payload.redownload_pages,
            sample=payload.sample,
            chart_names=payload.chart_list,
            skip_completed=payload.skip_completed,
            only_completed=payload.only_completed,
            min_run_date=payload.min_run_date,
        )
        logger.info(
            "Background batch finished: %s -> %d/%d completed, %d failed "
            "(workers=%s) in %.1fs",
            source, result["completed"], result["charts_found"],
            result["failed"], result.get("workers"), result["duration_seconds"],
        )
        for chart in result["charts"]:
            if chart["status"] == "failed":
                logger.warning("  failed: %s — %s", chart["name"], chart.get("error"))
    except Exception:
        logger.exception("Background batch FAILED: %s", source)


def _require_db() -> None:
    """Fail fast if Postgres is unreachable, instead of accepting work we cannot do.

    The mutating endpoints return 202 and hand off to a BackgroundTask. Without
    this check a bad DATABASE_URL produced a cheerful 202, then a PoolTimeout 30
    seconds later that only ever appeared in the server log — so the caller saw
    "accepted" and no rows, with nothing connecting the two. A short-timeout
    probe turns that into an immediate 503 naming the real error.
    """
    import psycopg

    from common.config import DATABASE_URL

    try:
        if DB_BACKEND_ACTIVE == "cosmos":
            from common.db.cosmos_store import get_cosmos_store

            get_cosmos_store().ping()
            return
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
        from common.capabilities import all_capabilities

        caps = all_capabilities()
    except Exception as exc:  # never let a probe fail on an optional feature
        caps = {"member_ner": {"enabled": MEMBER_NER_ENABLED, "ready": False,
                               "reason": str(exc)}}

    return {
        "status": "ok",
        # member_ner.ready=false means member verification runs rules-only: no
        # page can be marked wrong_member, so no document can be Rejected.
        # blob.ready=false means run/batch-run work locally but not from blob.
        **caps,
        # configured = DB_BACKEND in .env; active = what runs (Postgres until Cosmos is wired).
        "db_backend": {"configured": DB_BACKEND, "active": DB_BACKEND_ACTIVE,
                       **({"cosmos_copy_failures": _mirror_failures()} if DB_BACKEND == "both" else {})},
        "stage_workers": STAGE_WORKERS,
        # inline = charts run in this process; queue = on Service Bus, run by the workers.
        # queue.reachable was checked once at startup (restart the API to re-check).
        "run_mode": RUN_MODE,
        "code": _code_version(),
        **({"queue": dict(_QUEUE_STATUS)} if RUN_MODE == "queue" else {}),
        "api_key_required": _api_key_enforced(),
    }


@app.get("/ready", tags=["ops"])
def ready() -> dict[str, Any]:
    """Readiness — verifies the database is reachable and schema v7 is applied."""
    try:
        if DB_BACKEND_ACTIVE == "cosmos":
            from common.db.cosmos_store import get_cosmos_store

            get_cosmos_store().ping()
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


def _validate_stages(body: "ChartRunOptions") -> None:
    """Reject an unknown stage name with a 400 naming the known ones.

    Without this a typo reaches the background task, where it becomes a log
    line the caller never sees — the run just does nothing.
    """
    for token in ([body.through] if body.through else []) + list(body.only or []):
        try:
            resolve_stage(token)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _check_source(body: ChartRunOptions) -> None:
    """Shape rules shared by run and batch-run (400 with a readable detail)."""
    if body.is_blob and not body.container_name:
        raise HTTPException(
            status_code=400, detail="input_type=blob needs container_name"
        )
    if not body.is_blob and body.container_name:
        raise HTTPException(
            status_code=400,
            detail="container_name is for input_type=blob only; remove it for local",
        )
    if not body.input_path:
        raise HTTPException(status_code=400, detail="input_path is required")
    _validate_stages(body)


def _chart_finished(chart_name: str) -> Optional[str]:
    """The chart's status when it is already finished, else None."""
    from apps.worker.jobs.batch_intake import FINISHED_CHART_STATUSES

    with connect() as conn:
        chart = get_chart_by_name(conn, chart_name)
    status = str((chart or {}).get("status") or "")
    return status if status in FINISHED_CHART_STATUSES else None


def _options_echo(body: ChartRunOptions) -> dict[str, Any]:
    return {
        "only": body.only,
        "run_through": body.run_through,
        "skip_ocr": body.skip_ocr,
        "skip_completed": body.skip_completed,
        "skip_page_download": body.skip_page_download,
    }


# --- queue mode (RUN_MODE=queue) ----------------------------------------------


def _chart_source(req: RunRequest) -> str:
    """Where the chart is read from, as batch_intake._pre_register expects it."""
    if req.is_blob:
        return _join(req.input_path, req.chart_name)
    return str(Path(req.input_path) / req.chart_name)


def _enqueue_charts(
    requests: list[RunRequest],
    trigger_id: str,
    *,
    min_run_date: Optional[str] = None,
    job_stage: Optional[str] = None,
    target: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Register each chart, record a ``queued_run`` job per chart, send one message each.

    The job id is the ``run_id`` (GET /api/runs/{run_id}). A send failure marks the
    jobs failed and is a 503: nothing was queued, so the caller should retry.
    ``job_stage`` / ``target``: the /v1 step APIs (steps.py) record ``v1_<target>`` jobs
    and put ``target`` in the message; the worker reads neither.
    """
    from apps.worker.jobs.batch_intake import _pre_register
    from common import queue as q
    from common.db.path_ids import resolve_run_batch

    if not requests:
        return []
    first = requests[0]
    run_label, batch_label = resolve_run_batch(
        None, None, first.blob_read_path, first.local_read_path
    )
    registered = _pre_register(
        [(_chart_source(r), r.chart_name, r.input_type) for r in requests],
        blob_container=first.blob_container,
        run_id=run_label,
        batch_id=batch_label,
        blob_write_path=first.blob_write_path,
        local_write_path=first.local_write_path,
    )
    chart_ids = {row["chart_name"]: row["chart_id"] for row in registered}

    queued: list[dict[str, Any]] = []
    with connect() as conn:
        for req in requests:
            chart_id = chart_ids.get(req.chart_name)
            run_id = create_job(
                conn, chart_id=chart_id, stage_name=job_stage or q.JOB_STAGE, status="queued",
                queue_name=SERVICEBUS_QUEUE,
            )
            queued.append({"run_id": run_id, "chart_id": chart_id, "chart_name": req.chart_name, "req": req})

    messages = [
        (item["chart_name"], q.run_message(
            item["run_id"], item["req"].model_dump(mode="json"), trigger_id, min_run_date=min_run_date
        ))
        for item in queued
    ]
    if target:
        for _sid, body in messages:
            body["target"] = target
    try:
        q.send(messages)
    except Exception as exc:
        error = f"could not queue on {SERVICEBUS_QUEUE}: {type(exc).__name__}: {str(exc).splitlines()[0][:300]}"
        with connect() as conn:
            for item in queued:
                update_job(conn, item["run_id"], status="failed", completed=True, error_message=error)
        logger.error(error)
        raise HTTPException(status_code=503, detail=error) from exc
    return [{k: v for k, v in item.items() if k != "req"} for item in queued]


def _batch_sources(body: BatchRequest) -> dict[str, Any]:
    """The charts a batch-run would run: listing + chart_list + skip/only_completed + sample.

    Same filters as batch_intake.run_batch, without running anything. Raises ValueError
    with a readable message when nothing matches.
    """
    from apps.worker.jobs.batch_intake import (
        filter_sources_by_chart_names,
        find_local_chart_folders,
        finished_chart_names,
        select_batch_sources,
    )

    if body.is_blob:
        from common.db.blob_store import chart_name_from_blob_path, ensure_blob_ready, list_chart_prefixes

        ensure_blob_ready(body.blob_container)
        prefixes = list_chart_prefixes(body.blob_container, body.blob_read_path)
        sources = [(p, chart_name_from_blob_path(p), "blob") for p in prefixes]
    else:
        sources = [(str(f), f.name, "local") for f in find_local_chart_folders(body.input_path)]
    found = len(sources)
    if not found:
        raise ValueError(f"No chart folders with images under {body.input_path}")
    missing: list[str] = []
    if body.chart_list:
        sources, missing = filter_sources_by_chart_names(sources, body.chart_list)
        if not sources:
            raise ValueError(
                f"None of chart_list exists under {body.input_path}"
                + (f" (missing: {', '.join(missing[:20])})" if missing else "")
            )
    skipped: list[str] = []
    if body.skip_completed:
        finished = finished_chart_names([n for _s, n, _m in sources], body.min_run_date)
        skipped = [n for _s, n, _m in sources if n in finished]
        sources = [x for x in sources if x[1] not in finished]
    elif body.only_completed:
        finished = finished_chart_names([n for _s, n, _m in sources], body.min_run_date)
        sources = [x for x in sources if x[1] in finished]
    sources = select_batch_sources(sources, body.charts_cap())

    shared = body.model_dump(
        mode="json", include=set(ChartRunOptions.model_fields)
    )
    requests = []
    for src, name, mode in sources:
        # Blob prefixes are direct children of input_path; a local root may itself be the chart.
        parent = body.input_path if mode == "blob" else str(Path(src).parent)
        requests.append(RunRequest.model_validate({**shared, "input_path": parent, "chart_name": name}))
    return {"requests": requests, "found": found, "missing": missing, "skipped": skipped}


def _bg_enqueue_batch(body: BatchRequest, trigger_id: str) -> None:
    """Blob batch in queue mode: list in the background (as inline does), then queue."""
    source = f"{body.blob_container}/{body.blob_read_path}"
    try:
        listed = _batch_sources(body)
        queued = _enqueue_charts(
            listed["requests"], trigger_id,
            min_run_date=body.min_run_date.isoformat() if body.min_run_date else None,
        )
        logger.info(
            "Batch %s: %d chart(s) found, %d queued on %s (missing %d, skipped as finished %d)",
            source, listed["found"], len(queued), SERVICEBUS_QUEUE,
            len(listed["missing"]), len(listed["skipped"]),
        )
    except Exception as exc:
        if _is_credential_failure(exc):
            logger.error("Batch listing FAILED for %s — %s", source, chart_run.CREDENTIAL_HINT)
        else:
            logger.exception("Batch queueing FAILED for %s", source)


@app.post("/api/charts/run", status_code=202, tags=["charts"])
def run_chart(
    body: RunRequest, background_tasks: BackgroundTasks, response: Response
) -> dict[str, Any]:
    """One chart: ``<input_path>/<chart_name>`` → pipeline → ``<output_path>/<chart_name>``.

    Re-running a chart resumes it: workspace pages are reused and every stage
    reprocesses. With ``skip_completed`` a finished chart is not run (200).

    Returns immediately (202). Poll GET /api/charts/by-name/{chart_name}.
    """
    _check_source(body)
    _require_db()
    folder = body.chart_name

    if body.skip_completed:
        finished = _chart_finished(folder)
        if finished:
            response.status_code = 200
            return {
                "status": "skipped",
                "reason": f"skip_completed: chart status is {finished}",
                "chart_name": folder,
                "poll": f"/api/charts/by-name/{folder}",
            }

    write_to = body.output_path
    trigger = (run_stats.new_trigger("run"), "run")
    base = {
        "status": "accepted",
        "input_type": body.input_type,
        "chart_name": folder,
        **_options_echo(body),
        "write": _write_summary(body, folder, write_to),
    }

    if RUN_MODE == "queue":
        source = (
            f"{body.container_name}/{_join(body.input_path, folder)}"
            if body.is_blob else str(Path(body.input_path) / folder)
        )
        if not body.is_blob and not Path(source).is_dir():
            raise HTTPException(status_code=400, detail=f"Not a directory: {source}")
        queued = _enqueue_charts([body], trigger[0])[0]
        return {
            **base,
            "status": "queued",
            "trigger_id": trigger[0],
            "run_id": queued["run_id"],
            "chart_id": queued["chart_id"],
            "source": source,
            "poll": f"/api/runs/{queued['run_id']}",
        }

    if not body.is_blob:
        from apps.worker.stages.utilities.download_blob import import_local_folder

        source = str(Path(body.input_path) / folder)
        try:
            result = import_local_folder(
                source,
                chart_name=folder,
                force=body.force,
                redownload_pages=body.redownload_pages,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        background_tasks.add_task(
            run_stats.in_trigger, trigger, _bg_pipeline_then_write, result["chart_id"], folder, body
        )
        return {
            **base,
            "trigger_id": trigger[0],
            "chart_id": result["chart_id"],
            "source": source,
            "imported": result["imported"],
            "page_count": result["page_count"],
            "poll": f"/api/charts/{result['chart_id']}",
        }

    background_tasks.add_task(run_stats.in_trigger, trigger, _bg_run, body)
    return {
        **base,
        "trigger_id": trigger[0],
        "source": f"{body.container_name}/{_join(body.input_path, folder)}",
        "poll": f"/api/charts/by-name/{folder}",
    }


@app.post(
    "/api/charts/write",
    status_code=410,
    tags=["charts"],
    deprecated=True,
    include_in_schema=False,
)
def write_chart_out(body: WriteRequest) -> dict[str, Any]:
    """Removed — write is part of ``POST /api/charts/run`` and ``/batch-run``.

    Pass ``local_write_path`` or ``blob_write_path`` on run/batch-run. Existing
    destination files are skipped; missing ones are written (``overwrite=true``
    replaces all).
    """
    raise HTTPException(
        status_code=410,
        detail=(
            "POST /api/charts/write was removed. Pass local_write_path or "
            "blob_write_path on POST /api/charts/run (or /batch-run). "
            f"Example resume+write: "
            f'{{"chart_name":"{body.chart_name}","local_write_path":"..."}}'
        ),
    )


@app.post("/api/charts/batch-run", status_code=202, tags=["charts"])
@app.post(
    "/api/charts/batch",
    status_code=202,
    tags=["charts"],
    deprecated=True,
    include_in_schema=False,
)
def batch_run(
    body: BatchRequest, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Every chart folder under a path: register, run, write if a path is set.

    Canonical path: ``POST /api/charts/batch-run``. ``/batch`` is a deprecated
    alias (not listed in /docs). Write is part of this call when
    ``local_write_path`` / ``blob_write_path`` is set (sync: missing files
    written, existing skipped).
    """
    return _batch_run_impl(body, background_tasks)


def _queue_batch(body: BatchRequest, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """batch-run in queue mode: one message per chart. Local drops are listed and queued
    now (the reply has the count and the run ids); blob drops in the background."""
    _require_db()
    trigger_id = run_stats.new_trigger("batch-run")
    source = f"{body.container_name}/{body.input_path}" if body.is_blob else body.input_path
    reply: dict[str, Any] = {
        "status": "queued",
        "trigger_id": trigger_id,
        "input_type": body.input_type,
        "source": source,
        "queue": SERVICEBUS_QUEUE,
        "chart_list": body.chart_list,
        "sample": body.sample,
        "only_completed": body.only_completed,
        "min_run_date": body.min_run_date.isoformat() if body.min_run_date else None,
        **_options_echo(body),
    }
    if body.is_blob:
        background_tasks.add_task(run_stats.in_trigger, (trigger_id, "batch-run"), _bg_enqueue_batch, body, trigger_id)
        return {
            **reply,
            "status": "accepted",
            "charts_found": None,
            "charts_queued": None,
            "note": "blob drops are listed in the background; GET /api/jobs shows the queued_run rows",
            "poll": "/api/jobs",
        }
    try:
        listed = _batch_sources(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    queued = _enqueue_charts(
        listed["requests"], trigger_id,
        min_run_date=body.min_run_date.isoformat() if body.min_run_date else None,
    )
    return {
        **reply,
        "charts_found": listed["found"],
        "charts_queued": len(queued),
        "charts_missing": listed["missing"] or None,
        "charts_skipped_completed": listed["skipped"] if body.skip_completed else None,
        "runs": queued,
        "poll": "/api/runs/{run_id}",
    }


def _batch_run_impl(
    body: BatchRequest, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Register every chart under input_path, then run them with a worker pool."""
    from apps.worker.jobs.batch_intake import (
        filter_sources_by_chart_names,
        find_local_chart_folders,
        finished_chart_names,
        resolve_batch_workers,
    )

    _check_source(body)
    if RUN_MODE == "queue":
        return _queue_batch(body, background_tasks)
    try:
        workers = resolve_batch_workers(None)  # BATCH_WORKERS, checked against DB_POOL_MAX
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _require_db()

    found: Optional[int] = None
    queued: Optional[int] = None
    missing: Optional[list[str]] = None
    skipped: Optional[list[str]] = None
    cap = body.charts_cap()
    if not body.is_blob:
        # Local drops can be counted up front; blob is listed in the background.
        try:
            folders = find_local_chart_folders(body.input_path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        sources = [(str(f), f.name, "local") for f in folders]
        found = len(sources)
        if not found:
            raise HTTPException(
                status_code=400,
                detail=f"No chart folders with images under {body.input_path}",
            )
        if body.chart_list:
            sources, missing = filter_sources_by_chart_names(sources, body.chart_list)
            if not sources:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"None of chart_list exists under {body.input_path}"
                        + (f" (missing: {', '.join(missing[:20])})" if missing else "")
                    ),
                )
        if body.skip_completed:
            finished = finished_chart_names([n for _s, n, _m in sources], body.min_run_date)
            skipped = [n for _s, n, _m in sources if n in finished]
            sources = [x for x in sources if x[1] not in finished]
        elif body.only_completed:
            finished = finished_chart_names([n for _s, n, _m in sources], body.min_run_date)
            sources = [x for x in sources if x[1] in finished]
        queued = min(len(sources), cap) if cap else len(sources)

    trigger = (run_stats.new_trigger("batch-run"), "batch-run")
    background_tasks.add_task(run_stats.in_trigger, trigger, _bg_batch, body)
    source = (
        f"{body.container_name}/{body.input_path}" if body.is_blob else body.input_path
    )
    return {
        "status": "accepted",
        "trigger_id": trigger[0],
        "input_type": body.input_type,
        "source": source,
        "charts_found": found,
        "charts_queued": queued,
        "charts_missing": missing,
        "charts_skipped_completed": skipped,
        "chart_list": body.chart_list,
        "sample": body.sample,
        "only_completed": body.only_completed,
        "min_run_date": body.min_run_date.isoformat() if body.min_run_date else None,
        "workers": workers,
        **_options_echo(body),
        "write": (
            {
                "destination": (
                    f"{body.container_name}/{body.output_path}"
                    if body.is_blob
                    else body.output_path
                ),
                "note": "each chart is written under its own folder name, replacing existing files",
            }
            if body.output_path
            else None
        ),
        "poll": "/api/charts/by-name/{chart_name}",
    }


def _chart_payload(conn: Any, chart_id: int, include_pages: bool) -> dict[str, Any]:
    progress = refresh_chart_status(conn, chart_id)
    chart = get_chart(conn, chart_id)
    summary = get_member_summary(conn, chart_id)
    payload: dict[str, Any] = {
        "chart": chart,
        "progress": progress,
        "member_verification": summary,
    }
    if include_pages:
        payload["pages"] = list_pages(conn, chart_id)
    return payload


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


@app.post(
    "/api/charts/{chart_id}/rerun",
    status_code=410,
    tags=["charts"],
    deprecated=True,
    include_in_schema=False,
)
def rerun_chart(chart_id: int) -> dict[str, Any]:
    """Removed — resume via ``POST /api/charts/run`` with ``chart_id``."""
    raise HTTPException(
        status_code=410,
        detail=(
            "POST /api/charts/{id}/rerun was removed. Resume with "
            f'POST /api/charts/run and {{"chart_id":{chart_id}}} '
            "(optional: through, only, force, skip_ocr, local_write_path / "
            "blob_write_path)."
        ),
    )


@app.get("/api/manifest/{record_id}", tags=["manifest"])
def get_manifest(record_id: str) -> dict[str, Any]:
    """Manifest rows for one record id (= chart folder name), from manifest_member_list.
    Manifests are loaded by db-utilities (2026-11-07), not by core-pipeline."""
    rid = (record_id or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="record_id is required")

    try:
        with connect() as conn:
            rows = list_manifest_members(conn, record_id=rid)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"no manifest rows for record_id={rid!r} (load them with db-utilities)",
        )
    return {"record_id": rid, "source": "postgres", "members": rows}


_RUN_DONE = frozenset({"completed", "failed"})
_RUN_POLL_SECONDS = 2.0


def _get_job(conn: Any, run_id: int) -> Optional[dict[str, Any]]:
    """One pipeline_jobs row by id (common.db.get_job)."""
    from common.db import get_job

    row = get_job(conn, int(run_id))
    return dict(row) if row else None


@app.get("/api/runs/{run_id}", tags=["charts"])
def get_run(
    run_id: int,
    wait: int = Query(0, ge=0, le=60, description="Wait up to N seconds (max 60) for the run to finish"),
) -> dict[str, Any]:
    """A queued run (RUN_MODE=queue): its pipeline_jobs row and the chart's status.

    ``status`` is queued → running → completed | failed. A failed run is delivered
    again by Service Bus (up to 5 times), so it can go back to running.
    """
    deadline = time.monotonic() + wait
    while True:
        with connect() as conn:
            job = _get_job(conn, run_id)
            if not job:
                raise HTTPException(status_code=404, detail="run not found")
            done = job.get("status") in _RUN_DONE
            if done or time.monotonic() >= deadline:
                chart = (
                    _chart_payload(conn, int(job["chart_id"]), include_pages=False)
                    if job.get("chart_id") and get_chart(conn, int(job["chart_id"])) else None
                )
                return {"run_id": run_id, "status": job.get("status"), "done": done, "run": job, "chart": chart}
        time.sleep(min(_RUN_POLL_SECONDS, max(0.0, deadline - time.monotonic())))


@app.get("/api/jobs", tags=["ops"])
def list_jobs(
    chart_id: Optional[int] = Query(None),
    limit: int = Query(50, le=500),
) -> dict[str, Any]:
    """Recent pipeline_jobs rows — the run log for a chart or the whole service."""
    with connect() as conn:
        rows = db_list_jobs(conn, chart_id=chart_id, limit=limit)
    return {"jobs": rows}


# Phase 5: the /v1 step-by-step chart APIs (apps/orchestrator_api/steps.py).
from apps.orchestrator_api.steps import router as _steps_router  # noqa: E402

app.include_router(_steps_router)


def main() -> None:
    import uvicorn

    uvicorn.run("apps.orchestrator_api.main:app", host=API_HOST, port=API_PORT, reload=False)


if __name__ == "__main__":
    main()
