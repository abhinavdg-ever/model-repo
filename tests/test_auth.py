"""review-ui sign-in (app/services/auth.py) and utilities/create_user.py's hash format."""
from __future__ import annotations

import importlib.util

from app.core.config import Settings
from app.services import auth


def settings(**kw) -> Settings:
    kw.setdefault("auth_secret", "test-secret")
    return Settings(database_url="", **kw)


def test_a_password_hash_verifies_and_never_holds_the_password() -> None:
    stored = auth.hash_password("correct horse", iterations=1000)
    assert "correct horse" not in stored and stored.startswith("pbkdf2_sha256$1000$")
    assert auth.verify_password("correct horse", stored)
    assert not auth.verify_password("wrong", stored)
    assert not auth.verify_password("correct horse", None)
    assert not auth.verify_password("correct horse", "plain-text")


def test_create_user_writes_the_hash_review_ui_checks() -> None:
    spec = importlib.util.spec_from_file_location(
        "create_user", auth.__file__.replace("review-ui/backend/app/services/auth.py", "utilities/create_user.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    module.ITERATIONS = 1000
    assert auth.verify_password("s3cret-pass", module.hash_password("s3cret-pass"))


def test_a_token_carries_the_user_and_rejects_tampering_and_expiry() -> None:
    s = settings()
    user = auth.SignedInUser(id=7, username="simran.priya", name="Simran Priya")
    token = auth.issue_token(s, user)
    assert auth.read_token(s, token) == user
    body, sig = token.rsplit(".", 1)
    assert auth.read_token(s, f"{body}x.{sig}") is None
    assert auth.read_token(settings(auth_secret="other"), token) is None
    assert auth.read_token(s, None) is None
    expired = auth.issue_token(settings(auth_session_hours=-1), user)
    assert auth.read_token(s, expired) is None


def test_without_a_database_only_the_configured_account_signs_in() -> None:
    off = settings()
    assert auth.authenticate(off, "anyone", "anything", ip_address=None, user_agent=None) is None
    on = settings(login_username="local-admin", login_password="local-pass-123")
    user = auth.authenticate(on, "local-admin", "local-pass-123", ip_address="1.2.3.4", user_agent="t")
    assert user is not None and user.id is None
    assert auth.authenticate(on, "local-admin", "nope", ip_address=None, user_agent=None) is None


def test_the_configured_account_signs_in_without_touching_the_database(monkeypatch) -> None:
    def no_db(*a, **k):
        raise AssertionError("the configured account must not query users")

    monkeypatch.setattr(auth, "connection", no_db)
    s = Settings(database_url="postgresql://u:p@db.invalid/x", auth_secret="t",
                 login_username="local-admin", login_password="local-pass-123")
    user = auth.authenticate(s, "local-admin", "local-pass-123", ip_address=None, user_agent=None)
    assert user is not None and user.id is None and user.role == "admin"


def test_saving_ground_truth_needs_a_signed_in_user(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.api import routes
    from app.core.config import get_settings
    from app.main import app

    s = Settings(database_url="postgresql://u:p@db.example:5432/x", auth_secret="test-secret")
    app.dependency_overrides[get_settings] = lambda: s
    saved: dict = {}

    def fake_upsert(database_url, folder_id, body, db_schema="public", *, user_id=None):
        saved["user_id"] = user_id
        return body

    monkeypatch.setattr(routes, "upsert_page_ground_truth", fake_upsert)
    monkeypatch.setattr(routes, "database_url_usable", lambda url: True)
    monkeypatch.setattr(routes, "clear_report_cache", lambda: None)
    try:
        client = TestClient(app)
        assert client.post("/api/folders/c1/ground-truth", json={"pageNumber": 1}).status_code == 401
        token = auth.issue_token(s, auth.SignedInUser(id=42, username="ajit.solanki", name="Ajit Solanki"))
        ok = client.post(
            "/api/folders/c1/ground-truth", json={"pageNumber": 1}, headers={"Authorization": f"Bearer {token}"}
        )
        assert ok.status_code == 200 and saved["user_id"] == 42
    finally:
        app.dependency_overrides.clear()


def test_only_an_admin_can_delete_a_chart(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.api import routes
    from app.core.config import get_settings
    from app.main import app

    s = Settings(database_url="postgresql://u:p@db.example:5432/x", auth_secret="test-secret")
    app.dependency_overrides[get_settings] = lambda: s
    deleted: dict = {}

    def fake_delete(database_url, chart_name, *, db_schema="public", user_id=None):
        deleted.update(chart=chart_name, user_id=user_id)
        return {"chart_name": chart_name, "page_count": 3}

    monkeypatch.setattr(routes, "delete_chart", fake_delete)
    monkeypatch.setattr(routes, "database_url_usable", lambda url: True)
    monkeypatch.setattr(routes, "clear_report_cache", lambda: None)

    def bearer(role: str) -> dict:
        token = auth.issue_token(s, auth.SignedInUser(id=9, username="u", name="U", role=role))
        return {"Authorization": f"Bearer {token}"}

    try:
        client = TestClient(app)
        assert client.delete("/api/folders/c1").status_code == 401
        assert client.delete("/api/folders/c1", headers=bearer("reviewer")).status_code == 403
        assert not deleted
        ok = client.delete("/api/folders/c1", headers=bearer("admin"))
        assert ok.status_code == 200 and deleted == {"chart": "c1", "user_id": 9}
    finally:
        app.dependency_overrides.clear()
