from urllib.parse import quote
import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from app.adapters.base import FolderRepository
from app.adapters.factory import get_repository
from app.core.config import ROOT_DIR, Settings, get_settings
from app.core.schemas import (
    AnnotationListResponse,
    AnnotationRow,
    AnnotationSaveRequest,
    AppConfigResponse,
    FileViewerFolderDetail,
    FileViewerListResponse,
    FolderDetail,
    FolderListResponse,
    FolderSummary,
    HealthResponse,
    ExtractionReviewResponse,
    AccuracyReportResponse,
    ImagingDocumentResponse,
    ImagingManifestDetails,
    OcrTextResponse,
    PageGroundTruth,
)
from app.services.blob_store import download_blob_bytes
from app.services.folder_list import FolderListParams, build_folder_list
from app.services.annotations import annotation_file, load_annotations, save_annotation
from app.services.extraction_review import load_extraction_review
from app.services.imaging_csv import filter_folder, iter_csv_lines
from app.services.chart_run_batch import database_url_usable
from app.services.accuracy_cache import clear_report_cache, load_report
from app.services import excel_compare
from app.services.ground_truth import (
    GroundTruthSource,
    bulk_ground_truth,
    ground_truth_charts,
    ground_truth_labels,
    upsert_page_ground_truth,
)
from app.services.page_images import (
    bytes_to_display_jpeg,
    cached_derived_jpeg,
    is_tiff_name,
    is_tiff_path,
    path_to_display_jpeg,
    tiff_bytes_to_jpeg_bytes,
    tiff_path_to_jpeg_bytes,
)

logger = logging.getLogger("review_ui.api")
router = APIRouter()


def _build_blob_url(
    settings: Settings,
    *,
    folder_id: str,
    filename: str,
    page_number: int,
) -> str:
    account = settings.blob_account_url.strip().rstrip("/")
    container = settings.blob_container.strip().strip("/")
    if not account or not container:
        raise HTTPException(status_code=400, detail="BLOB_ACCOUNT_URL / BLOB_CONTAINER not configured")

    template = settings.blob_path_template.strip() or "{folder}/pages/{filename}"
    key = (
        template.replace("{folder}", folder_id)
        .replace("{filename}", filename)
        .replace("{page}", str(page_number))
        .lstrip("/")
    )
    encoded_key = "/".join(quote(part, safe="") for part in key.split("/"))
    sas = settings.blob_sas_token.strip()
    if sas.startswith("?"):
        sas = sas[1:]
    base = f"{account}/{container}/{encoded_key}"
    return f"{base}?{sas}" if sas else base


def _filename_for_page(repo: FolderRepository, folder_id: str, page_number: int) -> str:
    detail = repo.get_folder(folder_id)
    for page in detail.pages:
        if page.page_number == page_number:
            return page.filename
    raise HTTPException(status_code=404, detail=f"Page {page_number} not found in {folder_id}")


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        data_mode=settings.data_mode,
        mode_label=settings.mode_label,
    )


@router.get("/config", response_model=AppConfigResponse)
def app_config() -> AppConfigResponse:
    """Public UI config. Does not expose secrets."""
    settings = get_settings()
    sas_configured = bool(settings.blob_sas_token.strip())
    return AppConfigResponse(
        data_mode=settings.data_mode,
        mode_label=settings.mode_label,
        file_viewer_blob_enabled=settings.file_viewer_blob_enabled,
        blob_auth_mode=settings.blob_auth_mode,
        blob_account_url=settings.blob_account_url.strip().rstrip("/"),
        blob_container=settings.blob_container.strip().strip("/"),
        blob_path_template=settings.blob_path_template.strip() or "{folder}/pages/{filename}",
        blob_entra_ready=settings.blob_entra_ready,
        blob_auth_required=settings.blob_auth_required,
        blob_sas_configured=sas_configured,
    )


