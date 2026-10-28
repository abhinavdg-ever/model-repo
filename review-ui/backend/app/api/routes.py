from urllib.parse import quote
import json
import logging
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from app.adapters.base import FolderRepository
from app.adapters.factory import get_repository, request_is_production
from app.core.config import ROOT_DIR, Settings, get_settings
from app.core.schemas import (
    ChartGroundTruthUpdates,
    ChartReview,
    ReviewLogResponse,
    LoginRequest,
    LoginResponse,
    SignedInUserResponse,
    AnnotationListResponse,
    AnnotationRow,
    AnnotationSaveRequest,
    AppConfigResponse,
    NewUser,
    PasswordReset,
    PlaygroundRun,
    FileViewerFolderDetail,
    FileViewerListResponse,
    FolderDetail,
    FolderListResponse,
    FolderSummary,
    HealthResponse,
    ExtractionPagesResponse,
    ExtractionReviewResponse,
    AccuracyReportResponse,
    ImagingDocumentResponse,
    ImagingManifestDetails,
    OcrTextResponse,
    PageGroundTruth,
    PageSequenceSave,
    EncounterReview,
    EncounterReviewSave,
    ChartGroundTruth,
)
from app.services.blob_store import download_blob_bytes
from app.services import ocr_search, playground, user_admin
from app.services.folder_list import FolderListParams, build_folder_list
from app.services.annotations import annotation_file, load_annotations, save_annotation
from app.services.extraction_review import (
    load_extraction_pages,
    load_extraction_pages_from_db,
    load_extraction_review,
    load_extraction_review_from_db,
)
from app.services.staged_continuity import load_encounters_from_db, load_staged_encounters
from app.services.imaging_csv import filter_folder, iter_csv_lines
from app.services.chart_run_batch import database_url_usable
from app.services.accuracy_cache import clear_report_cache, load_report
from app.services.auth import SignedInUser, authenticate, issue_token, read_token
from app.services.chart_delete import ChartNotFound, delete_chart
from app.services import excel_compare
from app.services.ground_truth import (
    GroundTruthSource,
    bulk_ground_truth,
    ground_truth_charts,
    ground_truth_labels,
    save_page_sequences,
    upsert_page_ground_truth,
)
from app.services.encounter_review import load_encounter_review, save_encounter_review
from app.services.chart_ground_truth import load_chart_ground_truth, save_chart_ground_truth
from app.services.ground_truth_updates import ground_truth_updates
from app.services.chart_review import (
    ChartFrozen,
    ensure_not_frozen,
    load_chart_review,
    review_log,
    set_chart_review,
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


def current_user(request: Request, settings: Settings = Depends(get_settings)) -> SignedInUser | None:
    """The user of the request's ``Authorization: Bearer`` token, or None."""
    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return read_token(settings, token)


def _client_ip(request: Request) -> str | None:
    # Behind a proxy the browser's address is the first X-Forwarded-For entry.
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return (forwarded or (request.client.host if request.client else None) or None)


@router.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, settings: Settings = Depends(get_settings)) -> LoginResponse:
    """Sign in against the users table; records when and from where."""
    try:
        user = authenticate(
            settings,
            body.username,
            body.password,
            ip_address=_client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:500] or None,
        )
    except Exception as exc:
        logger.warning("sign-in failed: %s", exc)
        raise HTTPException(status_code=503, detail="Sign-in is unavailable right now") from exc
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    return LoginResponse(
        token=issue_token(settings, user),
        user=SignedInUserResponse(id=user.id, username=user.username, name=user.name, role=user.role),
    )


@router.get("/auth/me", response_model=SignedInUserResponse)
def me(user: SignedInUser | None = Depends(current_user)) -> SignedInUserResponse:
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return SignedInUserResponse(id=user.id, username=user.username, name=user.name, role=user.role)


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


def _mode(request: Request) -> tuple[str, str]:
    """(data_mode, label) from DATA_MODE."""
    production = request_is_production(request)
    return ("production", "Production Mode") if production else ("local", "Local Mode")


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    data_mode, label = _mode(request)
    return HealthResponse(status="ok", data_mode=data_mode, mode_label=label)


