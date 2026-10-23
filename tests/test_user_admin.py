"""review-ui Users page: an admin creates a user (never over an existing one) or resets a password."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.services import auth, user_admin


class FakeUsers:
    """Just enough of the users table for the three statements user_admin runs."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {"ajit.solanki": {"username": "Ajit.Solanki", "name": "Ajit", "role": "admin",
                                                       "hash": "x", "active": True}}
        self._result: list = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params=()):
        text = " ".join(sql.split())
        if text.startswith("SELECT username FROM users"):
            row = self.rows.get(params[0].lower())
            self._result = [(row["username"],)] if row else []
        elif text.startswith("INSERT INTO users"):
            username, name, role, hashed = params
            self.rows[username.lower()] = {"username": username, "name": name, "role": role, "hash": hashed,
                                           "active": True}
        elif text.startswith("UPDATE users SET password_hash"):
            hashed, username = params
            row = self.rows.get(username.lower())
            if row:
                row["hash"] = hashed
            self._result = [(row["username"], row["name"], row["role"])] if row else []
        elif text.startswith("SELECT username, name, role, is_active"):
            self._result = [(r["username"], r["name"], r["role"], r["active"], None) for r in self.rows.values()]

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


@pytest.fixture()
def table(monkeypatch):
    fake = FakeUsers()

    @contextmanager
    def connection(url, schema):
        yield fake

    monkeypatch.setattr(user_admin, "connection", connection)
    return fake


def test_create_never_overwrites_an_existing_username_in_any_case(table):
    with pytest.raises(user_admin.UserError) as taken:
        user_admin.create_user("db", "public", username="AJIT.SOLANKI", name="", password="long-enough", role="reviewer")
    assert taken.value.status == 409
    made = user_admin.create_user("db", "public", username="testing-user", name="", password="testing@2026",
                                  role="reviewer")
    assert made == {"username": "testing-user", "name": "testing-user", "role": "reviewer"}
    assert auth.verify_password("testing@2026", table.rows["testing-user"]["hash"])


def test_reset_only_changes_an_existing_users_password(table):
    with pytest.raises(user_admin.UserError) as missing:
        user_admin.reset_password("db", "public", username="nobody", password="long-enough")
    assert missing.value.status == 404
    user_admin.reset_password("db", "public", username="ajit.solanki", password="new-password")
    row = table.rows["ajit.solanki"]
    assert auth.verify_password("new-password", row["hash"]) and row["role"] == "admin"


def test_short_passwords_and_odd_usernames_are_refused(table):
    for kwargs in ({"username": "ok.name", "password": "short"}, {"username": "has space", "password": "long-enough"}):
        with pytest.raises(user_admin.UserError) as bad:
            user_admin.create_user("db", "public", name="", role="reviewer", **kwargs)
        assert bad.value.status == 400


def test_only_an_admin_reaches_the_user_routes(monkeypatch, table):
    from fastapi.testclient import TestClient

    from app.api import routes
    from app.core.config import get_settings
    from app.main import app

    s = Settings(database_url="postgresql://u:p@db.example:5432/x", auth_secret="test-secret")
    app.dependency_overrides[get_settings] = lambda: s
    monkeypatch.setattr(routes, "database_url_usable", lambda url: True)

    def bearer(role: str) -> dict:
        token = auth.issue_token(s, auth.SignedInUser(id=9, username="u", name="U", role=role))
        return {"Authorization": f"Bearer {token}"}

    new = {"username": "testing-user", "password": "testing@2026"}
    try:
        client = TestClient(app)
        assert client.post("/api/users", json=new).status_code == 401
        assert client.post("/api/users", json=new, headers=bearer("reviewer")).status_code == 403
        assert client.post("/api/users", json=new, headers=bearer("admin")).status_code == 201
        assert client.post("/api/users", json=new, headers=bearer("admin")).status_code == 409
        reset = client.put("/api/users/password", json={"username": "nobody", "password": "long-enough"},
                           headers=bearer("admin"))
        assert reset.status_code == 404
        listed = client.get("/api/users", headers=bearer("admin")).json()
        assert {u["username"] for u in listed} == {"Ajit.Solanki", "testing-user"}
        assert all("hash" not in u and "password" not in str(u).lower() for u in listed)
    finally:
        app.dependency_overrides.clear()