@router.get("/folders", response_model=FolderListResponse)
def list_folders(
    repo: FolderRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
    limit: int | None = Query(
        None,
        description="Page size. Omit or <=0 to return all matching rows.",
    ),
    offset: int = Query(0, ge=0),
    q: str = Query("", description="Case-insensitive chart name substring"),
    status: list[str] | None = Query(
        None, description="Repeatable OCR status filter (QUEUED, …)"
    ),
    run: list[str] | None = Query(None, description="Repeatable run_id filter (R2, …)"),
    batch: list[str] | None = Query(
        None, description="Repeatable batch_id filter (B4, …)"
    ),
    sort: str = Query("updated", pattern="^(filename|pages|updated)$"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
) -> FolderListResponse:
    """Landing list: DB-backed summaries when DATABASE_URL is set; paginated."""
    params = FolderListParams(
        q=q or "",
        status=list(status or []),
        run=list(run or []),
        batch=list(batch or []),
        sort=sort,  # type: ignore[arg-type]
        sort_dir=sort_dir,  # type: ignore[arg-type]
        limit=limit,
        offset=offset,
    )
    data_root = settings.resolved_data_root
    db_url = settings.database_url
    full_local = None
    # Expensive full disk scan only when Postgres is unavailable.
    if not database_url_usable(db_url):
        full_local = repo.list_folders()
    result = build_folder_list(
        database_url=db_url,
        db_schema=settings.db_schema,
        data_root=data_root if data_root.is_dir() else None,
        full_local_rows=full_local,
        params=params,
    )
    logger.info(
        "list_folders total=%d returned=%d limit=%s offset=%d",
        result.total,
        len(result.items),
        result.limit,
        result.offset,
    )
    return FolderListResponse(
        items=result.items,
        total=result.total,
        page_count_sum=result.page_count_sum,
        ocr_processed_sum=result.ocr_processed_sum,
        run_options=result.run_options,
        batch_options=result.batch_options,
        limit=result.limit,
        offset=result.offset,
    )


def _file_viewer_repo(settings: Settings):
    """Disk reader for File Viewer. Never opens Postgres."""
    from app.adapters.local.repository import LocalFolderRepository

    return LocalFolderRepository(settings.resolved_data_root, database_url="")


def _serve_disk_image(path: Path) -> Response:
    cache_hdr = "private, max-age=300"
    if is_tiff_path(path):
        try:
            jpeg = tiff_path_to_jpeg_bytes(path)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Could not convert image for display: {exc}",
            ) from exc
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={"Cache-Control": cache_hdr},
        )
    return FileResponse(
        path,
        media_type=_media_type(path.suffix),
        headers={"Cache-Control": cache_hdr},
    )


@router.get("/file-viewer/folders", response_model=FileViewerListResponse)
def list_file_viewer_folders(
    settings: Settings = Depends(get_settings),
) -> FileViewerListResponse:
    """Folders under data/folders that contain pages/. No database."""
    items = _file_viewer_repo(settings).list_file_viewer_folders()
    return FileViewerListResponse(items=items, total=len(items))


@router.get("/file-viewer/folders/{folder_id}", response_model=FileViewerFolderDetail)
def get_file_viewer_folder(
    folder_id: str,
    settings: Settings = Depends(get_settings),
) -> FileViewerFolderDetail:
    return _file_viewer_repo(settings).get_file_viewer_folder(folder_id)


@router.get("/file-viewer/folders/{folder_id}/pages/{page_number}/image")
def get_file_viewer_page_image(
    folder_id: str,
    page_number: int,
    corrected: bool = Query(False, description="Serve corrected-pages when that file exists"),
    settings: Settings = Depends(get_settings),
):
    path = _file_viewer_repo(settings).get_file_viewer_image_path(
        folder_id, page_number, corrected=corrected
    )
    return _serve_disk_image(path)


@router.get("/folders/{folder_id}", response_model=FolderDetail)
def get_folder(folder_id: str, repo: FolderRepository = Depends(get_repository)) -> FolderDetail:
    detail = repo.get_folder(folder_id)
    logger.info(
        "get_folder id=%s pages=%d",
        folder_id,
        len(detail.pages),
    )
    return detail


