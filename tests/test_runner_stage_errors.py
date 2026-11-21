"""The runner (2026-11-21, user): a stage that raises does not stop the chain -- the later stages
still run and the chart ends needs_review, not failed; and a run is processing from its start,
not "completed" from an earlier run's stage rows."""
from contextlib import contextmanager

import pytest


@pytest.fixture
def chain(monkeypatch):
    import apps.worker.runner as runner
    import apps.worker.run_stats as run_stats
    import apps.worker.stages.utilities.download_blob as download_blob
    import apps.worker.jobs.outputs_cleanup as outputs_cleanup
    import modules.ocr.reuse as reuse

    calls: list[str] = []
    statuses: list[str] = []

    def ok(name):
        def fn(chart_id, force=True):
            calls.append(name)
            return {"ok": True}
        return fn

    def boom(chart_id, force=True):
        calls.append("member_verify")
        raise ValueError("no pages left to read")

    monkeypatch.setattr(runner, "STAGE_CHAIN", [
        ("blank_junk", 2, ok("blank_junk")), ("member_verify", 1, boom), ("imaging_final", 1, ok("imaging_final"))])
    chart = {"id": 1, "chart_name": "c1", "status": "completed"}
    jobs: dict = {}

    @contextmanager
    def _connect():
        yield object()

    def _refresh(conn, chart_id, running=None):
        status = "processing" if running else "completed"
        statuses.append(status)
        return {"status": status, "current_stage": running[0] if running else None,
                "current_pass": running[1] if running else None}

    monkeypatch.setattr(runner, "connect", _connect)
    monkeypatch.setattr(runner, "get_chart", lambda conn, cid: chart)
    monkeypatch.setattr(runner, "refresh_chart_status", _refresh)
    monkeypatch.setattr(runner, "create_job", lambda conn, **kw: 7)
    monkeypatch.setattr(runner, "update_job", lambda conn, job_id, **kw: jobs.update(kw))
    monkeypatch.setattr(runner, "set_chart_status", lambda conn, cid, status, **kw: statuses.append(status))
    monkeypatch.setattr(download_blob, "ensure_chart_images", lambda *a, **k: {"page_count": 1})
    monkeypatch.setattr(outputs_cleanup, "mark_chart_used", lambda name: None)
    monkeypatch.setattr(run_stats, "record", lambda *a, **k: None)
    monkeypatch.setattr(reuse, "should_skip_ocr_stages", lambda **k: False)
    import common.db as db
    monkeypatch.setattr(db, "clear_stuck_processing", lambda conn, cid: 0)
    return runner, calls, statuses, jobs


def test_a_stage_error_does_not_stop_the_chain_or_fail_the_chart(chain):
    runner, calls, statuses, jobs = chain
    out = runner.run_pipeline_for_chart(1, force=False, skip_ocr=False)
    assert calls == ["blank_junk", "member_verify", "imaging_final"]
    assert out["status"] == "needs_review" and "member_verify:1" in out["stage_errors"]
    assert "failed" not in statuses and statuses[-1] == "needs_review"
    assert jobs["status"] == "completed" and "no pages left" in jobs["error_message"]


def test_the_run_is_processing_from_its_start(chain):
    runner, _calls, statuses, _jobs = chain
    runner.run_pipeline_for_chart(1, force=False, skip_ocr=False)
    assert statuses[0] == "processing"
