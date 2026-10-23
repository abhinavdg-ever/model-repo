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


def test_chart_names_split_on_commas_spaces_and_lines_and_need_no_quotes():
    assert playground.chart_names("a, b\nc  a") == ["a", "b", "c"]
    assert playground.chart_names('["a", "b"]') == playground.chart_names("'a','b'") == ["a", "b"]
    assert playground.chart_names(None) == []


def test_only_an_admin_starts_a_run_and_the_output_is_derived(monkeypatch):
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import app

    s = Settings(database_url="", auth_secret="test-secret", core_pipeline_urls="VM=http://pipeline:8001, Local=http://localhost:8001/")
    app.dependency_overrides[get_settings] = lambda: s
    sent: dict = {}

    def fake_start(base_url, body):
        sent.update(base_url=base_url, body=body)
        return {"status": "accepted"}

    monkeypatch.setattr(playground, "start_batch", fake_start)

    def bearer(role: str) -> dict:
        token = auth.issue_token(s, auth.SignedInUser(id=9, username="u", name="U", role=role))
        return {"Authorization": f"Bearer {token}"}

    run = {"target": "VM", "input_path": "Raw_Input/Run2/Batch6/DEID_PNGs", "chart_list": "c1, c2"}
    try:
        client = TestClient(app)
        assert client.post("/api/playground/batch-run", json=run).status_code == 401
        assert client.post("/api/playground/batch-run", json=run, headers=bearer("reviewer")).status_code == 403
        ok = client.post("/api/playground/batch-run", json=run, headers=bearer("admin"))
        assert ok.status_code == 202
        assert sent["base_url"] == "http://pipeline:8001"
        # Defaults: OCR reused (no Final OCR 2 bill) and finished charts re-run.
        assert sent["body"] == {
            "input_type": "blob", "container_name": "imaging-pipeline",
            "input_path": "Raw_Input/Run2/Batch6/DEID_PNGs", "output_path": "Processed/Run2/Batch6",
            "skip_ocr": True, "skip_completed": False, "chart_list": ["c1", "c2"],
        }
        assert ok.json()["target_label"] == "VM"
        assert ok.json()["pipeline"] == {"status": "accepted"}
        # The other pipeline by URL or label; an address not configured, or none, is refused.
        other = client.post("/api/playground/batch-run", json={**run, "target": "Local"}, headers=bearer("admin"))
        assert other.status_code == 202 and sent["base_url"] == "http://localhost:8001"
        blank = client.post("/api/playground/batch-run", json={**run, "target": " "}, headers=bearer("admin"))
        assert blank.status_code == 400
        missing = {k: v for k, v in run.items() if k != "target"}
        assert client.post("/api/playground/batch-run", json=missing, headers=bearer("admin")).status_code == 422
        # Charts are required, at most 10, and "At most" (sample) is gone.
        no_charts = {k: v for k, v in run.items() if k != "chart_list"}
        assert client.post("/api/playground/batch-run", json=no_charts, headers=bearer("admin")).status_code == 422
        empty = client.post("/api/playground/batch-run", json={**run, "chart_list": " , "}, headers=bearer("admin"))
        assert empty.status_code == 400
        eleven = ",".join(f"c{n}" for n in range(11))
        many = client.post("/api/playground/batch-run", json={**run, "chart_list": eleven}, headers=bearer("admin"))
        assert many.status_code == 400 and "at most 10" in many.json()["detail"]
        ten = client.post("/api/playground/batch-run", json={**run, "chart_list": eleven.rsplit(",", 1)[0]},
                          headers=bearer("admin"))
        assert ten.status_code == 202 and len(sent["body"]["chart_list"]) == 10
        sampled = client.post("/api/playground/batch-run", json={**run, "sample": 3}, headers=bearer("admin"))
        assert sampled.status_code == 422
        evil = client.post("/api/playground/batch-run", json={**run, "target": "http://elsewhere:80"},
                           headers=bearer("admin"))
        assert evil.status_code == 400
        # An output path cannot be sent: it is always derived.
        bad = client.post("/api/playground/batch-run", json={**run, "output_path": "x"}, headers=bearer("admin"))
        assert bad.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_by_default_the_vm_then_local_pipeline_are_offered():
    assert Settings(database_url="").playground_targets == [
        {"label": "VM", "url": "http://172.20.4.170:8001"}, {"label": "Local", "url": "http://localhost:8001"}]
    # A bare URL is Local when it names this machine, else VM.
    assert Settings(database_url="", core_pipeline_urls="http://localhost:8001/, http://172.20.4.170:8001").playground_targets == [
        {"label": "Local", "url": "http://localhost:8001"}, {"label": "VM", "url": "http://172.20.4.170:8001"}]


def test_without_pipelines_the_playground_is_off(monkeypatch):
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import app

    s = Settings(database_url="", auth_secret="test-secret", core_pipeline_urls="")
    app.dependency_overrides[get_settings] = lambda: s
    token = auth.issue_token(s, auth.SignedInUser(id=9, username="u", name="U", role="admin"))
    try:
        reply = TestClient(app).post(
            "/api/playground/batch-run", json={"target": "VM", "input_path": "Raw_Input/R/B/p", "chart_list": "c1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert reply.status_code == 503
    finally:
        app.dependency_overrides.clear()
