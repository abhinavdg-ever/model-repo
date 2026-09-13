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
from orchestrator.runner import STAGE_NAMES, ingest_and_run, run_pipeline_for_chart
from stages.download_blob import import_local_folder, register_local_pages

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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


@app.on_event("shutdown")
def _shutdown() -> None:
    close_pool()


# --- request models ---------------------------------------------------------


class IngestRequest(BaseModel):
    """One chart, from either source. Give blob_container + blob_path, OR local_path.

    The two modes converge: both end with the page images under
    data/folders/<chart>/pages/ named 1.jpg, 2.jpg … and the chart registered,
    so every later stage is identical regardless of where the pages came from.
    """

    # --- blob mode ---
    blob_container: Optional[str] = Field(
        None, description="Azure Blob container name. With blob_path."
    )
    blob_path: Optional[str] = Field(
        None, description="Prefix of the chart folder holding the page images"
    )

    # --- local mode ---
    local_path: Optional[str] = Field(
        None,
        description=(
            "A directory ON THE SERVER holding the page images. Under Docker "
            "this must be a path inside the container, so mount the folder "
            "first — the host filesystem is not visible."
        ),
        examples=["/data/inbox/52743839_44976074"],
    )
    chart_name: Optional[str] = Field(
        None,
        description="Local mode only. Defaults to the source folder's own name.",
    )
    move: bool = Field(
        False,
        description="Local mode only. Default copies, leaving your folder intact.",
    )
    recursive: bool = Field(
        False, description="Local mode only. Also pick up images in subfolders."
    )
    load_manifest: bool = Field(
        True, description="Local mode only. Load any CSV/XLSX found in the folder."
    )

    # --- both ---
    run_id: Optional[str] = None
    batch_id: Optional[str] = None
    run_pipeline: bool = Field(True, description="Run the stage chain after intake")
    force: bool = Field(
        False,
        description="Reprocess pages already completed. Default resumes instead.",
    )


class LocalRegisterRequest(BaseModel):
    chart_name: str = Field(
        ..., description="Folder name under data/folders that already holds pages/"
    )
    run_id: Optional[str] = None
    batch_id: Optional[str] = None
    run_pipeline: bool = True
    force: bool = False


class BatchRequest(BaseModel):
    """Scan a folder or blob prefix and run every chart found, one at a time."""

    local_root: Optional[str] = Field(
        None,
        description=(
            "Parent directory ON THE SERVER; each subfolder holding images is "
            "one chart. Under Docker this must be a path inside the container."
        ),
    )
    blob_container: Optional[str] = None
    blob_prefix: Optional[str] = Field(
        None, description="Each sub-folder under this prefix holding images is one chart"
    )
    move: bool = Field(False, description="Local only: move instead of copy")
    force: bool = False
    load_manifest: bool = True
    run_pipeline: bool = True
    limit: Optional[int] = Field(
        None, description="Only the first N charts — use for a dry run first"
    )
    run_id: Optional[str] = None
    batch_id: Optional[str] = None


