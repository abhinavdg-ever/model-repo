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


@app.on_event("shutdown")
def _shutdown() -> None:
    close_pool()


# --- request models ---------------------------------------------------------


class IngestRequest(BaseModel):
    blob_container: str = Field(..., description="Azure Blob container name")
    blob_path: str = Field(
        ..., description="Prefix of the chart folder holding the page images"
    )
    run_id: Optional[str] = None
    batch_id: Optional[str] = None
    run_pipeline: bool = Field(True, description="Run the stage chain after download")
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


class ImportFolderRequest(BaseModel):
    source_path: str = Field(
        ...,
        description=(
            "Any directory ON THE SERVER holding page images. Under Docker this "
            "must be a path inside the container, so the folder has to be "
            "mounted first — the host's filesystem is not visible."
        ),
        examples=["/data/inbox/52743839_44976074"],
    )
    chart_name: Optional[str] = Field(
        None, description="Defaults to the source folder's own name"
    )
    move: bool = Field(
        False, description="Move instead of copy. Default copies, leaving the source intact"
    )
    recursive: bool = Field(False, description="Also pick up images in subfolders")
    force: bool = Field(
        False, description="Replace pages already in the workspace for this chart"
    )
    load_manifest: bool = Field(
        True,
        description=(
            "Load any CSV/XLSX manifest found in the source folder before "
            "registering, so the chart links to its member row"
        ),
    )
    run_id: Optional[str] = None
    batch_id: Optional[str] = None
    run_pipeline: bool = True


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


def _bg_ingest(payload: IngestRequest) -> None:
    try:
        ingest_and_run(
            blob_container=payload.blob_container,
            blob_path=payload.blob_path,
            run_id=payload.run_id,
            batch_id=payload.batch_id,
            run_pipeline=payload.run_pipeline,
            force=payload.force,
        )
    except Exception:
        logger.exception("Background ingest failed for %s", payload.blob_path)


def _bg_pipeline(chart_id: int, force: bool, only: Optional[list[str]]) -> None:
    try:
        run_pipeline_for_chart(chart_id, force=force, only=only)
    except Exception:
        logger.exception("Background pipeline failed for chart %s", chart_id)


def _bg_manifest(payload: ManifestSweepRequest) -> None:
    try:
        run_load(
            local_path=payload.local_path,
            blob_container=payload.blob_container,
            blob_prefix=payload.blob_prefix,
            run_id=payload.run_id,
            batch_id=payload.batch_id,
            mirror_local=payload.mirror_local,
        )
    except Exception:
        logger.exception("Background manifest sweep failed")


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
    """Download a chart folder from blob and run the pipeline.

    Returns immediately. Poll GET /api/charts/{chart_id} for progress; the
    chart_id is derivable from the folder name, which is also returned here.
    """
    from db.blob_store import chart_name_from_blob_path

    chart_name = chart_name_from_blob_path(body.blob_path)
    background_tasks.add_task(_bg_ingest, body)
    return {
        "status": "accepted",
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
        "manifest_rows_linked": result["manifest_rows_linked"],
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


@app.post("/api/charts/import-local", status_code=202, tags=["charts"])
def import_local(
    body: ImportFolderRequest, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Copy a server-side folder of images into the workspace and run it.

    Unlike `/api/charts/register-local`, the source does **not** have to be
    under `data/folders` already — images are copied in and renamed to
    `1.jpg`, `2.jpg` … in natural-sort order, the same shape the blob intake
    produces.
    """
    try:
        result = import_local_folder(
            body.source_path,
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
        background_tasks.add_task(_bg_pipeline, result["chart_id"], False, None)
    return {
        "status": "accepted",
        "chart_id": result["chart_id"],
        "chart_name": result["chart_name"],
        "source": result["source"],
        "imported": result["imported"],
        "moved": result["moved"],
        "manifest": result["manifest"],
        "page_count": result["page_count"],
        "manifest_rows_linked": result["manifest_rows_linked"],
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
