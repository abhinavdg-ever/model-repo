"""A user's Review Log."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from app.core.config import Settings, get_settings
from app.core.schemas import ReviewLogResponse
from app.services.backend import store_usable
from app.services.chart_run_batch import database_url_usable
from app.services.auth import SignedInUser
from app.services.chart_review import review_log
from app.routers.deps import current_user

logger = logging.getLogger("review_ui.api")
router = APIRouter()


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
    if not store_usable(settings, database_url_usable):
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

