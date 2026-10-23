"""review-ui Playground: an admin starts a core-pipeline batch run over a blob folder."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services import auth, playground


@pytest.mark.parametrize("given, output", [
    ("Raw_Input/Run2/Batch6/DEID_PNGs", "Processed/Run2/Batch6"),
    ("/Raw_Input/Run2/Batch6/DEID_PNGs/", "Processed/Run2/Batch6"),
    ("raw_input\\Run1\\Batch1\\pngs", "Processed/Run1/Batch1"),
    ("Other/Run3/pngs", "Processed/Other/Run3"),
])
def test_the_output_folder_follows_from_the_input(given, output):
    assert playground.output_path_for(given) == output


def test_chart_names_split_on_commas_spaces_and_lines():
    assert playground.chart_names("a, b\nc  a") == ["a", "b", "c"]
    assert playground.chart_names(None) == []


def test_only_an_admin_starts_a_run_and_the_output_is_derived(monkeypatch):
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import app

    s = Settings(database_url="", auth_secret="test-secret", core_pipeline_url="http://pipeline:8001")
    app.dependency_overrides[get_settings] = lambda: s
    sent: dict = {}

    def fake_start(base_url, body):
        sent.update(base_url=base_url, body=body)
        return {"status": "accepted"}

    monkeypatch.setattr(playground, "start_batch", fake_start)

    def bearer(role: str) -> dict:
        token = auth.issue_token(s, auth.SignedInUser(id=9, username="u", name="U", role=role))
        return {"Authorization": f"Bearer {token}"}

    run = {"input_path": "Raw_Input/Run2/Batch6/DEID_PNGs", "chart_list": "c1, c2"}
    try:
        client = TestClient(app)
        assert client.post("/api/playground/batch-run", json=run).status_code == 401
        assert client.post("/api/playground/batch-run", json=run, headers=bearer("reviewer")).status_code == 403
        ok = client.post("/api/playground/batch-run", json=run, headers=bearer("admin"))
        assert ok.status_code == 202
        assert sent["base_url"] == "http://pipeline:8001"
        # Defaults: OCR runs and finished charts re-run.
        assert sent["body"] == {
            "input_type": "blob", "container_name": "imaging-pipeline",
            "input_path": "Raw_Input/Run2/Batch6/DEID_PNGs", "output_path": "Processed/Run2/Batch6",
            "skip_ocr": False, "skip_completed": False, "chart_list": ["c1", "c2"],
        }
        assert ok.json()["pipeline"] == {"status": "accepted"}
        # An output path cannot be sent: it is always derived.
        bad = client.post("/api/playground/batch-run", json={**run, "output_path": "x"}, headers=bearer("admin"))
        assert bad.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_without_core_pipeline_url_the_playground_is_off(monkeypatch):
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import app

    s = Settings(database_url="", auth_secret="test-secret", core_pipeline_url="")
    app.dependency_overrides[get_settings] = lambda: s
    token = auth.issue_token(s, auth.SignedInUser(id=9, username="u", name="U", role="admin"))
    try:
        reply = TestClient(app).post(
            "/api/playground/batch-run", json={"input_path": "Raw_Input/R/B/p"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert reply.status_code == 503
    finally:
        app.dependency_overrides.clear()
