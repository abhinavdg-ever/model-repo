"""Excel Compare: the pipeline's output against a ground-truth workbook."""

import json
import logging
import time
from datetime import datetime
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from app.store.base import FolderRepository
from app.store.factory import get_repository
from app.core.config import Settings, get_settings
from app.core.schemas import ImagingDocumentResponse
from app.services.chart_run_batch import database_url_usable
from app.services import excel_compare
from app.services.ground_truth import (
    GroundTruthSource,
    ground_truth_charts,
    ground_truth_labels,
)

logger = logging.getLogger("review_ui.api")
router = APIRouter()


async def _upload_bytes(label: str, upload: UploadFile | None) -> tuple[bytes, str]:
    if upload is None or not upload.filename:
        raise HTTPException(status_code=400, detail=f"{label} file is required")
    name = upload.filename
    if not name.lower().endswith((".xlsx", ".xls", ".csv")):
        raise HTTPException(status_code=400, detail=f"{label} file must be .xlsx, .xls or .csv")
    data = await upload.read()
    if len(data) > excel_compare.MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"{label} file is over 25 MB")
    return data, name


class _PipelineCharts:
    """Each chart's pipeline output as Algo Generated CSV bytes, and when it
    last ran. With a database, charts are fetched excel_compare.database_algo
    .BATCH at a time in one query each (the page values only); without one, a
    chart is read by name through the repository. Never repo.list_folders(),
    which in Local Mode scans every page of every chart."""

    def __init__(self, repo: FolderRepository, settings: Settings, pdf_ids: list[str]) -> None:
        self._repo = repo
        self._settings = settings
        self._order = list(pdf_ids)
        self._use_db = database_url_usable(settings.database_url)
        self._docs: dict[str, ImagingDocumentResponse | None] = {}
        self._runs: dict[str, datetime | None] | None = {} if self._use_db else None

    def _fetch_from(self, pdf_id: str) -> None:
        """This chart and the next unfetched ones, one query."""
        start = self._order.index(pdf_id) if pdf_id in self._order else 0
        batch = [pdf_id] + [
            name for name in self._order[start + 1:] if name not in self._docs and name != pdf_id
        ][: excel_compare.database_algo.BATCH - 1]
        docs, runs = excel_compare.database_algo.fetch(
            self._settings.database_url, self._settings.db_schema, batch
        )
        for name in batch:
            self._docs[name] = docs.get(name)
        self._runs.update(runs)

    def load(self, pdf_id: str) -> bytes | None:
        """None when there is no output for the chart."""
        if not self._use_db:
            return None  # the database is the only source (2026-11-07)
        if pdf_id not in self._docs:
            self._fetch_from(pdf_id)
        doc = self._docs.get(pdf_id)
        return excel_compare.database_system.system_csv([(pdf_id, doc)]) if doc else None

    def last_run(self, pdf_id: str) -> str | None:
        """ISO time of the chart's last pipeline run; None when never run."""
        if not self._use_db:
            return None
        if pdf_id not in self._docs:
            self._fetch_from(pdf_id)
        moment = self._runs.get(pdf_id)
        return moment.isoformat() if moment else None


def _system_from_pipeline(
    repo: FolderRepository, settings: Settings, pdf_ids: list[str]
) -> tuple[bytes, str]:
    """The pipeline output for the charts named by the ground truth's PDF IDs, as one sheet."""
    charts = _PipelineCharts(repo, settings, pdf_ids)
    try:
        parts = [data for data in (charts.load(pdf_id) for pdf_id in pdf_ids) if data]
    except Exception as exc:
        logger.warning("manual review algo output lookup failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not read the algo output") from exc
    if not parts:
        shown = ", ".join(pdf_ids[:5]) + (" …" if len(pdf_ids) > 5 else "")
        raise HTTPException(
            status_code=400,
            detail=f"No algo output for the PDF IDs in the Ground Truth file ({shown}). "
            "PDF ID must be the chart name.",
        )
    # Every part carries the same header; keep the first one's.
    body = parts[0] + b"".join(part.split(b"\n", 1)[1] for part in parts[1:])
    return body, "algo_output_database.csv"


def _ground_from_database(
    settings: Settings, pdf_ids: list[str] | None, ground_truth: GroundTruthSource
) -> tuple[bytes, str]:
    """page_ground_truth as a Ground Truth sheet: for ``pdf_ids``, or every
    chart that has ground truth when None."""
    if not database_url_usable(settings.database_url):
        raise HTTPException(status_code=503, detail="Database is not configured")
    try:
        if pdf_ids is None:
            pdf_ids = ground_truth_charts(settings.database_url, settings.db_schema)
        rows = ground_truth_labels(settings.database_url, pdf_ids, ground_truth, settings.db_schema)
    except Exception as exc:
        logger.warning("manual review ground truth lookup failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not read ground truth") from exc
    if not rows:
        shown = ", ".join(pdf_ids[:5]) + (" …" if len(pdf_ids) > 5 else "")
        detail = (
            f"No ground truth in the database for the PDF IDs in the Algo Generated file ({shown}). "
            "PDF ID must be the chart name."
            if pdf_ids
            else "No ground truth in the database."
        )
        raise HTTPException(status_code=400, detail=detail)
    return excel_compare.database_ground.ground_csv(rows), "ground_truth_database.csv"