@router.get("/folders/{folder_id}/manifest", response_model=ImagingManifestDetails)
def get_folder_manifest(
    folder_id: str,
    repo: FolderRepository = Depends(get_repository),
) -> ImagingManifestDetails:
    """Expected member identity from ``manifest_member_list`` (SQL) / metadata CSV."""
    detail = repo.get_folder(folder_id)
    return detail.manifest or ImagingManifestDetails()


@router.get("/folders/{folder_id}/pages/{page_number}/image")
def get_page_image(
    folder_id: str,
    page_number: int,
    repo: FolderRepository = Depends(get_repository),
    thumb: bool = Query(
        False,
        description="Return a small JPEG (~160px) for filmstrip thumbnails",
    ),
    corrected: bool = Query(
        False,
        description="Serve corrected-pages when that file exists. Default is pages/.",
    ),
):
    """Serve a page image. TIFF is re-encoded to JPEG — browsers cannot show it raw.

    Production Mode prefers Azure Blob via ``chart_list.blob_path`` (and
    Processed ``output_path`` when present). Falls back to DATA_ROOT when a
    local workspace copy exists.
    """
    settings = get_settings()
    cache_hdr = (
        "private, max-age=3600"
        if thumb
        else "private, max-age=300"
    )
    if settings.is_production_mode:
        from app.adapters.postgres.repository import PostgresFolderRepository
        from app.services.blob_store import download_blob_at

        if isinstance(repo, PostgresFolderRepository):
            loc = repo.resolve_page_blob(folder_id, page_number, corrected=corrected)
            if loc:
                name = loc.get("filename") or loc["key"]
                etag = loc.get("etag") or ""
                # With an ETag, a derived JPEG (thumb / TIFF conversion) is
                # served from disk without touching the blob at all.
                if etag and (thumb or is_tiff_name(name)):
                    stamp = f"blob|{loc['container']}|{loc['key']}|{etag}|{'thumb' if thumb else 'full'}"

                    def _produce() -> bytes:
                        raw, _ = download_blob_at(
                            container=loc["container"], key=loc["key"], filename=name
                        )
                        return bytes_to_display_jpeg(raw, thumb=thumb)

                    try:
                        data = cached_derived_jpeg(stamp, _produce)
                    except HTTPException:
                        raise
                    except Exception as exc:
                        logger.exception(
                            "Blob image encode failed folder=%s page=%s key=%s",
                            folder_id,
                            page_number,
                            loc["key"],
                        )
                        raise HTTPException(
                            status_code=500,
                            detail=f"Could not convert image for display: {exc}",
                        ) from exc
                    return Response(
                        content=data,
                        media_type="image/jpeg",
                        headers={"Cache-Control": cache_hdr},
                    )

                data, media_type = download_blob_at(
                    container=loc["container"],
                    key=loc["key"],
                    filename=loc.get("filename"),
                )
                is_tiff = is_tiff_name(name) or (media_type or "").lower() in {
                    "image/tiff",
                    "image/tif",
                }
                if thumb or is_tiff:
                    try:
                        data = bytes_to_display_jpeg(data, thumb=thumb)
                        media_type = "image/jpeg"
                    except Exception as exc:
                        logger.exception(
                            "Blob image encode failed folder=%s page=%s key=%s",
                            folder_id,
                            page_number,
                            loc["key"],
                        )
                        raise HTTPException(
                            status_code=500,
                            detail=f"Could not convert image for display: {exc}",
                        ) from exc
                return Response(
                    content=data,
                    media_type=media_type or "application/octet-stream",
                    headers={"Cache-Control": cache_hdr},
                )

    try:
        path = repo.get_page_image_path(folder_id, page_number, corrected=corrected)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Page image not found: {folder_id} #{page_number}",
        ) from exc

    if thumb or is_tiff_path(path):
        try:
            jpeg = path_to_display_jpeg(path, thumb=thumb) if thumb else tiff_path_to_jpeg_bytes(path)
        except Exception as exc:
            logger.exception(
                "Image encode failed folder=%s page=%s path=%s",
                folder_id,
                page_number,
                path,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Could not convert image for display: {exc}",
            ) from exc
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={"Cache-Control": cache_hdr},
        )
    return FileResponse(
        path,
        media_type=_media_type(path.suffix),
        headers={"Cache-Control": cache_hdr},
    )


