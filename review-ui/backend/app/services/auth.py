"""review-ui sign-in: users in Postgres, a signed session token, who did what.

Passwords are stored only as ``pbkdf2_sha256$<iterations>$<salt>$<hash>`` (standard
library, no extra package); utilities/create_user.py writes the same format. A sign-in
records ``users.last_login_at / _ip / _user_agent``, bumps ``login_count`` and appends a
``user_login`` row. The session token is ``<payload>.<HMAC-SHA256>`` with the user's id,
username, name and expiry, signed with ``AUTH_SECRET``; no session table.

The LOGIN_USERNAME / LOGIN_PASSWORD account from review-ui/.env (when both are set) signs
in with or without a database, as an admin with no user id: a local sign-in that needs no
users row. Its sign-ins are not recorded.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from app.core.config import Settings
from app.services.chart_run_batch import database_url_usable
from app.services.db import connection

logger = logging.getLogger("review_ui.auth")

PBKDF2_ITERATIONS = 600_000
_ALGORITHM = "pbkdf2_sha256"

# Used when AUTH_SECRET is empty: sign-ins last until the process restarts.
_PROCESS_SECRET = secrets.token_bytes(32)
_warned_secret = False


@dataclass(frozen=True)
class SignedInUser:
    id: Optional[int]
    username: str
    name: str
    role: str = "reviewer"


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algorithm, iterations, salt, digest = (stored or "").split("$")
        if algorithm != _ALGORITHM:
            return False
        got = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _unb64(salt), int(iterations))
        return hmac.compare_digest(got, _unb64(digest))
    except (ValueError, TypeError):
        return False


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _secret(settings: Settings) -> bytes:
    global _warned_secret
    if settings.auth_secret:
        return settings.auth_secret.encode("utf-8")
    if not _warned_secret:
        logger.warning("AUTH_SECRET is not set: sign-ins end when review-ui restarts")
        _warned_secret = True
    return _PROCESS_SECRET


def issue_token(settings: Settings, user: SignedInUser) -> str:
    payload = {
        "uid": user.id,
        "usr": user.username,
        "name": user.name,
        "role": user.role,
        "exp": int(time.time() + settings.auth_session_hours * 3600),
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(_secret(settings), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(settings: Settings, token: str | None) -> Optional[SignedInUser]:
    """The signed-in user, or None for a missing, forged or expired token."""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    want = _b64(hmac.new(_secret(settings), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, want):
        return None
    try:
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp") or 0) < time.time():
        return None
    uid = payload.get("uid")
    return SignedInUser(
        id=int(uid) if uid is not None else None,
        username=str(payload.get("usr") or ""),
        name=str(payload.get("name") or payload.get("usr") or ""),
        role=str(payload.get("role") or "reviewer"),
    )


_DUMMY: list[str] = []


def _dummy_hash() -> str:
    if not _DUMMY:
        _DUMMY.append(hash_password(secrets.token_hex(8)))
    return _DUMMY[0]


def authenticate(
    settings: Settings,
    username: str,
    password: str,
    *,
    ip_address: str | None,
    user_agent: str | None,
) -> Optional[SignedInUser]:
    """Check the password; on success record the sign-in. None = wrong name or password."""
    username = (username or "").strip()
    if not username or not password:
        return None
    if (
        settings.login_username
        and settings.login_password
        and hmac.compare_digest(username, settings.login_username)
        and hmac.compare_digest(password, settings.login_password)
    ):
        return SignedInUser(id=None, username=username, name=username, role="admin")
    if not database_url_usable(settings.database_url):
        return None

    with connection(settings.database_url, settings.db_schema) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, name, role, password_hash, is_active
                  FROM users
                 WHERE lower(username) = lower(%s)
                """,
                (username,),
            )
            row = cur.fetchone()
            if row is None:
                # Spend the same time as a real check, so a wrong name and a wrong
                # password cannot be told apart by the reply time.
                verify_password(password, _dummy_hash())
                return None
            user_id, name_db, display, role, stored, active = row
            if not active or not verify_password(password, stored):
                return None
            cur.execute(
                """
                UPDATE users
                   SET last_login_at = now(), last_login_ip = %s,
                       last_login_user_agent = %s, login_count = login_count + 1
                 WHERE id = %s
                """,
                (ip_address, user_agent, user_id),
            )
            cur.execute(
                "INSERT INTO user_login (user_id, ip_address, user_agent) VALUES (%s, %s, %s)",
                (user_id, ip_address, user_agent),
            )
    return SignedInUser(id=int(user_id), username=str(name_db), name=str(display or name_db), role=str(role))
