"""Step-by-step chart APIs, under /v1 (phase 5, docs/specials/PLATFORM_PLAN.md §6).

Each endpoint names a target (apps/worker/targets.py). The call runs only the target's
stages that are not yet completed on every page, from the first missing one to the end of
the target (later stages read its output); ``rerun=true`` runs all of them. When nothing is
missing the answer is 200 with the stored result at once, so a second call is instant.

Otherwise a run is created (a ``pipeline_jobs`` row, ``stage_name`` ``v1_<target>``; its id
is the ``run_id``) and handed to the same code path as POST /api/charts/run: a worker
thread in this process (RUN_MODE=inline) or one Service Bus message (RUN_MODE=queue), with
``only`` = the missing stages. 202 with the run_id; ``?wait=N`` (max 60) blocks until the
run finishes and returns the result.

Not done yet: a stage counts as done when its page_stage_status rows are completed or
skipped. The plan's version / input-hash staleness (a canon or model change re-runs the
stages it affects) is not built.

Main's names (``connect``, ``RUN_MODE``, ``_require_db`` ...) are read through the module
at call time, so tests that patch them on ``apps.orchestrator_api.main`` patch them here.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query, Response

from apps.orchestrator_api.schemas import RunRequest, StepRequest
from apps.worker import targets as T

logger = logging.getLogger("core-pipeline")

router = APIRouter(prefix="/v1", tags=["v1 step APIs"])

JOB_PREFIX = "v1_"
POLL_SECONDS = 1.0
_ACTIVE = frozenset({"queued", "running"})
_DONE = frozenset({"completed", "failed"})
# Best engine first: final2 (Azure DocIntel), final1, prelim (Tesseract).
OCR_ENGINES = ("azuredocintel", "docling", "tesseract")
_QUALITY_FIELDS = (
    "quality_tag", "quality_score", "printed_or_handwritten", "hw_method", "hw_confidence",
    "document_type", "review_required", "rotation_applied",
)


def _api():
    from apps.orchestrator_api import main

    return main


def _db():
    import common.db as db

    return db


# --- what is missing ----------------------------------------------------------------


def missing_stages(conn: Any, chart_id: Optional[int], target: str, *, rerun: bool = False) -> list[str]:
    """The target's stages to run: from the first one not completed on every page to the end.

    A chart that is not registered, or has no pages, needs all of them.
    """
    db = _db()
    stages = T.stages_for(target)
    if rerun or chart_id is None or not db.list_pages(conn, chart_id):
        return stages
    for index, token in enumerate(stages):
        name, pass_no = T.split(token)
        if db.pages_needing_stage(conn, chart_id, name, pass_no):
            return stages[index:]
    return []


# --- results --------------------------------------------------------------------------


def _page_numbers(conn: Any, chart_id: int) -> list[tuple[int, Any]]:
    return [(int(p["id"]), p.get("page_number")) for p in _db().list_pages(conn, chart_id)]


def _ocr_result(conn: Any, chart_id: int) -> dict[str, Any]:
    db = _db()
    texts = {engine: db.get_ocr_texts(conn, chart_id, engine) for engine in OCR_ENGINES}
    quality = db.get_quality_map(conn, chart_id)
    pages = []
    for pid, number in _page_numbers(conn, chart_id):
        chars = {e: len(texts[e][pid]) for e in OCR_ENGINES if pid in texts[e]}
        engine = next((e for e in OCR_ENGINES if texts[e].get(pid, "").strip()), None)
        q = quality.get(pid) or {}
        pages.append({
            "page_number": number,
            "engine": engine,
            "chars": chars.get(engine, 0) if engine else 0,
            "chars_by_engine": chars,
            "quality": {k: q.get(k) for k in _QUALITY_FIELDS} if q else None,
        })
    return {"pages": pages}


def _page_type_result(conn: Any, chart_id: int) -> dict[str, Any]:
    db = _db()
    cls = db.get_page_classification_map(conn, chart_id)
    bj = db.get_blank_junk_final(conn, chart_id)
    pages = []
    for pid, number in _page_numbers(conn, chart_id):
        c = cls.get(pid) or {}
        b = bj.get(pid)
        pages.append({
            "page_number": number,
            "page_type": c.get("page_type"),
            "page_subtype": c.get("page_subtype"),
            "classification_category": c.get("classification_category"),
            "confidence": c.get("confidence"),
            "blank_junk": {
                "flag": b.get("blank_junk_flag"),
                "junk_subtype": b.get("junk_subtype"),
                "duplicate_of_page_id": b.get("duplicate_of_page_id"),
                "reason": b.get("reason"),
            } if b else None,
        })
    return {"pages": pages}


def _member_result(conn: Any, chart_id: int) -> dict[str, Any]:
    db = _db()
    extraction = db.get_member_extraction_map(conn, chart_id)
    wrong = [
        {
            "page_number": number,
            "extracted_name": (extraction[pid] or {}).get("extracted_name"),
            "extracted_dob": (extraction[pid] or {}).get("extracted_dob"),
            "other_names": (extraction[pid] or {}).get("other_names"),
        }
        for pid, number in _page_numbers(conn, chart_id)
        if (extraction.get(pid) or {}).get("page_status") == "wrong_member"
    ]
    return {"summary": db.get_member_summary(conn, chart_id), "wrong_member_pages": wrong}


def _dos_result(conn: Any, chart_id: int) -> dict[str, Any]:
    """Per page, and per document: consecutive pages that share their document-level dates."""
    dos = _db().get_dos_map(conn, chart_id)
    pages: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    previous_doc: Optional[tuple[Any, Any]] = None  # the page before's document dates
    for pid, number in _page_numbers(conn, chart_id):
        row = dos.get(pid)
        if not row:
            previous_doc = None
            continue
        pages.append({
            "page_number": number,
            "date_of_service_from": row.get("date_of_service_from"),
            "date_of_service_to": row.get("date_of_service_to"),
            "confidence": row.get("confidence"),
        })
        doc = (row.get("date_of_service_from_doclevel"), row.get("date_of_service_to_doclevel"))
        if not any(doc):
            previous_doc = None
            continue
        if doc == previous_doc:
            documents[-1]["last_page"] = number
        else:
            documents.append({
                "first_page": number, "last_page": number,
                "date_of_service_from": doc[0], "date_of_service_to": doc[1],
            })
        previous_doc = doc
    return {"pages": pages, "documents": documents}


def build_result(conn: Any, chart_id: int, target: str) -> dict[str, Any]:
    if target == "ocr":
        return _ocr_result(conn, chart_id)
    if target == "page-type":
        return _page_type_result(conn, chart_id)
    if target == "member-verify":
        return _member_result(conn, chart_id)
    if target == "dos":
        return _dos_result(conn, chart_id)
    chart = _db().get_chart(conn, chart_id) or {}
    return {
        "chart_status": chart.get("status"),
        "page_type": _page_type_result(conn, chart_id)["pages"],
        "member_verification": _member_result(conn, chart_id),
        "dos": _dos_result(conn, chart_id),
    }


# --- running ----------------------------------------------------------------------------


def _update_job(run_id: int, **fields: Any) -> None:
    try:
        with _api().connect() as conn:
            _db().update_job(conn, run_id, **fields)
    except Exception:
        logger.warning("could not update run %s", run_id, exc_info=True)


def _run_inline(run_id: int, req: RunRequest, chart_id: Optional[int], ingest: bool) -> None:
    """The run, in a thread of this process. ``ingest``: fetch from the source first (as
    /api/charts/run); else run the stages on the registered chart's workspace."""
    from apps.worker import chart_run

    _update_job(run_id, status="running", started=True)
    try:
        if ingest:
            out = chart_run.execute_run(req, on_chart=lambda cid: _update_job(run_id, chart_id=cid))
            chart_id = out.get("chart_id") or chart_id
        else:
            chart_run.run_imported_chart(int(chart_id), req)
    except Exception as exc:
        logger.exception("v1 run %s (%s) FAILED", run_id, req.chart_name)
        error = f"{type(exc).__name__}: {exc}"
        if chart_run.is_credential_failure(exc):
            error = f"{error} — {chart_run.CREDENTIAL_HINT}"
        _update_job(run_id, status="failed", completed=True, error_message=error[:4000])
        return
    _update_job(run_id, status="completed", completed=True, chart_id=chart_id)