@router.get("/blob/{folder_id}/pages/{page_number}/image")
def get_blob_page_image(
    folder_id: str,
    page_number: int,
    repo: FolderRepository = Depends(get_repository),
):
    """Serve a page image from Azure Blob.

    - blob_auth_mode=entra: proxy bytes using Microsoft Entra ID (works with Shared Key disabled)
    - blob_auth_mode=sas: redirect to object URL with server SAS (requires Shared Key allowed)
    - TIFF always proxied + converted to JPEG (browsers cannot render image/tiff)
    """
    settings = get_settings()
    if not settings.file_viewer_blob_enabled:
        raise HTTPException(status_code=400, detail="Blob viewer is disabled")

    filename = _filename_for_page(repo, folder_id, page_number)
    needs_tiff_convert = is_tiff_name(filename)

    if settings.blob_auth_mode == "entra" or needs_tiff_convert:
        if settings.blob_auth_mode == "entra":
            data, media_type = download_blob_bytes(
                folder_id=folder_id,
                filename=filename,
                page_number=page_number,
            )
        else:
            # SAS mode + TIFF: fetch via SAS URL then convert (no Entra client).
            if not settings.blob_sas_token.strip():
                raise HTTPException(
                    status_code=400,
                    detail="Server SAS not configured; set BLOB_SAS_TOKEN or use BLOB_AUTH_MODE=entra",
                )
            url = _build_blob_url(
                settings,
                folder_id=folder_id,
                filename=filename,
                page_number=page_number,
            )
            data, media_type = _fetch_url_bytes(url, fallback_name=filename)

        if needs_tiff_convert or (media_type or "").lower() in {
            "image/tiff",
            "image/tif",
        }:
            try:
                data = tiff_bytes_to_jpeg_bytes(data)
                media_type = "image/jpeg"
            except Exception as exc:
                logger.exception(
                    "Blob TIFF→JPEG failed folder=%s page=%s file=%s",
                    folder_id,
                    page_number,
                    filename,
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Could not convert TIFF for display: {exc}",
                ) from exc
        return Response(
            content=data,
            media_type=media_type,
            headers={"Cache-Control": "private, max-age=120"},
        )

    if not settings.blob_sas_token.strip():
        raise HTTPException(
            status_code=400,
            detail="Server SAS not configured; set BLOB_SAS_TOKEN or use BLOB_AUTH_MODE=entra",
        )
    url = _build_blob_url(
        settings,
        folder_id=folder_id,
        filename=filename,
        page_number=page_number,
    )
    return RedirectResponse(url=url, status_code=307)


