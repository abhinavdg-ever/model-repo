"""Admin Playground: start core-pipeline batch runs."""

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.core.config import Settings, get_settings
from app.core.schemas import PlaygroundRun
from app.store.base import FolderRepository
from app.store.factory import get_repository
from app.services import playground
from app.services.auth import SignedInUser
from app.routers.deps import current_user

logger = logging.getLogger("review_ui.api")
router = APIRouter()


def _admin_target(settings: Settings, user: SignedInUser | None, target: str) -> dict[str, str]:
    """The configured pipeline ``target`` names, for a signed-in admin; else an HTTP error."""
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in again to start a run")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only an admin can start a run")
    targets = settings.playground_targets
    if not targets:
        raise HTTPException(status_code=503, detail="Playground is off: set CORE_PIPELINE_URLS in review-ui/.env")
    wanted = (target or "").strip().rstrip("/")
    if not wanted:
        raise HTTPException(status_code=400, detail="Choose the pipeline to run on")
    # Only a configured pipeline: the browser never names an arbitrary address.
    chosen = next((t for t in targets if wanted in (t["url"], t["label"])), None)
    if chosen is None:
        names = ", ".join(f"{t['label']} ({t['url']})" for t in targets)
        raise HTTPException(status_code=400, detail=f"Unknown pipeline {wanted}; choose one of {names}")
    return chosen


@router.post("/playground/batch-run", status_code=202)
def playground_batch_run(
    body: PlaygroundRun,
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> dict:
    """Admins only: start a core-pipeline batch run over a blob folder, written to the
    matching Processed folder (Raw_Input/Run2/Batch6/DEID_PNGs -> Processed/Run2/Batch6)."""
    chosen = _admin_target(settings, user, body.target)
    base_url = chosen["url"]
    if not playground.clean_path(body.input_path):
        raise HTTPException(status_code=400, detail="Give the blob folder that holds the chart folders")
    container = body.container_name.strip() or settings.playground_container.strip()
    if not container:
        raise HTTPException(status_code=400, detail="Give the blob container")
    charts = playground.chart_names(body.chart_list)
    if not charts:
        raise HTTPException(status_code=400, detail="List the charts to run (up to 10)")
    if len(charts) > playground.MAX_CHARTS:
        raise HTTPException(
            status_code=400,
            detail=f"{len(charts)} charts listed: a run takes at most {playground.MAX_CHARTS}",
        )
    request = playground.batch_body(
        container_name=container,
        input_path=body.input_path,
        chart_list=charts,
        skip_ocr=body.skip_ocr,
        skip_completed=body.skip_completed,
    )
    try:
        answer = playground.start_batch(base_url, request, settings.orchestrator_api_key)
    except playground.PipelineError as exc:
        logger.warning("playground run refused for %s: %s", request["input_path"], exc.detail)
        raise HTTPException(status_code=exc.status if 400 <= exc.status < 600 else 502, detail=exc.detail) from exc
    logger.info("playground run of %s -> %s started by %s", request["input_path"], request["output_path"], user.username)
    return {"target": base_url, "target_label": chosen["label"], "request": request, "pipeline": answer}



class RerunRequest(BaseModel):
    target: str
    charts: list[str] = Field(min_length=1)
    skip_ocr: bool = True


@router.post("/playground/rerun", status_code=202)
def playground_rerun(
    body: RerunRequest,
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
    repo: FolderRepository = Depends(get_repository),
) -> dict:
    """Admins only (2026-11-19, user: History's Rerun): run charts again from where they were
    read (chart_list blob_container / blob_path) into their own output folder. Charts sharing a
    source go in one batch-run; at most MAX_CHARTS charts."""
    chosen = _admin_target(settings, user, body.target)
    charts = playground.chart_names(body.charts)
    if len(charts) > playground.MAX_CHARTS:
        raise HTTPException(status_code=400, detail=f"{len(charts)} charts: a rerun takes at most {playground.MAX_CHARTS}")
    groups: dict[tuple[str, str, str], list[str]] = {}
    missing: list[str] = []
    for chart in charts:
        try:
            rows = repo._page_location_rows(chart)
        except Exception:  # noqa: BLE001 -- unknown chart
            rows = []
        source = playground.rerun_source(chart, rows[0][:3] if rows else None, settings.playground_container)
        if source is None:
            missing.append(chart)
        else:
            groups.setdefault(source, []).append(chart)
    if missing:
        raise HTTPException(status_code=400, detail=f"No blob source recorded for: {', '.join(missing)}")
    started = []
    for (container, input_path, output_path), names in groups.items():
        request = playground.batch_body(container_name=container, input_path=input_path, chart_list=names,
                                        skip_ocr=body.skip_ocr, skip_completed=False)
        if output_path:
            request["output_path"] = output_path
        try:
            answer = playground.start_batch(chosen["url"], request, settings.orchestrator_api_key)
        except playground.PipelineError as exc:
            raise HTTPException(status_code=exc.status if 400 <= exc.status < 600 else 502, detail=exc.detail) from exc
        started.append({"request": request, "pipeline": answer})
    logger.info("rerun of %d chart(s) on %s started by %s", len(charts), chosen["label"], user.username if user else "")
    return {"target": chosen["url"], "target_label": chosen["label"], "runs": started}
