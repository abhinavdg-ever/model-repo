"""Admin Playground: start core-pipeline batch runs."""

import logging
from fastapi import APIRouter, Depends, HTTPException
from app.core.config import Settings, get_settings
from app.core.schemas import PlaygroundRun
from app.services import playground
from app.services.auth import SignedInUser
from app.routers.deps import current_user

logger = logging.getLogger("review_ui.api")
router = APIRouter()


@router.post("/playground/batch-run", status_code=202)
def playground_batch_run(
    body: PlaygroundRun,
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> dict:
    """Admins only: start a core-pipeline batch run over a blob folder, written to the
    matching Processed folder (Raw_Input/Run2/Batch6/DEID_PNGs -> Processed/Run2/Batch6)."""
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in again to start a run")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only an admin can start a run")
    targets = settings.playground_targets
    if not targets:
        raise HTTPException(status_code=503, detail="Playground is off: set CORE_PIPELINE_URLS in review-ui/.env")
    wanted = body.target.strip().rstrip("/")
    if not wanted:
        raise HTTPException(status_code=400, detail="Choose the pipeline to run on")
    # Only a configured pipeline: the browser never names an arbitrary address.
    chosen = next((t for t in targets if wanted in (t["url"], t["label"])), None)
    if chosen is None:
        names = ", ".join(f"{t['label']} ({t['url']})" for t in targets)
        raise HTTPException(status_code=400, detail=f"Unknown pipeline {wanted}; choose one of {names}")
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

