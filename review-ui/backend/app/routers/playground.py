"""Admin Playground: start core-pipeline batch runs."""

import logging
import re
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.core.config import Settings, get_settings
from app.core.schemas import PlaygroundRun
from app.store.base import FolderRepository
from app.store.factory import get_repository
from app.services import playground
from app.services.chart_tags import chart_tags_map
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


def _tag_map(settings: Settings) -> dict[str, list[str]]:
    try:
        return chart_tags_map(settings.database_url, settings.db_schema)
    except Exception as exc:  # noqa: BLE001 -- no tags column / no database
        logger.warning("playground tag lookup failed: %s", exc)
        raise HTTPException(status_code=503, detail="Could not read the chart tags") from exc


@router.get("/playground/tags")
def playground_tags(
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> dict:
    """Admins only (2026-11-22, user): every tag and the charts carrying it, for the Tag Run."""
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in again")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only an admin can start a run")
    by_tag: dict[str, list[str]] = {}
    for chart, tags in _tag_map(settings).items():
        for tag in tags:
            key = next((k for k in by_tag if k.casefold() == tag.casefold()), tag)
            by_tag.setdefault(key, []).append(chart)
    return {"tags": [{"tag": t, "charts": sorted(c)} for t, c in sorted(by_tag.items(), key=lambda kv: kv[0].casefold())]}


class TagRunRequest(BaseModel):
    target: str
    tags: list[str] | str
    match: str = Field(default="any", pattern="^(any|all)$")
    skip_ocr: bool = True
    skip_completed: bool = False


@router.post("/playground/tag-run", status_code=202)
def playground_tag_run(
    body: TagRunRequest,
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> dict:
    """Admins only (2026-11-22, user): run every chart carrying the tag(s), across runs and
    batches, through core-pipeline's batch-run-tag (each chart from its recorded Blob folder to
    its own Processed folder). At most MAX_CHARTS charts, as every Playground run."""
    chosen = _admin_target(settings, user, body.target)
    # A tag may hold a space ("Pilot Nov"): split a string on commas and new lines only.
    raw = re.split(r"[,;\n]+", body.tags) if isinstance(body.tags, str) else body.tags
    tags = list(dict.fromkeys(t.strip() for t in raw if t and t.strip()))
    if not tags:
        raise HTTPException(status_code=400, detail="Choose at least one tag")
    charts = playground.tagged_charts(_tag_map(settings), tags, match_all=body.match == "all")
    if not charts:
        raise HTTPException(status_code=404, detail=f"No chart is tagged {', '.join(tags)} ({body.match})")
    if len(charts) > playground.MAX_CHARTS:
        raise HTTPException(status_code=400, detail=(
            f"{len(charts)} charts carry {', '.join(tags)} ({body.match}): a run takes at most {playground.MAX_CHARTS}"))
    request = playground.tag_body(tags=tags, match_all=body.match == "all",
                                  skip_ocr=body.skip_ocr, skip_completed=body.skip_completed)
    try:
        answer = playground.start_batch(chosen["url"], request, settings.orchestrator_api_key,
                                        path="/api/charts/batch-run-tag")
    except playground.PipelineError as exc:
        logger.warning("playground tag run refused for %s: %s", tags, exc.detail)
        raise HTTPException(status_code=exc.status if 400 <= exc.status < 600 else 502, detail=exc.detail) from exc
    logger.info("playground tag run of %s (%d charts) on %s started by %s",
                tags, len(charts), chosen["label"], user.username if user else "")
    return {"target": chosen["url"], "target_label": chosen["label"], "request": request,
            "charts": charts, "pipeline": answer}