def _fetch_url_bytes(url: str, *, fallback_name: str) -> tuple[bytes, str]:
    """HTTP GET for SAS blob URLs (stdlib only)."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            return data, ctype or _media_type(Path(fallback_name).suffix)
    except urllib.error.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Blob download via SAS failed: HTTP {exc.code}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Blob download via SAS failed: {exc}",
        ) from exc


@router.get("/folders/{folder_id}/ocr", response_model=OcrTextResponse)
def get_folder_ocr(
    folder_id: str,
    kind: str = "preliminary",
    repo: FolderRepository = Depends(get_repository),
) -> OcrTextResponse:
    return repo.get_ocr_text(folder_id, kind)


@router.get("/annotations", response_model=AnnotationListResponse)
def list_annotations(
    folder_id: str = "",
    page_file: str = "",
) -> AnnotationListResponse:
    rows = load_annotations(annotation_file(ROOT_DIR))
    if folder_id:
        rows = [row for row in rows if row["folder_id"] == folder_id]
    if page_file:
        rows = [row for row in rows if row["page_file"] == page_file]
    return AnnotationListResponse(
        rows=[
            AnnotationRow(
                folder_id=row["folder_id"],
                page_number=int(row["page_number"] or 0),
                page_file=row["page_file"],
                field_id=row["field_id"],
                verdict=row["verdict"],
                value=row["value"],
                saved_at=row["saved_at"],
            )
            for row in rows
        ]
    )


@router.post("/annotations", response_model=AnnotationRow)
def post_annotation(body: AnnotationSaveRequest) -> AnnotationRow:
    saved = save_annotation(
        annotation_file(ROOT_DIR),
        folder_id=body.folder_id,
        page_number=body.page_number,
        page_file=body.page_file,
        field_id=body.field_id,
        verdict=body.verdict,
        value=body.value,
    )
    return AnnotationRow(
        folder_id=saved["folder_id"],
        page_number=int(saved["page_number"]),
        page_file=saved["page_file"],
        field_id=saved["field_id"],
        verdict=saved["verdict"],
        value=saved["value"],
        saved_at=saved["saved_at"],
    )


@router.get("/folders/{folder_id}/extraction", response_model=ExtractionReviewResponse)
def get_folder_extraction(
    folder_id: str,
    page: str = Query(..., description="Image file name of the page being reviewed."),
    settings: Settings = Depends(get_settings),
) -> ExtractionReviewResponse:
    """Selected key/value extraction for one page. Processed equals extracted."""
    return load_extraction_review(settings.resolved_data_root, folder_id, page)


@router.post("/folders/{folder_id}/ground-truth", response_model=PageGroundTruth)
def save_folder_ground_truth(
    folder_id: str,
    body: PageGroundTruth,
    settings: Settings = Depends(get_settings),
) -> PageGroundTruth:
    """Upsert one page of ground truth. A later save of the same page updates that row."""
    if not database_url_usable(settings.database_url):
        raise HTTPException(status_code=503, detail="Database is not configured")
    try:
        saved = upsert_page_ground_truth(
            settings.database_url,
            folder_id,
            body,
            settings.db_schema,
        )
        clear_report_cache()  # the accuracy report reads ground truth
        return saved
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("ground truth upsert failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not save ground truth") from exc


@router.get("/accuracy", response_model=AccuracyReportResponse)
def get_accuracy(
    ground_truth: GroundTruthSource = Query(
        default="latest",
        description="latest = newest value from any source; bulk_upload = each page's latest spreadsheet load, manual edits hidden.",
    ),
    refresh: bool = Query(default=False, description="Rebuild instead of reusing the last minute's report."),
    settings: Settings = Depends(get_settings),
) -> AccuracyReportResponse:
    """Saved scores. A chart whose results changed comes back unscored, with a slim document.
    Reused for up to a minute (see accuracy_cache.REPORT_TTL_SEC) unless ``refresh``."""
    if not database_url_usable(settings.database_url):
        raise HTTPException(status_code=503, detail="Database is not configured")
    try:
        return load_report(settings, ground_truth, fresh=refresh)
    except Exception as exc:
        logger.warning("accuracy report failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not load accuracy") from exc


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
    """The charts review-ui knows, listed once on first use: each one's
    pipeline output as Algo Generated CSV bytes, and when it last ran."""

    def __init__(self, repo: FolderRepository) -> None:
        self._repo = repo
        self._folders: dict[str, FolderSummary] | None = None

    def _folder(self, pdf_id: str) -> FolderSummary | None:
        if self._folders is None:
            self._folders = {folder.name: folder for folder in self._repo.list_folders()}
        return self._folders.get(pdf_id)

    def load(self, pdf_id: str) -> bytes | None:
        """None when review-ui has no output for the chart."""
        folder = self._folder(pdf_id)
        if folder is None:
            return None
        try:
            doc = self._repo.get_imaging(folder.id)
        except HTTPException:
            return None  # no imaging output for this chart yet
        return excel_compare.database_system.system_csv([(pdf_id, doc)])

    def last_run(self, pdf_id: str) -> str | None:
        """ISO time of the chart's last pipeline run; None when never run."""
        folder = self._folder(pdf_id)
        moment = folder.last_updated_at if folder else None
        return moment.isoformat() if moment else None