class RerunRequest(BaseModel):
    force: bool = Field(
        False, description="Reprocess completed pages instead of resuming"
    )
    only: Optional[list[str]] = Field(
        None,
        description=(
            "Restrict to these stages. Use 'name' for pass 1 or 'name:2' for "
            f"pass 2. Known: {', '.join(STAGE_NAMES)}"
        ),
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
def _bg_ingest(payload: IngestRequest) -> None:
    logger.info("Background ingest starting: %s", payload.blob_path)
    try:
        result = ingest_and_run(
            blob_container=payload.blob_container,
            blob_path=payload.blob_path,
            run_id=payload.run_id,
            batch_id=payload.batch_id,
            run_pipeline=payload.run_pipeline,
            force=payload.force,
        )
        logger.info(
            "Background ingest finished: %s -> chart_id=%s",
            payload.blob_path, (result or {}).get("chart_id"),
        )
    except Exception:
        logger.exception("Background ingest FAILED for %s", payload.blob_path)


def _bg_pipeline(chart_id: int, force: bool, only: Optional[list[str]]) -> None:
    logger.info(
        "Background pipeline starting: chart_id=%s force=%s only=%s",
        chart_id, force, only or "all stages",
    )
    try:
        run_pipeline_for_chart(chart_id, force=force, only=only)
        logger.info("Background pipeline finished: chart_id=%s", chart_id)
    except Exception:
        logger.exception("Background pipeline FAILED for chart %s", chart_id)


def _bg_batch(payload: "BatchRequest") -> None:
    from jobs.batch_intake import run_batch

    source = payload.local_root or f"{payload.blob_container}/{payload.blob_prefix}"
    logger.info("Background batch starting: %s", source)
    try:
        result = run_batch(
            local_root=payload.local_root,
            blob_container=payload.blob_container,
            blob_prefix=payload.blob_prefix,
            move=payload.move,
            force=payload.force,
            load_manifest=payload.load_manifest,
            run_pipeline=payload.run_pipeline,
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
    try:
        import sys as _sys

        _lib = str(ROOT / "stages" / "lib")
        if _lib not in _sys.path:
            _sys.path.insert(0, _lib)
        from member import ner_status

        ner = ner_status()
    except Exception as exc:  # never let a probe fail on an optional feature
        ner = {"enabled": MEMBER_NER_ENABLED, "ready": False, "reason": str(exc)}

    return {
        "status": "ok",
        # ready=false means member verification runs rules-only: no page can be
        # marked wrong_member, so no document can be Rejected.
        "member_ner": ner,
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


@app.post("/api/charts/ingest", status_code=202, tags=["charts"])
def ingest_chart(
    body: IngestRequest, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Ingest one chart from blob storage OR from a local folder, then run it.

    Pass either `blob_container` + `blob_path`, or `local_path` — not both.
    Both paths end identically: pages under data/folders/<chart>/pages/ named
    1.jpg, 2.jpg … with the chart registered.

    Returns immediately. Poll GET /api/charts/by-name/{chart_name}.
    """
    has_blob = bool(body.blob_container or body.blob_path)
    has_local = bool(body.local_path)
    if has_blob and has_local:
        raise HTTPException(
            status_code=400,
            detail="Pass either blob_container + blob_path, or local_path, not both",
        )
    if not has_blob and not has_local:
        raise HTTPException(
            status_code=400,
            detail="Provide blob_container + blob_path, or local_path",
        )
    if has_blob and not (body.blob_container and body.blob_path):
        raise HTTPException(
            status_code=400,
            detail="blob_container and blob_path must be given together",
        )
    _require_db()

    if has_local:
        # Resolve up front: a bad path should be a 400 now, not a background
        # failure the caller never sees.
        try:
            result = import_local_folder(
                body.local_path,
                chart_name=body.chart_name,
                move=body.move,
                recursive=body.recursive,
                force=body.force,
                load_manifest=body.load_manifest,
                run_id=body.run_id,
                batch_id=body.batch_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if body.run_pipeline:
            background_tasks.add_task(_bg_pipeline, result["chart_id"], body.force, None)
        return {
            "status": "accepted",
            "mode": "local",
            "chart_id": result["chart_id"],
            "chart_name": result["chart_name"],
            "source": result["source"],
            "imported": result["imported"],
            "moved": result["moved"],
            "manifest": result["manifest"],
            "page_count": result["page_count"],
            "poll": f"/api/charts/{result['chart_id']}",
        }

    from db.blob_store import chart_name_from_blob_path

    chart_name = chart_name_from_blob_path(body.blob_path)
    background_tasks.add_task(_bg_ingest, body)
    return {
        "status": "accepted",
        "mode": "blob",
        "chart_name": chart_name,
        "blob_container": body.blob_container,
        "blob_path": body.blob_path,
        "poll": f"/api/charts/by-name/{chart_name}",
    }


@app.post("/api/charts/register-local", status_code=202, tags=["charts"])
def register_local(
    body: LocalRegisterRequest, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Register a chart folder already present under data/folders and run it."""
    _require_db()
    try:
        result = register_local_pages(
            body.chart_name, run_id=body.run_id, batch_id=body.batch_id
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if body.run_pipeline:
        background_tasks.add_task(_bg_pipeline, result["chart_id"], body.force, None)
    return {
        "status": "accepted",
        "chart_id": result["chart_id"],
        "chart_name": result["chart_name"],
        "page_count": result["page_count"],
        "manifest_rows": result["manifest_rows"],
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
    """Run every chart under a folder or blob prefix, sequentially.

    Charts run one at a time on purpose: each already parallelises across pages,
    and stage 5 is billed per page, so overlapping charts multiplies memory and
    spend without finishing sooner. One bad folder does not stop the batch.

    Returns 202 immediately — a batch can run for hours. Watch the server log
    for `[n/total]` progress, or poll GET /api/charts/by-name/{chart_name}.
    """
    from jobs.batch_intake import find_local_chart_folders, run_batch

    if bool(body.local_root) == bool(body.blob_container or body.blob_prefix):
        raise HTTPException(
            status_code=400,
            detail="Provide either local_root, or both blob_container and blob_prefix",
        )
    _require_db()
    # Resolve the chart list up front so the caller learns immediately that the
    # path is wrong, instead of getting 202 and an empty batch an hour later.
    found: Optional[int] = None
    if body.local_root:
        try:
            found = len(find_local_chart_folders(body.local_root))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not found:
            raise HTTPException(
                status_code=400,
                detail=f"No chart folders with images under {body.local_root}",
            )

    background_tasks.add_task(
        _bg_batch,
        body,
    )
    return {
        "status": "accepted",
        "mode": "local" if body.local_root else "blob",
        "source": body.local_root or f"{body.blob_container}/{body.blob_prefix}",
        "charts_found": found,
        "limit": body.limit,
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
    force=true to reprocess everything, or `only` to re-run named stages.
    """
    if body.only:
        unknown = [
            s for s in body.only
            if s not in STAGE_NAMES and s not in {n.split(":")[0] for n in STAGE_NAMES}
        ]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown stage(s) {unknown}; known: {STAGE_NAMES}",
            )
    with connect() as conn:
        if not get_chart(conn, chart_id):
            raise HTTPException(status_code=404, detail="chart not found")
    background_tasks.add_task(_bg_pipeline, chart_id, body.force, body.only)
    return {
        "status": "accepted",
        "chart_id": chart_id,
        "force": body.force,
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