def _start_inline(run_id: int, req: RunRequest, chart_id: Optional[int], ingest: bool) -> None:
    # A thread, not a BackgroundTask: a BackgroundTask starts only after the response is
    # sent, so ``?wait=N`` could never see it finish.
    from apps.worker import run_stats

    trigger = (run_stats.new_trigger("v1"), "v1")
    threading.Thread(
        target=run_stats.in_trigger, args=(trigger, _run_inline, run_id, req, chart_id, ingest),
        name=f"v1-run-{run_id}", daemon=True,
    ).start()


def _source_request(body: StepRequest, chart_name: str, only: Optional[list[str]]) -> RunRequest:
    if not body.input_path:
        raise HTTPException(status_code=400, detail="input_type needs input_path")
    req = RunRequest.model_validate({
        "input_type": body.input_type, "container_name": body.container_name,
        "input_path": body.input_path, "output_path": body.output_path,
        "chart_name": chart_name, "skip_ocr": body.skip_ocr, "only": only,
    })
    _api()._check_source(req)
    if not req.is_blob and not (Path(req.input_path) / chart_name).is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {Path(req.input_path) / chart_name}")
    return req


def _registered_request(chart: dict[str, Any], body: StepRequest, only: Optional[list[str]], queue: bool) -> RunRequest:
    """A RunRequest for a chart already in chart_list, from its stored source."""
    name = chart["chart_name"]
    blob_path = str(chart.get("blob_path") or "").strip("/")
    if chart.get("source") == "blob" and chart.get("blob_container") and blob_path:
        parent = blob_path.rsplit("/", 1)[0] if "/" in blob_path else ""
        fields = {"input_type": "blob", "container_name": chart["blob_container"], "input_path": parent}
    elif queue:
        raise HTTPException(
            status_code=400,
            detail=f"{name} was imported from a local folder; pass input_type=local and input_path to queue it",
        )
    else:
        from common.config import DATA_ROOT

        # Not read: the stages run on the chart's workspace (run_imported_chart).
        fields = {"input_type": "local", "input_path": str(DATA_ROOT)}
    return RunRequest.model_validate({
        **fields, "output_path": body.output_path, "chart_name": name,
        "skip_ocr": body.skip_ocr, "only": only,
    })