def _as_ground_truth(upload: tuple[bytes, str]) -> tuple[bytes, str]:
    """An uploaded sheet in the page_ground_truth format (chart_name,
    page_number, ...) is read exactly as the database rows are; any other
    sheet is read by its headers as before."""
    database_ground = excel_compare.database_ground
    try:
        if database_ground.is_table_format(*upload):
            return database_ground.from_upload(*upload), "ground_truth_upload.csv"
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # unreadable sheet: the comparison reports it
        logger.info("ground truth sheet not read as page_ground_truth: %s", exc)
    return upload


def _pdf_ids(upload: tuple[bytes, str], label: str) -> list[str]:
    try:
        return excel_compare.database_system.pdf_ids(*upload, label=label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _inputs(
    settings: Settings,
    ground_file: UploadFile | None,
    ground_source: str,
    ground_truth: GroundTruthSource,
    system_file: UploadFile | None,
    system_source: str,
) -> tuple[tuple[bytes, str], tuple[bytes, str] | None]:
    """(ground, uploaded algo sheet or None when the algo side is the
    database). Database ground truth covers the algo file's PDF IDs, or every
    chart with ground truth when the algo side is the database too."""
    if ground_source not in ("upload", "db"):
        raise HTTPException(status_code=400, detail="ground_source must be upload or db")
    if system_source not in ("db", "upload"):
        raise HTTPException(status_code=400, detail="system_source must be db or upload")
    ground = await _upload_bytes("Ground Truth", ground_file) if ground_source == "upload" else None
    system = await _upload_bytes("Algo Generated", system_file) if system_source == "upload" else None
    if ground is not None:
        return _as_ground_truth(ground), system
    pdf_ids = _pdf_ids(system, "Algo Generated") if system is not None else None
    return _ground_from_database(settings, pdf_ids, ground_truth), system


async def _compare_inputs(
    settings: Settings,
    repo: FolderRepository,
    ground_file: UploadFile | None,
    ground_source: str,
    ground_truth: GroundTruthSource,
    system_file: UploadFile | None,
    system_source: str,
) -> tuple[tuple[bytes, str], tuple[bytes, str]]:
    """(ground, algo) as two whole sheets; database algo output covers the
    ground truth's PDF IDs."""
    ground, system = await _inputs(settings, ground_file, ground_source, ground_truth, system_file, system_source)
    if system is None:
        system = _system_from_pipeline(repo, settings, _pdf_ids(ground, "Ground Truth"))
    return ground, system


_GROUND_SOURCE = Form(default="upload", description="upload = the Ground Truth file; db = page_ground_truth")


_GROUND_TRUTH = Form(
    default="latest",
    description="With ground_source=db: latest, or bulk_upload (manual edits hidden).",
)


# How often a streaming Manual Review logs "X/Y charts compared".
_PROGRESS_LOG_SEC = 10.0


_SYSTEM_SOURCE = Form(default="db", description="db = the pipeline output review-ui shows; upload = the Algo Generated file")


@router.get("/excel-compare/columns")
def get_excel_compare_columns() -> dict:
    """The columns a Ground Truth or Algo Generated file needs, and the header spellings accepted."""
    return excel_compare.columns()


@router.post("/excel-compare")
async def post_excel_compare(
    ground_file: UploadFile | None = File(default=None),
    ground_source: str = _GROUND_SOURCE,
    ground_truth: GroundTruthSource = _GROUND_TRUTH,
    system_file: UploadFile | None = File(default=None),
    system_source: str = _SYSTEM_SOURCE,
    repo: FolderRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> dict:
    """Manual Review: compare ground truth with the system output. Nothing is stored."""
    ground, system = await _compare_inputs(
        settings, repo, ground_file, ground_source, ground_truth, system_file, system_source
    )
    try:
        return excel_compare.run(ground, system)
    except ValueError as exc:
        # Missing key column, duplicate PDF ID / Page Number, unreadable file.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("manual review compare failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"Comparison failed: {exc}") from exc


@router.post("/excel-compare/stream")
async def post_excel_compare_stream(
    ground_file: UploadFile | None = File(default=None),
    ground_source: str = _GROUND_SOURCE,
    ground_truth: GroundTruthSource = _GROUND_TRUTH,
    system_file: UploadFile | None = File(default=None),
    system_source: str = _SYSTEM_SOURCE,
    repo: FolderRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """The comparison as NDJSON, one chart per line, so the screen fills in
    while later charts are still being read. Bad input fails before the
    stream starts; a failure part-way is an ``error`` line."""
    ground, system = await _inputs(settings, ground_file, ground_source, ground_truth, system_file, system_source)
    charts = _PipelineCharts(repo, settings, _pdf_ids(ground, "Ground Truth"))
    events = excel_compare.stream(
        ground,
        system,
        load_system=None if system is not None else charts.load,
        last_run=charts.last_run,
    )
    try:
        first = next(events)  # reads and checks both files' headers
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("manual review stream failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"Comparison failed: {exc}") from exc

    def lines():
        total = first.get("charts", 0)
        done = 0
        started = last_log = time.monotonic()
        logger.info("manual review: comparing %d charts", total)
        yield json.dumps(first) + "\n"
        for event in events:
            if event["type"] == "chart":
                done += 1
                now = time.monotonic()
                if now - last_log >= _PROGRESS_LOG_SEC:
                    logger.info("manual review: %d/%d charts compared", done, total)
                    last_log = now
            elif event["type"] == "done":
                logger.info(
                    "manual review: %d/%d charts compared in %.1fs",
                    done, total, time.monotonic() - started,
                )
            elif event["type"] == "error":
                logger.warning(
                    "manual review: stopped at chart %s after %d/%d: %s",
                    event.get("pdfId"), done, total, event.get("detail"),
                )
            yield json.dumps(event) + "\n"

    return StreamingResponse(
        lines(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


_REPORT_ID = Form(
    default=None,
    description="The reportId of the comparison on screen: built from it, without comparing again.",
)


async def _download_result(
    report_id: str | None,
    settings: Settings,
    repo: FolderRepository,
    ground_file: UploadFile | None,
    ground_source: str,
    ground_truth: GroundTruthSource,
    system_file: UploadFile | None,
    system_source: str,
) -> dict:
    """The comparison a download is built from: the one on screen (``report_id``)
    while it is kept, else the inputs compared again."""
    result = excel_compare.remembered(report_id)
    if result is not None:
        return result
    ground, system = await _compare_inputs(
        settings, repo, ground_file, ground_source, ground_truth, system_file, system_source
    )
    try:
        return await run_in_threadpool(excel_compare.comparison, ground, system)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("manual review comparison failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"Comparison failed: {exc}") from exc


@router.post("/excel-compare/report")
async def post_excel_compare_report(
    ground_file: UploadFile | None = File(default=None),
    ground_source: str = _GROUND_SOURCE,
    ground_truth: GroundTruthSource = _GROUND_TRUTH,
    system_file: UploadFile | None = File(default=None),
    system_source: str = _SYSTEM_SOURCE,
    report_id: str | None = _REPORT_ID,
    repo: FolderRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> Response:
    """The same comparison as an Excel workbook (Comparison Results, Summary, Exempted Columns)."""
    result = await _download_result(
        report_id, settings, repo, ground_file, ground_source, ground_truth, system_file, system_source
    )
    try:
        data = await run_in_threadpool(excel_compare.report, result)
    except Exception as exc:
        logger.warning("manual review report failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Could not build the report: {exc}") from exc
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="manual_review_report.xlsx"'},
    )


@router.post("/excel-compare/summary")
async def post_excel_compare_summary(
    ground_file: UploadFile | None = File(default=None),
    ground_source: str = _GROUND_SOURCE,
    ground_truth: GroundTruthSource = _GROUND_TRUTH,
    system_file: UploadFile | None = File(default=None),
    system_source: str = _SYSTEM_SOURCE,
    report_id: str | None = _REPORT_ID,
    repo: FolderRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> Response:
    """The same comparison as the printable Master Summary PDF (totals, chart-wise and
    field-wise accuracy, mismatch patterns, Missing / Extra pages, exempted columns)."""
    result = await _download_result(
        report_id, settings, repo, ground_file, ground_source, ground_truth, system_file, system_source
    )
    ground_label = (
        ground_file.filename
        if ground_source == "upload" and ground_file and ground_file.filename
        else "Ground truth (database)"
    )
    system_label = (
        system_file.filename
        if system_source == "upload" and system_file and system_file.filename
        else "Pipeline output (database)"
    )
    try:
        data = await run_in_threadpool(
            excel_compare.summary_pdf, result, ground_label=ground_label, system_label=system_label
        )
    except Exception as exc:
        logger.warning("manual review summary failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Could not build the Master Summary: {exc}") from exc
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="manual_review_master_summary.pdf"'},
    )

