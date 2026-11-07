"""Charts: list, detail, pages, page images (Azure Blob), OCR, imaging, ground truth, reviews, delete."""

import logging
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from app.store.base import FolderRepository
from app.store.factory import get_repository
from app.core.config import Settings, get_settings
from app.core.schemas import (
    ChartGroundTruthUpdates,
    ChartReview,
    FolderDetail,
    FolderListResponse,
    ExtractionPagesResponse,
    ExtractionReviewResponse,
    ImagingDocumentResponse,
    ImagingManifestDetails,
    OcrTextResponse,
    PageGroundTruth,
    PageSequenceSave,
    EncounterReview,
    EncounterReviewSave,
    ChartGroundTruth,
)
from app.services.folder_list import FolderListParams, build_folder_list
from app.services.extraction_review import (
    load_extraction_pages_from_db,
    load_extraction_review_from_db,
)
from app.services.staged_continuity import load_encounters_from_db
from app.services.imaging_csv import filter_folder, iter_csv_lines
from app.services.chart_run_batch import database_url_usable
from app.services.accuracy_cache import clear_report_cache
from app.services.auth import SignedInUser
from app.services.chart_delete import ChartNotFound, delete_chart
from app.services.ground_truth import (
    GroundTruthSource,
    bulk_ground_truth,
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
    set_chart_review,
)
from app.services.page_images import (
    bytes_to_display_jpeg,
    cached_derived_jpeg,
    is_tiff_name,
)
from app.routers.deps import current_user

logger = logging.getLogger("review_ui.api")
router = APIRouter()


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
    tag: list[str] | None = Query(None, description="Repeatable chart Tag filter ((Blank) = no tag)"),
    reviewed: list[str] | None = Query(None, description="Repeatable Reviewed filter (Yes / No)"),
    truth: list[str] | None = Query(None, description="Repeatable Ground Truth filter (Yes / No)"),
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
        tag=list(tag or []),
        reviewed=list(reviewed or []),
        truth=list(truth or []),
        run_after=run_after,
        sort=sort,  # type: ignore[arg-type]
        sort_dir=sort_dir,  # type: ignore[arg-type]
        limit=limit,
        offset=offset,
    )
    # chart_list only: review-ui reads no chart folders (2026-11-07).
    result = build_folder_list(
        database_url=settings.database_url,
        db_schema=settings.db_schema,
        data_root=None,
        full_local_rows=None,
        params=params,
        metadata_dir=None,
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
        reviewed_options=result.reviewed_options,
        truth_options=result.truth_options,
        tag_options=result.tag_options,
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
    """Serve a page image from Azure Blob (``chart_list.blob_path``, and the Processed
    ``output_path`` when present). TIFF is re-encoded to JPEG — browsers cannot show it raw.
    There is no disk fallback: review-ui reads the database and Blob only (2026-11-07)."""
    cache_hdr = (
        "private, max-age=3600"
        if thumb
        else "private, max-age=300"
    )
    blob_error: str | None = None
    blob_missing = False  # looked in Blob, nothing there (not a sign-in / network failure)
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
        blob_error, blob_missing = f"not in Azure Blob: {missing or 'no blob found'}", True
    # 503 (sign-in / network): the viewer offers "Retry images" (POST /api/blob/retry).
    # 404: the image is not in Blob at all, so a retry cannot help.
    raise HTTPException(
        status_code=404 if blob_missing else 503,
        detail=f"Page image unavailable: {blob_error}",
    )


@router.post("/blob/retry")
def retry_blob(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    """Try Azure Blob again now (after a sign-in failure), and say whether it works."""
    from app.services.blob_store import check_blob_access, reset_blob_access

    reset_blob_access()
    return {"blob": check_blob_access(settings)}


@router.get("/folders/{folder_id}/ocr", response_model=OcrTextResponse)
def get_folder_ocr(
    folder_id: str,
    kind: str = "preliminary",
    repo: FolderRepository = Depends(get_repository),
) -> OcrTextResponse:
    return repo.get_ocr_text(folder_id, kind)


@router.get("/folders/{folder_id}/extraction/pages", response_model=ExtractionPagesResponse)
def get_folder_extraction_pages(
    folder_id: str,
    settings: Settings = Depends(get_settings),
) -> ExtractionPagesResponse:
    """Every page's extraction review in one response, so the Additional tab (Section
    Headers, Page Numbers) is ready on every page as soon as the chart opens. Same sources
    as /extraction: additional_page_details."""
    if not database_url_usable(settings.database_url):
        return ExtractionPagesResponse(folder_id=folder_id, available=False)
    return load_extraction_pages_from_db(settings.database_url, settings.db_schema, folder_id)


@router.get("/folders/{folder_id}/extraction", response_model=ExtractionReviewResponse)
def get_folder_extraction(
    folder_id: str,
    page: str = Query(..., description="Image file name of the page being reviewed."),
    settings: Settings = Depends(get_settings),
) -> ExtractionReviewResponse:
    """Selected key/value extraction for one page. Processed equals extracted.

    Read from the database (additional_page_details): Page Number and Section Headers."""
    if not database_url_usable(settings.database_url):
        return ExtractionReviewResponse(folder_id=folder_id, available=False)
    return load_extraction_review_from_db(
        settings.database_url, settings.db_schema, folder_id, page
    )


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
    # The encounter grouping, from encounter_type_results.
    if not doc.encounters:
        encounters = load_encounters_from_db(settings.database_url, settings.db_schema, folder_id)
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