def _wait(run_id: int, wait: int) -> dict[str, Any]:
    deadline = time.monotonic() + wait
    while True:
        with _api().connect() as conn:
            job = dict(_db().get_job(conn, run_id) or {})
        if job.get("status") in _DONE or time.monotonic() >= deadline:
            return job
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def _run_reply(job: dict[str, Any], target: str, response: Response, extra: dict[str, Any]) -> dict[str, Any]:
    status = job.get("status")
    reply = {**extra, "run_id": job.get("id"), "status": status, "done": status in _DONE,
             "poll": f"/v1/runs/{job.get('id')}"}
    if status == "failed":
        reply["error"] = job.get("error_message")
    if status in _DONE:
        response.status_code = 200
        if status == "completed" and job.get("chart_id"):
            with _api().connect() as conn:
                reply["result"] = build_result(conn, int(job["chart_id"]), target)
    else:
        response.status_code = 202
    return reply


def run_target(target: str, chart_name: str, body: StepRequest, wait: int, response: Response) -> dict[str, Any]:
    api = _api()
    db = _db()
    if body.chart_name and body.chart_name != chart_name:
        raise HTTPException(status_code=400, detail=f"chart_name {body.chart_name!r} does not match the path ({chart_name!r})")
    if not body.has_source() and (body.container_name or body.input_path):
        raise HTTPException(status_code=400, detail="container_name / input_path need input_type")
    api._require_db()

    with api.connect() as conn:
        chart = db.get_chart_by_name(conn, chart_name)
        chart_id = int(chart["id"]) if chart else None
        needed = missing_stages(conn, chart_id, target, rerun=body.rerun)
        base = {"target": target, "chart_name": chart_name, "chart_id": chart_id}
        if not needed:
            response.status_code = 200
            return {**base, "status": "completed", "done": True, "stages_run": [],
                    "result": build_result(conn, chart_id, target)}
        job_stage = f"{JOB_PREFIX}{target}"
        active = next(
            (j for j in db.list_jobs(conn, chart_id=chart_id, limit=20)
             if j.get("stage_name") == job_stage and j.get("status") in _ACTIVE),
            None,
        ) if chart_id is not None else None
    if active:
        job = _wait(int(active["id"]), wait) if wait else dict(active)
        return _run_reply(job, target, response, {**base, "already_running": True})
    if chart is None and not body.has_source():
        raise HTTPException(
            status_code=404,
            detail=f"chart {chart_name!r} is not registered; pass input_type and input_path (and container_name for blob)",
        )

    # The whole chain from scratch is a plain full run (only=None): it rebuilds the chart.
    only = None if needed == list(T.CHAIN) else needed
    queue = api.RUN_MODE == "queue"
    req = (_source_request(body, chart_name, only) if body.has_source()
           else _registered_request(chart, body, only, queue))
    extra = {**base, "stages": needed, "rerun": body.rerun, "skip_ocr": body.skip_ocr}

    if queue:
        from apps.worker import run_stats

        queued = api._enqueue_charts([req], run_stats.new_trigger("v1"), job_stage=job_stage, target=target)[0]
        run_id = int(queued["run_id"])
        extra["chart_id"] = queued["chart_id"]
    else:
        with api.connect() as conn:
            run_id = db.create_job(conn, chart_id=chart_id, stage_name=job_stage, status="queued", queue_name="inline")
        _start_inline(run_id, req, chart_id, ingest=body.has_source())
    logger.info("v1 %s: chart %s, run %s, stages %s", target, chart_name, run_id, ", ".join(needed))

    if wait:
        job = _wait(run_id, wait)
    else:
        with api.connect() as conn:
            job = dict(db.get_job(conn, run_id) or {"id": run_id, "status": "queued"})
    return _run_reply(job, target, response, extra)