def _system_from_pipeline(repo: FolderRepository, pdf_ids: list[str]) -> tuple[bytes, str]:
    """The pipeline output for the charts named by the ground truth's PDF IDs, as one sheet."""
    charts = _PipelineCharts(repo)
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
        system = _system_from_pipeline(repo, _pdf_ids(ground, "Ground Truth"))
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
    charts = _PipelineCharts(repo)
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


@router.post("/excel-compare/report")
async def post_excel_compare_report(
    ground_file: UploadFile | None = File(default=None),
    ground_source: str = _GROUND_SOURCE,
    ground_truth: GroundTruthSource = _GROUND_TRUTH,
    system_file: UploadFile | None = File(default=None),
    system_source: str = _SYSTEM_SOURCE,
    repo: FolderRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> Response:
    """The same comparison as an Excel workbook (Comparison Results, Summary, Exempted Columns)."""
    ground, system = await _compare_inputs(
        settings, repo, ground_file, ground_source, ground_truth, system_file, system_source
    )
    try:
        data = excel_compare.report(ground, system)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("manual review report failed: %s", exc)
        raise HTTPException(status_code=400, detail=f"Comparison failed: {exc}") from exc
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="manual_review_report.xlsx"'},
    )


@router.get("/folders/{folder_id}/imaging", response_model=ImagingDocumentResponse)
def get_folder_imaging(
    folder_id: str,
    ground_truth: GroundTruthSource = Query(
        default="latest",
        description="latest = newest value from any source; bulk_upload = each page's latest spreadsheet load, manual edits hidden.",
    ),
    repo: FolderRepository = Depends(get_repository),
    settings: Settings = Depends(get_settings),
) -> ImagingDocumentResponse:
    """Imaging page/document results (dummy/local JSON until Postgres schema lands)."""
    doc = repo.get_imaging(folder_id)
    if ground_truth == "bulk_upload":
        doc = doc.model_copy(
            update={
                "pages": bulk_ground_truth(
                    doc.pages, folder_id, settings.database_url, settings.db_schema
                )
            }
        )
    logger.info(
        "get_imaging id=%s pages=%d",
        folder_id,
        len(doc.pages),
    )
    return doc


@router.get("/imaging/export.csv")
def export_imaging_csv(
    status: str | None = Query(
        default=None,
        description="Optional ocr_status filter (e.g. IMAGING_COMPLETED). Omit or ALL = no filter.",
    ),
    q: str | None = Query(
        default=None,
        description="Optional folder name substring filter (case-insensitive).",
    ),
    repo: FolderRepository = Depends(get_repository),
) -> StreamingResponse:
    """Download all imaging pipeline outputs as one CSV (one row per page)."""
    logger.info("export_imaging_csv status=%s q=%s", status or "ALL", q or "")

    def docs():
        for folder in repo.list_folders():
            if not filter_folder(
                name=folder.name,
                ocr_status=folder.ocr_status,
                status=status,
                q=q,
            ):
                continue
            try:
                doc = repo.get_imaging(folder.id)
            except HTTPException:
                continue
            yield folder.name, doc

    filename = "imaging_export.csv"
    return StreamingResponse(
        iter_csv_lines(docs()),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _media_type(suffix: str) -> str:
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".jpe": "image/jpeg",
        ".jfif": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }
    return mapping.get(suffix.lower(), "application/octet-stream")
