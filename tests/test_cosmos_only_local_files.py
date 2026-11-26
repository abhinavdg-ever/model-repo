"""AKS (2026-11-26, user): Cosmos only, no Postgres package; LOCAL_FILES=false keeps nothing on disk."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
REVIEW = REPO / "review-ui" / "backend"

# Makes `import psycopg` / `psycopg_pool` fail, as in an image that does not install them.
BLOCK = textwrap.dedent("""
    import sys
    class _NoPostgres:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in {"psycopg", "psycopg_pool", "psycopg2"}:
                raise ImportError(f"{name} is not installed (Cosmos-only image)")
            return None
    sys.meta_path.insert(0, _NoPostgres())
""")


def _run(code: str, cwd: Path, **env: str) -> subprocess.CompletedProcess:
    full = {**os.environ, "DB_BACKEND": "cosmos", "LOG_TO_FILE": "false", "DATABASE_URL": "",
            "PYTHONPATH": str(cwd), **env}
    return subprocess.run([sys.executable, "-c", BLOCK + textwrap.dedent(code)], cwd=cwd, env=full,
                          capture_output=True, text=True, timeout=300)


def test_core_pipeline_imports_without_postgres():
    out = _run("""
        import apps.orchestrator_api.main, apps.worker.queue_worker, apps.worker.runner, apps.worker.chart_run
        import common.db
        assert not any(m.split(".")[0] in {"psycopg", "psycopg_pool"} for m in sys.modules)
        print("ok")
    """, CORE, RUN_MODE="queue")
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr[-3000:]


def test_review_ui_serves_without_postgres():
    out = _run("""
        from fastapi.testclient import TestClient
        from app.main import app
        with TestClient(app) as client:
            assert client.get("/api/health").status_code == 200
        assert not any(m.split(".")[0] in {"psycopg", "psycopg_pool"} for m in sys.modules)
        print("ok")
    """, REVIEW, COSMOS_ENDPOINT="https://example.invalid:443/", COSMOS_KEY="x")
    assert out.returncode == 0 and "ok" in out.stdout, out.stderr[-3000:]


def _core_path():
    if str(CORE) not in sys.path:
        sys.path.insert(0, str(CORE))


def test_local_files_false_removes_the_chart_folder(monkeypatch, tmp_path):
    _core_path()
    from apps.worker import chart_run
    from apps.worker.jobs import outputs_cleanup

    monkeypatch.setattr(outputs_cleanup, "DATA_ROOT", tmp_path)
    payload = types.SimpleNamespace(is_blob=True, chart_name="chart_x")

    def fake_run(p):
        (tmp_path / "chart_x" / "pages").mkdir(parents=True)
        (tmp_path / "chart_x" / "pages" / "1.jpg").write_bytes(b"x")
        return {"chart_id": 7}

    monkeypatch.setattr(chart_run, "run_blob_chart", fake_run)

    monkeypatch.setenv("LOCAL_FILES", "true")
    assert chart_run.execute_run(payload)["chart_id"] == 7
    assert (tmp_path / "chart_x").is_dir()  # default: kept (purged after OUTPUTS_RETENTION_DAYS)

    import shutil
    shutil.rmtree(tmp_path / "chart_x")
    monkeypatch.setenv("LOCAL_FILES", "false")
    assert chart_run.execute_run(payload)["chart_id"] == 7
    assert not (tmp_path / "chart_x").exists()

    def failing(p):
        (tmp_path / "chart_x").mkdir()
        raise RuntimeError("stage blew up")

    monkeypatch.setattr(chart_run, "run_blob_chart", failing)
    try:
        chart_run.execute_run(payload)
    except RuntimeError:
        pass
    assert not (tmp_path / "chart_x").exists()  # removed on failure too


def test_local_files_false_writes_no_run_stats_or_purge_mark(monkeypatch, tmp_path):
    _core_path()
    from datetime import datetime
    from apps.worker import run_stats, usage
    from common import logging_setup

    monkeypatch.setenv("LOCAL_FILES", "false")
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(usage, "write", lambda *a, **k: None)
    now = datetime.now().astimezone()
    run_stats.record({"chart_name": "c", "id": 1}, {}, started=now, ended=now, status="completed")
    logging_setup.purge_old_logs(force=True)
    assert not (tmp_path / "logs").exists()


def test_review_ui_thumb_cache_off(monkeypatch, tmp_path):
    if str(REVIEW) not in sys.path:
        sys.path.insert(0, str(REVIEW))
    from app.services import page_images

    monkeypatch.setattr(page_images.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setenv("LOCAL_FILES", "false")
    assert page_images.cached_derived_jpeg("stamp-1", lambda: b"jpeg") == b"jpeg"
    assert not any(tmp_path.iterdir())
    monkeypatch.setenv("LOCAL_FILES", "true")
    page_images.cached_derived_jpeg("stamp-1", lambda: b"jpeg")
    assert any(tmp_path.iterdir())
