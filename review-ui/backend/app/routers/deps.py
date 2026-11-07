"""Shared by several routers: the signed-in user and the admin + database guard."""

import logging
from fastapi import Depends, HTTPException, Request
from app.core.config import Settings, get_settings
from app.services.chart_run_batch import database_url_usable
from app.services.auth import SignedInUser, read_token

logger = logging.getLogger("review_ui.api")


def current_user(request: Request, settings: Settings = Depends(get_settings)) -> SignedInUser | None:
    """The user of the request's ``Authorization: Bearer`` token, or None."""
    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return read_token(settings, token)


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

