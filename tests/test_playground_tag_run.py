"""review-ui Playground Tag Run (2026-11-22, user): an admin runs every chart with the tag(s),
across runs and batches, through core-pipeline's batch-run-tag."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services import auth, playground

TAGS = {"c1": ["Pilot-Nov"], "c2": ["pilot-nov", "Review"], "c3": ["Review"], "c4": ["Pilot Nov"]}


def test_tagged_charts_any_or_all_case_insensitive():
    assert playground.tagged_charts(TAGS, ["PILOT-NOV"]) == ["c1", "c2"]
    assert playground.tagged_charts(TAGS, ["pilot-nov", "review"]) == ["c1", "c2", "c3"]
    assert playground.tagged_charts(TAGS, ["pilot-nov", "review"], match_all=True) == ["c2"]
    assert playground.tagged_charts(TAGS, [" "]) == []


@pytest.fixture
def api(monkeypatch):
    from fastapi.testclient import TestClient

    import app.routers.playground as router
    from app.core.config import get_settings
    from app.main import app

    s = Settings(database_url="", auth_secret="test-secret", core_pipeline_urls="VM=http://pipeline:8001")
    app.dependency_overrides[get_settings] = lambda: s
    sent: dict = {}

    def fake_start(base_url, body, api_key="", path="/api/charts/batch-run"):
        sent.update(base_url=base_url, body=body, path=path)
        return {"status": "queued", "charts_queued": 2}

    monkeypatch.setattr(playground, "start_batch", fake_start)
    monkeypatch.setattr(router, "chart_tags_map", lambda *a, **k: dict(TAGS))

    def bearer(role: str) -> dict:
        token = auth.issue_token(s, auth.SignedInUser(id=9, username="u", name="U", role=role))
        return {"Authorization": f"Bearer {token}"}

    yield TestClient(app), sent, bearer
    app.dependency_overrides.clear()


def test_tags_list_for_admins_merges_case(api):
    client, _sent, bearer = api
    assert client.get("/api/playground/tags").status_code == 401
    assert client.get("/api/playground/tags", headers=bearer("reviewer")).status_code == 403
    tags = client.get("/api/playground/tags", headers=bearer("admin")).json()["tags"]
    by = {t["tag"].casefold(): t["charts"] for t in tags}
    assert by["pilot-nov"] == ["c1", "c2"] and by["review"] == ["c2", "c3"] and by["pilot nov"] == ["c4"]


def test_tag_run_goes_to_batch_run_tag(api):
    client, sent, bearer = api
    run = {"target": "VM", "tags": ["Pilot-Nov"]}
    assert client.post("/api/playground/tag-run", json=run).status_code == 401
    assert client.post("/api/playground/tag-run", json=run, headers=bearer("reviewer")).status_code == 403
    ok = client.post("/api/playground/tag-run", json=run, headers=bearer("admin"))
    assert ok.status_code == 202 and ok.json()["charts"] == ["c1", "c2"]
    assert sent["path"] == "/api/charts/batch-run-tag" and sent["base_url"] == "http://pipeline:8001"
    assert sent["body"] == {"tags": ["Pilot-Nov"], "match": "any", "skip_ocr": True, "skip_completed": False}
    # A string splits on commas only: a tag may hold a space.
    spaced = client.post("/api/playground/tag-run", json={**run, "tags": "Pilot Nov"}, headers=bearer("admin"))
    assert spaced.status_code == 202 and spaced.json()["charts"] == ["c4"]
    both = client.post("/api/playground/tag-run", json={**run, "tags": "pilot-nov, review", "match": "all"},
                       headers=bearer("admin"))
    assert both.status_code == 202 and both.json()["charts"] == ["c2"] and sent["body"]["match"] == "all"


def test_tag_run_refuses_none_unknown_and_too_many(api, monkeypatch):
    client, _sent, bearer = api
    admin = bearer("admin")
    assert client.post("/api/playground/tag-run", json={"target": "VM", "tags": []}, headers=admin).status_code == 400
    assert client.post("/api/playground/tag-run", json={"target": "VM", "tags": ["nope"]}, headers=admin).status_code == 404
    assert client.post("/api/playground/tag-run", json={"target": "VM", "tags": ["x"], "match": "some"},
                       headers=admin).status_code == 422
    import app.routers.playground as router
    monkeypatch.setattr(router, "chart_tags_map", lambda *a, **k: {f"c{n}": ["Big"] for n in range(11)})
    many = client.post("/api/playground/tag-run", json={"target": "VM", "tags": ["big"]}, headers=admin)
    assert many.status_code == 400 and "at most 10" in many.json()["detail"]
