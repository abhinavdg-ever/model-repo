"""The accuracy report."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from app.core.config import Settings, get_settings
from app.core.schemas import AccuracyReportResponse
from app.services.chart_run_batch import database_url_usable
from app.services.accuracy_cache import load_report
from app.services.ground_truth import GroundTruthSource

logger = logging.getLogger("review_ui.api")
router = APIRouter()


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

