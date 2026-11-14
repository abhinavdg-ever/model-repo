"""review-ui admin switch between Postgres and Cosmos (2026-11-14, user): one request may ask for
the other store; only an admin's request, or a read with no token (a page image), gets it."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import DbBackendSwitch
from app.services import backend
from app.services.auth import SignedInUser, issue_token


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(backend, "available", lambda settings=None: ["postgres", "cosmos"])
    monkeypatch.setattr(backend, "configured_mode", lambda: "postgres")
    app = FastAPI()

    @app.get("/mode")
    def read_mode():  # a sync route: runs in the thread pool, as most review-ui routes do
        return {"mode": backend.mode(), "cosmos": backend.reads_cosmos()}

    @app.post("/mode")
    def write_mode():
        return {"mode": backend.mode()}

    app.add_middleware(DbBackendSwitch)
    return TestClient(app)


def _token(role):
    return issue_token(get_settings(), SignedInUser(id=1, username="u", name="U", role=role))


def test_an_admin_can_switch_one_request(client):
    r = client.get("/mode", headers={"X-DB-Backend": "cosmos", "Authorization": f"Bearer {_token('admin')}"})
    assert r.json() == {"mode": "cosmos", "cosmos": True} and r.headers["x-db-backend"] == "cosmos"
    # The next request without the switch is back on the deployment's store.
    assert client.get("/mode").json()["mode"] == "postgres"


def test_a_reviewer_cannot_switch(client):
    r = client.get("/mode", headers={"X-DB-Backend": "cosmos", "Authorization": f"Bearer {_token('reviewer')}"})
    assert r.json()["mode"] == "postgres" and r.headers["x-db-backend"] == "postgres"


def test_a_page_image_follows_the_cookie_but_a_tokenless_write_does_not(client):
    client.cookies.set("db_backend", "cosmos")
    assert client.get("/mode").json()["mode"] == "cosmos"
    assert client.post("/mode").json()["mode"] == "postgres"


def test_an_unknown_or_unavailable_store_is_ignored(client, monkeypatch):
    admin = {"Authorization": f"Bearer {_token('admin')}"}
    assert client.get("/mode", headers={**admin, "X-DB-Backend": "mysql"}).json()["mode"] == "postgres"
    monkeypatch.setattr(backend, "available", lambda settings=None: ["postgres"])
    assert client.get("/mode", headers={**admin, "X-DB-Backend": "cosmos"}).json()["mode"] == "postgres"


def test_postgres_on_a_both_deployment_keeps_the_cosmos_copy(monkeypatch):
    monkeypatch.setattr(backend, "configured_mode", lambda: "both")
    token = backend.use("postgres")
    try:
        assert backend.mode() == "both"
    finally:
        backend.reset(token)
    token = backend.use("cosmos")
    try:
        assert backend.mode() == "cosmos"
    finally:
        backend.reset(token)
    assert backend.mode() == "both"


def test_each_store_has_its_own_repository(monkeypatch):
    from app.store import factory

    made = []
    monkeypatch.setattr(factory, "build_repository", lambda settings, production=None, kind=None: made.append(kind) or object())
    factory.repository_for.cache_clear()
    try:
        monkeypatch.setattr(backend, "configured_mode", lambda: "postgres")
        a = factory._repository()
        token = backend.use("cosmos")
        monkeypatch.setattr(backend, "available", lambda settings=None: ["postgres", "cosmos"])
        try:
            b = factory._repository()
        finally:
            backend.reset(token)
        assert a is not b and made == ["postgres", "cosmos"] and factory._repository() is a
    finally:
        factory.repository_for.cache_clear()
