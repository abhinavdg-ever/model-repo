"""Sign-in and user administration."""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from app.core.config import Settings, get_settings
from app.core.schemas import (
    LoginRequest,
    LoginResponse,
    SignedInUserResponse,
    NewUser,
    PasswordReset,
)
from app.services import user_admin
from app.services.auth import SignedInUser, authenticate, issue_token
from app.routers.deps import _admin_with_db, current_user

logger = logging.getLogger("review_ui.api")
router = APIRouter()


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


def _user_call(action, *args, **kwargs):
    try:
        return action(*args, **kwargs)
    except user_admin.UserError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except Exception as exc:
        logger.warning("user admin failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not reach the users table") from exc


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