@router.get("/config", response_model=AppConfigResponse)
def app_config(request: Request) -> AppConfigResponse:
    """Public UI config. Does not expose secrets."""
    settings = get_settings()
    sas_configured = bool(settings.blob_sas_token.strip())
    data_mode, label = _mode(request)
    return AppConfigResponse(
        data_mode=data_mode,
        mode_label=label,
        db_available=database_url_usable(settings.database_url),
        default_mode="production" if settings.is_production_mode else "local",
        file_viewer_blob_enabled=settings.file_viewer_blob_enabled,
        blob_auth_mode=settings.blob_auth_mode,
        blob_account_url=settings.blob_account_url.strip().rstrip("/"),
        blob_container=settings.blob_container.strip().strip("/"),
        blob_path_template=settings.blob_path_template.strip() or "{folder}/pages/{filename}",
        blob_entra_ready=settings.blob_entra_ready,
        blob_auth_required=settings.blob_auth_required,
        blob_sas_configured=sas_configured,
        playground_enabled=bool(settings.playground_targets),
        playground_targets=settings.playground_targets,
        playground_container=settings.playground_container.strip(),
        client_view=settings.client_view,
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
    source: list[str] | None = Query(None, description="Repeatable Source (source_name) filter"),
    measure: list[str] | None = Query(None, description="Repeatable Measure filter"),
    retrieval: list[str] | None = Query(None, description="Repeatable Retrieval (RR Method) filter"),
    scope: list[str] | None = Query(None, description="Repeatable Scope (project_scope) filter"),
    sort: str = Query("updated", pattern="^(filename|pages|updated)$"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    run_after: datetime | None = Query(
        None, description="Only charts last run at or after this ISO date-time (with offset)"
    ),
) -> FolderListResponse:
    """Landing list: DB-backed summaries when DATABASE_URL is set; paginated."""
    params = FolderListParams(
        q=q or "",
        status=list(status or []),
        run=list(run or []),
        batch=list(batch or []),
        source=list(source or []),
        measure=list(measure or []),
        retrieval=list(retrieval or []),
        scope=list(scope or []),
        run_after=run_after,
        sort=sort,  # type: ignore[arg-type]
        sort_dir=sort_dir,  # type: ignore[arg-type]
        limit=limit,
        offset=offset,
    )
    # Production lists chart_list only; Local Mode merges in the workspace folders.
    data_root = None if settings.is_production_mode else settings.resolved_data_root
    db_url = settings.database_url
    full_local = None
    # Expensive full disk scan only when Postgres is unavailable.
    if not database_url_usable(db_url):
        full_local = repo.list_folders()
    result = build_folder_list(
        database_url=db_url,
        db_schema=settings.db_schema,
        data_root=data_root if data_root is not None and data_root.is_dir() else None,
        full_local_rows=full_local,
        params=params,
        metadata_dir=None if settings.is_production_mode else settings.resolved_metadata_root,
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
        imaging_completed_charts=result.imaging_completed_charts,
        imaging_completed_pages=result.imaging_completed_pages,
        ground_truth_charts=result.ground_truth_charts,
        run_options=result.run_options,
        batch_options=result.batch_options,
        scope_options=result.scope_options,
        source_options=result.source_options,
        measure_options=result.measure_options,
        retrieval_options=result.retrieval_options,
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


def _blob_page_image(repo, folder_id: str, page_number: int, corrected: bool, thumb: bool, cache_hdr: str):
    """The page image from Azure Blob, or None when the chart has no blob for the page."""
    from app.services.blob_store import download_blob_at

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
    return None


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
    from app.adapters.postgres.repository import PostgresFolderRepository

    # Production Mode: Azure Blob first; when Blob has no copy or fails (sign-in, network),
    # the workspace copy of the page image. Page data never comes from disk.
    blob_error: str | None = None
    blob_missing = False  # looked in Blob, nothing there (not a sign-in / network failure)
    if isinstance(repo, PostgresFolderRepository):
        try:
            served = _blob_page_image(repo, folder_id, page_number, corrected, thumb, cache_hdr)
        except HTTPException as exc:
            if exc.status_code not in (502, 503):
                raise
            blob_error = str(exc.detail)
            served = None
        if served is not None:
            return served
        if blob_error is None:
            from app.services.blob_store import blob_unavailable

            blob_error = blob_unavailable()
        if blob_error is None:
            # No sign-in trouble: the blob was simply not where it was looked for.
            missing = repo.no_blob_reason(folder_id, page_number)
            if missing:
                blob_error, blob_missing = f"not in Azure Blob: {missing}", True

    try:
        path = repo.get_page_image_path(folder_id, page_number, corrected=corrected)
    except Exception as exc:
        if blob_error:
            # 503 (sign-in / network): the viewer offers "Retry images" (POST /api/blob/retry).
            # 404: the image is not in Blob at all, so a retry cannot help.
            raise HTTPException(
                status_code=404 if blob_missing else 503,
                detail=f"Page image unavailable: {blob_error}; no local copy",
            ) from exc
        if isinstance(exc, HTTPException):
            raise
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


@router.post("/blob/retry")
def retry_blob(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    """Try Azure Blob again now (after a sign-in failure), and say whether it works."""
    from app.services.blob_store import check_blob_access, reset_blob_access

    reset_blob_access()
    return {"blob": check_blob_access(settings)}


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


@router.get("/folders/{folder_id}/extraction/pages", response_model=ExtractionPagesResponse)
def get_folder_extraction_pages(
    folder_id: str,
    settings: Settings = Depends(get_settings),
) -> ExtractionPagesResponse:
    """Every page's extraction review in one response, so the Additional tab (Section
    Headers, Page Numbers) is ready on every page as soon as the chart opens. Same sources
    as /extraction: the staging file (Local), additional_page_details (Production)."""
    if settings.is_production_mode:
        if not database_url_usable(settings.database_url):
            return ExtractionPagesResponse(folder_id=folder_id, available=False)
        return load_extraction_pages_from_db(settings.database_url, settings.db_schema, folder_id)
    return load_extraction_pages(settings.resolved_data_root, folder_id)


@router.get("/folders/{folder_id}/extraction", response_model=ExtractionReviewResponse)
def get_folder_extraction(
    folder_id: str,
    page: str = Query(..., description="Image file name of the page being reviewed."),
    settings: Settings = Depends(get_settings),
) -> ExtractionReviewResponse:
    """Selected key/value extraction for one page. Processed equals extracted.

    Local Mode reads the raw staging file on disk. Production Mode (Postgres + Blob only)
    has no workspace: it reads Page Number and Section Headers from additional_page_details."""
    if settings.is_production_mode:
        if not database_url_usable(settings.database_url):
            return ExtractionReviewResponse(folder_id=folder_id, available=False)
        return load_extraction_review_from_db(
            settings.database_url, settings.db_schema, folder_id, page
        )
    return load_extraction_review(settings.resolved_data_root, folder_id, page)


@router.delete("/folders/{folder_id}")
def delete_folder(
    folder_id: str,
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> dict:
    """Admins only: delete the chart and every pipeline result for it. Its ground truth,
    manifest row and files on disk are kept, so a re-run rebuilds it."""
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in again to delete a chart")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only an admin can delete a chart")
    if not database_url_usable(settings.database_url):
        raise HTTPException(status_code=503, detail="Database is not configured")
    try:
        result = delete_chart(settings.database_url, folder_id, db_schema=settings.db_schema, user_id=user.id)
    except ChartNotFound as exc:
        raise HTTPException(status_code=404, detail=f"No chart named {folder_id}") from exc
    except Exception as exc:
        logger.warning("chart delete failed for %s: %s", folder_id, exc)
        raise HTTPException(status_code=502, detail="Could not delete the chart") from exc
    clear_report_cache()  # the accuracy report lists charts
    logger.info("chart %s deleted by %s", folder_id, user.username)
    return result


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
        answer = playground.start_batch(base_url, request)
    except playground.PipelineError as exc:
        logger.warning("playground run refused for %s: %s", request["input_path"], exc.detail)
        raise HTTPException(status_code=exc.status if 400 <= exc.status < 600 else 502, detail=exc.detail) from exc
    logger.info("playground run of %s -> %s started by %s", request["input_path"], request["output_path"], user.username)
    return {"target": base_url, "target_label": chosen["label"], "request": request, "pipeline": answer}


def _admin_with_db(
    user: SignedInUser | None, settings: Settings, what: str = "manage users", kept: str = "Users are kept"
) -> SignedInUser:
    """The signed-in admin, when a database is configured (401 / 403 / 503 otherwise)."""
    if user is None:
        raise HTTPException(status_code=401, detail=f"Sign in again to {what}")
    if user.role != "admin":
        raise HTTPException(status_code=403, detail=f"Only an admin can {what}")
    if not database_url_usable(settings.database_url):
        raise HTTPException(status_code=503, detail=f"{kept} in the database, which is not configured")
    return user


def _user_call(action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except user_admin.UserError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except Exception as exc:
        logger.warning("user admin failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not reach the users table") from exc


@router.get("/search/ocr")
def search_ocr(
    q: str = Query(..., description="A word or phrase"),
    whole_word: bool = Query(True, description="Match whole words only (pain, not painful)"),
    run: str | None = Query(None, description="Only this run (chart_list.run_id)"),
    batch: str | None = Query(None, description="Only this batch (chart_list.batch_id)"),
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> dict:
    """Admins only: pages whose Tesseract text has the word, grouped by batch and chart."""
    admin = _admin_with_db(user, settings, "search the OCR", "The OCR text is kept")
    try:
        found = ocr_search.search_tesseract(
            settings.database_url, settings.db_schema, q, whole_word=whole_word, run_id=run, batch_id=batch
        )
    except ocr_search.SearchError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except Exception as exc:
        logger.warning("ocr search failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not search the OCR text") from exc
    logger.info("ocr search %r by %s: %d page(s) in %d chart(s)", found["query"], admin.username,
                found["total_pages"], found["total_charts"])
    return found


@router.get("/users")
def list_users(
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> list[dict]:
    """Admins only: every user (no password hashes)."""
    _admin_with_db(user, settings)
    return _user_call(user_admin.list_users, settings.database_url, settings.db_schema)


@router.post("/users", status_code=201)
def create_user(
    body: NewUser,
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> dict:
    """Admins only: a new user. 409 when the username is already taken."""
    admin = _admin_with_db(user, settings)
    created = _user_call(
        user_admin.create_user, settings.database_url, settings.db_schema,
        username=body.username, name=body.name, password=body.password, role=body.role,
    )
    logger.info("user %s (%s) created by %s", created["username"], created["role"], admin.username)
    return created


@router.put("/users/password")
def reset_user_password(
    body: PasswordReset,
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> dict:
    """Admins only: a new password for an existing user. 404 when there is none."""
    admin = _admin_with_db(user, settings)
    changed = _user_call(
        user_admin.reset_password, settings.database_url, settings.db_schema,
        username=body.username, password=body.password,
    )
    logger.info("password of %s reset by %s", changed["username"], admin.username)
    return changed


def _refuse_if_frozen(settings: Settings, folder_id: str) -> None:
    """409 when the chart is marked Reviewed: its ground truth is frozen."""
    try:
        ensure_not_frozen(settings.database_url, folder_id, settings.db_schema)
    except ChartFrozen as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/folders/{folder_id}/review", response_model=ChartReview)
def get_folder_review(folder_id: str, settings: Settings = Depends(get_settings)) -> ChartReview:
    """Whether the chart is marked Reviewed (its ground truth frozen), by whom and when."""
    if not database_url_usable(settings.database_url):
        return ChartReview()
    try:
        return load_chart_review(settings.database_url, folder_id, settings.db_schema)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("chart review read failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not read the review mark") from exc


@router.put("/folders/{folder_id}/review", response_model=ChartReview)
def put_folder_review(
    folder_id: str,
    body: ChartReview,
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> ChartReview:
    """Mark the chart Reviewed (reviewers and admins), freezing its ground truth; or clear
    the mark (admins, or the user who marked it)."""
    if not database_url_usable(settings.database_url):
        raise HTTPException(status_code=503, detail="Database is not configured")
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in again to mark the chart")
    if user.role == "viewer":
        raise HTTPException(status_code=403, detail="Viewers cannot mark a chart Reviewed")
    try:
        if not body.reviewed and user.role != "admin":
            marked = load_chart_review(settings.database_url, folder_id, settings.db_schema)
            if marked.reviewedById is None or marked.reviewedById != user.id:
                raise HTTPException(status_code=403, detail="Reach out to an Admin to enable review")
        saved = set_chart_review(
            settings.database_url, folder_id, body.reviewed, settings.db_schema, user_id=user.id
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("chart review save failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not save the review mark") from exc
    logger.info("chart %s %s by %s", folder_id, "marked Reviewed" if body.reviewed else "unmarked", user.username)
    return saved


@router.get("/folders/{folder_id}/ground-truth/updates", response_model=ChartGroundTruthUpdates)
def get_folder_ground_truth_updates(
    folder_id: str, settings: Settings = Depends(get_settings)
) -> ChartGroundTruthUpdates:
    """Who last updated each ground-truth tab (a user, or the bulk upload), and when."""
    if not database_url_usable(settings.database_url):
        return ChartGroundTruthUpdates()
    try:
        return ground_truth_updates(settings.database_url, folder_id, settings.db_schema)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("ground truth updates read failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not read the last update") from exc


@router.get("/review-log", response_model=ReviewLogResponse)
def get_review_log(
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
    user_id: int | None = Query(default=None, description="Admins: one user's log"),
    all_users: bool = Query(default=False, description="Admins: everyone's log"),
    limit: int = Query(default=200, ge=1, le=2000),
    offset: int = Query(default=0, ge=0),
) -> ReviewLogResponse:
    """The signed-in user's ground-truth saves, newest first. An admin may ask for another
    user's (user_id) or everyone's (all_users)."""
    if not database_url_usable(settings.database_url):
        raise HTTPException(status_code=503, detail="Database is not configured")
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in to see your review log")
    admin = user.role == "admin"
    who = user_id if admin and user_id is not None else user.id
    try:
        return review_log(
            settings.database_url,
            settings.db_schema,
            user_id=who,
            all_users=admin and all_users,
            limit=limit,
            offset=offset,
        )
    except Exception as exc:
        logger.warning("review log read failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not read the review log") from exc


@router.put("/folders/{folder_id}/ground-truth/sequence", response_model=list[PageGroundTruth])
def save_folder_page_sequences(
    folder_id: str,
    body: PageSequenceSave,
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> list[PageGroundTruth]:
    """Set the ground-truth actual sequence of the given pages (Sequencing tab).
    Returns the chart's ground-truth pages."""
    if not database_url_usable(settings.database_url):
        raise HTTPException(status_code=503, detail="Database is not configured")
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in again to save ground truth")
    _refuse_if_frozen(settings, folder_id)
    try:
        saved = save_page_sequences(
            settings.database_url, folder_id, body.pages, settings.db_schema, user_id=user.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("page sequence save failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not save the page sequence") from exc
    clear_report_cache()  # the accuracy report reads ground truth
    return sorted(saved.values(), key=lambda page: page.pageNumber)


@router.get("/folders/{folder_id}/encounter-review", response_model=EncounterReview)
def get_folder_encounter_review(
    folder_id: str, settings: Settings = Depends(get_settings)
) -> EncounterReview:
    """The reviewer's encounters for the chart; empty rows when none were saved."""
    try:
        return load_encounter_review(settings.database_url, folder_id, settings.db_schema)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("encounter review read failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not read the encounter review") from exc


@router.put("/folders/{folder_id}/encounter-review", response_model=EncounterReview)
def save_folder_encounter_review(
    folder_id: str,
    body: EncounterReviewSave,
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> EncounterReview:
    """Replace the reviewer's encounters for the chart (Encounter tab). Not ground truth."""
    if not database_url_usable(settings.database_url):
        raise HTTPException(status_code=503, detail="Database is not configured")
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in again to save encounters")
    _refuse_if_frozen(settings, folder_id)
    try:
        return save_encounter_review(
            settings.database_url, folder_id, body.rows, settings.db_schema, user_id=user.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("encounter review save failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not save the encounters") from exc


@router.get("/folders/{folder_id}/chart-ground-truth", response_model=ChartGroundTruth)
def get_folder_chart_ground_truth(
    folder_id: str, settings: Settings = Depends(get_settings)
) -> ChartGroundTruth:
    """The reviewer's member decision for the chart (Rejection Rules tab)."""
    if not database_url_usable(settings.database_url):
        return ChartGroundTruth()
    try:
        return load_chart_ground_truth(settings.database_url, folder_id, settings.db_schema)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("chart ground truth read failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not read the member decision") from exc


@router.put("/folders/{folder_id}/chart-ground-truth", response_model=ChartGroundTruth)
def save_folder_chart_ground_truth(
    folder_id: str,
    body: ChartGroundTruth,
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> ChartGroundTruth:
    """Save the reviewer's member decision, Accept or Reject (empty clears it)."""
    if not database_url_usable(settings.database_url):
        raise HTTPException(status_code=503, detail="Database is not configured")
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in again to save the decision")
    _refuse_if_frozen(settings, folder_id)
    try:
        return save_chart_ground_truth(
            settings.database_url, folder_id, body, settings.db_schema, user_id=user.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning("chart ground truth save failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not save the member decision") from exc


@router.post("/folders/{folder_id}/ground-truth", response_model=PageGroundTruth)
def save_folder_ground_truth(
    folder_id: str,
    body: PageGroundTruth,
    settings: Settings = Depends(get_settings),
    user: SignedInUser | None = Depends(current_user),
) -> PageGroundTruth:
    """Upsert one page of ground truth. A later save of the same page updates that row.
    The signed-in user is recorded on the row and its manual_entry (user_id)."""
    if not database_url_usable(settings.database_url):
        raise HTTPException(status_code=503, detail="Database is not configured")
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in again to save ground truth")
    _refuse_if_frozen(settings, folder_id)
    try:
        saved = upsert_page_ground_truth(
            settings.database_url,
            folder_id,
            body,
            settings.db_schema,
            user_id=user.id,
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

    def _data_root(self) -> Path:
        root = self._settings.resolved_data_root
        return root if isinstance(root, Path) else Path(root)

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
        if self._use_db:
            if pdf_id not in self._docs:
                self._fetch_from(pdf_id)
            doc = self._docs.get(pdf_id)
        else:
            try:
                doc = self._repo.get_imaging(pdf_id)
            except HTTPException:
                doc = None  # no such chart, or no imaging output yet
        return excel_compare.database_system.system_csv([(pdf_id, doc)]) if doc else None

    def last_run(self, pdf_id: str) -> str | None:
        """ISO time of the chart's last pipeline run; None when never run."""
        if self._use_db:
            if pdf_id not in self._docs:
                self._fetch_from(pdf_id)
        elif self._runs is None:
            listed = build_folder_list(
                database_url=self._settings.database_url,
                db_schema=self._settings.db_schema,
                data_root=self._data_root(),
                full_local_rows=None,
                params=FolderListParams(),
            )
            self._runs = {folder.name: folder.last_updated_at for folder in listed.items}
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
    # The encounter grouping: Production Mode from encounter_type_results, Local Mode from the
    # chart workspace's staging file.
    if not doc.encounters:
        encounters = (
            load_encounters_from_db(settings.database_url, settings.db_schema, folder_id)
            if settings.is_production_mode
            else load_staged_encounters(settings.resolved_data_root, folder_id)
        )
        if encounters:
            doc = doc.model_copy(update={"encounters": encounters})
    logger.info(
        "get_imaging id=%s pages=%d encounters=%d",
        folder_id,
        len(doc.pages),
        len(doc.encounters),
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