# --- endpoints ----------------------------------------------------------------------------

_WAIT = Query(0, ge=0, le=60, description="Wait up to N seconds (max 60) and return the result if done")


@router.post("/charts/run", status_code=202)
def v1_run(response: Response, body: StepRequest, wait: int = _WAIT) -> dict[str, Any]:
    """The whole chain (15 stages) for ``chart_name``, skipping a chart already complete."""
    if not body.chart_name:
        raise HTTPException(status_code=400, detail="chart_name is required")
    return run_target("full", body.chart_name, body, wait, response)


def _step(target: str):
    def endpoint(
        chart_name: str,
        response: Response,
        body: Optional[StepRequest] = Body(None),
        wait: int = _WAIT,
    ) -> dict[str, Any]:
        from common.db.paths import normalize_folder_name

        return run_target(target, normalize_folder_name(chart_name) or chart_name,
                          body or StepRequest(), wait, response)

    endpoint.__name__ = f"v1_{target.replace('-', '_')}"
    endpoint.__doc__ = f"Stages for {target}: {', '.join(T.stages_for(target))}. Runs only what is missing."
    return endpoint


for _target in ("ocr", "page-type", "member-verify", "dos"):
    router.add_api_route(f"/charts/{{chart_name}}/{_target}", _step(_target), methods=["POST"], status_code=202)


@router.get("/runs/{run_id}")
def v1_get_run(
    run_id: int,
    wait: int = Query(0, ge=0, le=60, description="Wait up to N seconds (max 60) for the run to finish"),
) -> dict[str, Any]:
    """GET /api/runs/{run_id}, plus ``target`` and, for a completed /v1 run, its ``result``."""
    reply = _api().get_run(run_id, wait)
    job = reply.get("run") or {}
    stage = str(job.get("stage_name") or "")
    if stage.startswith(JOB_PREFIX):
        target = stage[len(JOB_PREFIX):]
        reply["target"] = target
        if reply.get("status") == "completed" and job.get("chart_id") and target in T.TARGETS:
            with _api().connect() as conn:
                reply["result"] = build_result(conn, int(job["chart_id"]), target)
    return reply
