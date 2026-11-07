"""Page annotations."""

import logging
from fastapi import APIRouter
from app.core.config import ROOT_DIR
from app.core.schemas import (
    AnnotationListResponse,
    AnnotationRow,
    AnnotationSaveRequest,
)
from app.services.annotations import annotation_file, load_annotations, save_annotation

logger = logging.getLogger("review_ui.api")
router = APIRouter()


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

