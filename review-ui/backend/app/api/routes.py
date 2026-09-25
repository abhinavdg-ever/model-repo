from urllib.parse import quote
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse

from app.adapters.base import FolderRepository
from app.adapters.factory import get_repository
from app.core.config import Settings, get_settings
from app.core.schemas import (
    AppConfigResponse,
    FolderDetail,
    FolderListResponse,
    FolderSummary,
    HealthResponse,
    ImagingDocumentResponse,
    ImagingManifestDetails,
    OcrTextResponse,
)
from app.services.blob_store import download_blob_bytes
from app.services.folder_list import FolderListParams, build_folder_list
from app.services.imaging_csv import filter_folder, iter_csv_lines
from app.services.chart_run_batch import database_url_usable
from app.services.page_images import (
    is_tiff_name,
    is_tiff_path,
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
):
    """Serve a page image. TIFF is re-encoded to JPEG — browsers cannot show it raw.

    Production Mode prefers Azure Blob via ``chart_list.blob_path`` (and
    Processed ``output_path`` when present). Falls back to DATA_ROOT when a
    local workspace copy exists.
    """
    settings = get_settings()
    if settings.is_production_mode:
        from app.adapters.postgres.repository import PostgresFolderRepository
        from app.services.blob_store import download_blob_at

        if isinstance(repo, PostgresFolderRepository):
            loc = repo.resolve_page_blob(folder_id, page_number)
            if loc:
                data, media_type = download_blob_at(
                    container=loc["container"],
                    key=loc["key"],
                    filename=loc.get("filename"),
                )
                if is_tiff_name(loc.get("filename") or loc["key"]) or (
                    media_type or ""
                ).lower() in {"image/tiff", "image/tif"}:
                    try:
                        data = tiff_bytes_to_jpeg_bytes(data)
                        media_type = "image/jpeg"
                    except Exception as exc:
                        logger.exception(
                            "Blob TIFF→JPEG failed folder=%s page=%s key=%s",
                            folder_id,
                            page_number,
                            loc["key"],
                        )
                        raise HTTPException(
                            status_code=500,
                            detail=f"Could not convert TIFF for display: {exc}",
                        ) from exc
                return Response(
                    content=data,
                    media_type=media_type or "application/octet-stream",
                    headers={"Cache-Control": "private, max-age=120"},
                )

    try:
        path = repo.get_page_image_path(folder_id, page_number)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Page image not found: {folder_id} #{page_number}",
        ) from exc

    if is_tiff_path(path):
        try:
            jpeg = tiff_path_to_jpeg_bytes(path)
        except Exception as exc:
            logger.exception(
                "TIFF→JPEG failed folder=%s page=%s path=%s",
                folder_id,
                page_number,
                path,
            )
            raise HTTPException(
                status_code=500,
                detail=f"Could not convert TIFF for display: {exc}",
            ) from exc
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=120"},
        )
    return FileResponse(path, media_type=_media_type(path.suffix))


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


@router.get("/folders/{folder_id}/imaging", response_model=ImagingDocumentResponse)
def get_folder_imaging(
    folder_id: str,
    repo: FolderRepository = Depends(get_repository),
) -> ImagingDocumentResponse:
    """Imaging page/document results (dummy/local JSON until Postgres schema lands)."""
    doc = repo.get_imaging(folder_id)
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
        ".png": "image/png",
        ".webp": "image/webp",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
    }
    return mapping.get(suffix.lower(), "application/octet-stream")
