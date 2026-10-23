"""Admins' user management in review-ui: create a user, or reset a user's password.

The same table and hash format as utilities/create_user.py, but the two actions are
kept apart: creating never touches an existing username (any case), and a reset
only changes the password of a user that exists.
"""

from __future__ import annotations

import re
from typing import Any

from app.services.auth import hash_password
from app.services.db import connection

ROLES = ("reviewer", "admin", "viewer")
MIN_PASSWORD = 8
_USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{1,99}")


class UserError(ValueError):
    """A request the table cannot take; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise UserError(400, f"The password needs at least {MIN_PASSWORD} characters")


def list_users(database_url: str, db_schema: str) -> list[dict[str, Any]]:
    """Every user, without the hash."""
    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.execute("SELECT username, name, role, is_active, last_login_at FROM users ORDER BY lower(username)")
        return [
            {"username": u, "name": n, "role": r, "isActive": bool(a),
             "lastLoginAt": last.isoformat() if last else None}
            for u, n, r, a, last in cur.fetchall()
        ]


def create_user(database_url: str, db_schema: str, *, username: str, name: str, password: str, role: str) -> dict[str, Any]:
    """A new user. 409 when the username is taken in any case."""
    username = (username or "").strip()
    if not _USERNAME.fullmatch(username):
        raise UserError(400, "A username is 2-100 letters, digits, '.', '_', '-' or '@'")
    if role not in ROLES:
        raise UserError(400, f"The role must be one of {', '.join(ROLES)}")
    _check_password(password)
    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.execute("SELECT username FROM users WHERE lower(username) = lower(%s)", (username,))
        taken = cur.fetchone()
        if taken:
            raise UserError(409, f"The username {taken[0]} already exists: reset its password instead")
        cur.execute(
            "INSERT INTO users (username, name, role, password_hash, is_active) VALUES (%s, %s, %s, %s, TRUE)",
            (username, (name or "").strip() or username, role, hash_password(password)),
        )
    return {"username": username, "name": (name or "").strip() or username, "role": role}


def reset_password(database_url: str, db_schema: str, *, username: str, password: str) -> dict[str, Any]:
    """A new password for an existing user. 404 when there is no such user."""
    _check_password(password)
    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET password_hash = %s WHERE lower(username) = lower(%s) RETURNING username, name, role",
            (hash_password(password), (username or "").strip()),
        )
        row = cur.fetchone()
    if row is None:
        raise UserError(404, f"No user named {username}: create it instead")
    return {"username": row[0], "name": row[1], "role": row[2]}
