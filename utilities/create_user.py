#!/usr/bin/env python3
"""Create a review-ui user, or set a user's password (upsert on username).

The password is asked for (not echoed) and stored only as a salted PBKDF2-SHA256 hash, in
the format review-ui checks (review-ui/backend/app/services/auth.py). Re-running for an
existing username replaces its password, name and role.

Examples::

    cd core-pipeline && source .venv/bin/activate
    python ../utilities/create_user.py simran.priya --name "Simran Priya"
    python ../utilities/create_user.py ajit.solanki --name "Ajit Solanki" --role admin

    # Several at once from a CSV (username,name,password[,role]); keep that file out of git
    python ../utilities/create_user.py --from-csv /secure/place/users.csv

    # No database here: print the SQL to run with psql instead
    python ../utilities/create_user.py simran.priya --sql > create_users.sql

Needs DATABASE_URL (env or core-pipeline/.env) and, on an existing database,
schema/changes_2026-10-19.sql (the users table).
"""
from __future__ import annotations

import argparse
import base64
import csv
import getpass
import hashlib
import secrets
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

try:
    from dotenv import load_dotenv

    load_dotenv(CORE / ".env")
except ImportError:
    pass

# Same as review-ui/backend/app/services/auth.py (PBKDF2_ITERATIONS, the hash format).
ITERATIONS = 600_000
ROLES = ("reviewer", "admin", "viewer")

UPSERT = """
INSERT INTO users (username, name, role, password_hash, is_active)
VALUES (%s, %s, %s, %s, TRUE)
ON CONFLICT (username) DO UPDATE SET
    name = EXCLUDED.name,
    role = EXCLUDED.role,
    password_hash = EXCLUDED.password_hash,
    is_active = TRUE
RETURNING id, (xmax = 0) AS inserted
"""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${_b64(salt)}${_b64(digest)}"


def _sql_literal(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _users_from_args(args: argparse.Namespace) -> list[tuple[str, str, str, str]]:
    """(username, name, role, password) for every user asked for."""
    if args.from_csv:
        out = []
        with Path(args.from_csv).expanduser().open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                username = (row.get("username") or "").strip()
                password = row.get("password") or ""
                if not username or not password:
                    continue
                name = (row.get("name") or "").strip() or username
                role = (row.get("role") or "").strip() or args.role
                out.append((username, name, role, password))
        return out
    if not args.username:
        raise SystemExit("give a username, or --from-csv")
    password = getpass.getpass(f"Password for {args.username}: ")
    if password != getpass.getpass("Again: "):
        raise SystemExit("the passwords differ")
    return [(args.username, args.name or args.username, args.role, password)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("username", nargs="?", help="Sign-in name, e.g. simran.priya")
    parser.add_argument("--name", help="Display name (default: the username)")
    parser.add_argument("--role", default="reviewer", choices=ROLES)
    parser.add_argument("--from-csv", help="CSV with username,name,password[,role] columns")
    parser.add_argument("--sql", action="store_true", help="Print SQL for psql instead of writing")
    args = parser.parse_args(argv)

    users = _users_from_args(args)
    for username, _name, role, password in users:
        if role not in ROLES:
            raise SystemExit(f"{username}: role must be one of {', '.join(ROLES)}")
        if len(password) < 8:
            raise SystemExit(f"{username}: use a password of at least 8 characters")

    if args.sql:
        print("-- review-ui users (passwords as PBKDF2 hashes). Run: psql \"$DATABASE_URL\" -f <this file>")
        for username, name, role, password in users:
            print(
                "INSERT INTO users (username, name, role, password_hash, is_active) VALUES ("
                f"{_sql_literal(username)}, {_sql_literal(name)}, {_sql_literal(role)}, "
                f"{_sql_literal(hash_password(password))}, TRUE)\n"
                "ON CONFLICT (username) DO UPDATE SET name = EXCLUDED.name, role = EXCLUDED.role,\n"
                "    password_hash = EXCLUDED.password_hash, is_active = TRUE;"
            )
        return 0

    from db import connect

    with connect() as conn:
        for username, name, role, password in users:
            row = conn.execute(UPSERT, (username, name, role, hash_password(password))).fetchone()
            print(f"{'created' if row['inserted'] else 'updated'} {username} (id {row['id']}, {role})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
