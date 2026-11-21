"""POST /api/charts/batch-run-tag (2026-11-21, user): every chart carrying a tag, across runs and
batches, each read from its own recorded Blob folder and written to its own Processed folder."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest


class _Sender:
    def __init__(self, sent):
        self.sent = sent

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send_messages(self, messages):
        self.sent.extend(messages if isinstance(messages, list) else [messages])


class _Client:
    def __init__(self, sent):
        self.sent = sent

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_queue_sender(self, queue_name):
        return _Sender(self.sent)


def _tagged_store():
    from common.db.memory_store import MemoryStore

    mem = MemoryStore()
    for name, run, batch, tags in (
        ("c1", "Run1", "Batch1", ["Pilot-Nov"]),
        ("c2", "Run2", "Batch6", ["pilot-nov", "Review"]),
        ("c3", "Run2", "Batch6", ["Review"]),
    ):
        mem.upsert_chart(chart_name=name, blob_container="imaging-pipeline",
                         blob_path=f"Raw_Input/{run}/{batch}/DEID_PNGs/{name}",
                         output_path=f"Processed/{run}/{batch}/{name}", run_id=run, batch_id=batch)
        mem.charts[mem.charts_by_name[name]]["tags"] = tags
    mem.upsert_chart(chart_name="local1", source="local", output_path="C:/out/local1")
    mem.charts[mem.charts_by_name["local1"]]["tags"] = ["Pilot-Nov"]
    return mem


def test_charts_with_tags_any_or_all_case_insensitive():
    mem = _tagged_store()
    assert [r["chart_name"] for r in mem.charts_with_tags(["PILOT-nov"])] == ["c1", "c2", "local1"]
    assert [r["chart_name"] for r in mem.charts_with_tags(["pilot-nov", "review"], match_all=True)] == ["c2"]
    assert mem.charts_with_tags([" "]) == []


def test_each_chart_runs_from_its_own_folders():
    import apps.orchestrator_api.main as main

    mem = _tagged_store()
    body = main.TagBatchRequest(tags="Pilot-Nov", skip_ocr=True)
    requests, no_source = main.tag_run_requests(mem.charts_with_tags(body.tags), body)
    by_name = {r.chart_name: r for r in requests}
    assert by_name["c1"].input_path == "Raw_Input/Run1/Batch1/DEID_PNGs"
    assert by_name["c1"].output_path == "Processed/Run1/Batch1"
    assert by_name["c2"].input_path == "Raw_Input/Run2/Batch6/DEID_PNGs" and by_name["c2"].skip_ocr
    assert no_source == ["local1"]


@pytest.fixture
def api(monkeypatch):
    from fastapi.testclient import TestClient

    import apps.orchestrator_api.main as main
    import apps.worker.jobs.batch_intake as batch_intake
    import common.db as db
    from common import queue as q

    mem = _tagged_store()

    @contextmanager
    def _connect():
        yield mem

    sent: list = []
    monkeypatch.setattr(db, "connect", _connect)
    monkeypatch.setattr(main, "connect", _connect)
    monkeypatch.setattr(main, "RUN_MODE", "queue")
    monkeypatch.setattr(main, "_require_db", lambda: None)
    monkeypatch.setattr(q, "get_client", lambda: _Client(sent))
    monkeypatch.setattr(q, "new_message", lambda body, sid: SimpleNamespace(body=body, session_id=sid))
    registered: list = []

    def _pre_register(sources, **kw):
        registered.append((kw.get("run_id"), kw.get("batch_id"), [n for _s, n, _m in sources]))
        return [{"chart_id": mem.charts_by_name[name], "chart_name": name} for _src, name, _mode in sources]

    monkeypatch.setattr(batch_intake, "_pre_register", _pre_register)
    return TestClient(main.app, raise_server_exceptions=False), sent, registered


def test_batch_run_tag_queues_across_runs_and_batches(api):
    client, sent, registered = api
    r = client.post("/api/charts/batch-run-tag", json={"tags": ["Pilot-Nov"], "skip_ocr": True})
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["status"] == "queued" and out["charts_found"] == 3 and out["charts_queued"] == 2
    assert out["charts_no_source"] == ["local1"]
    assert {s["input_path"] for s in out["sources"]} == {"Raw_Input/Run1/Batch1/DEID_PNGs",
                                                         "Raw_Input/Run2/Batch6/DEID_PNGs"}
    assert sorted(m.session_id for m in sent) == ["c1", "c2"]
    # Each source folder registered on its own: its own run / batch.
    assert len(registered) == 2


def test_batch_run_tag_unknown_tag_is_404(api):
    client, _sent, _reg = api
    r = client.post("/api/charts/batch-run-tag", json={"tags": "nope"})
    assert r.status_code == 404 and "nope" in r.json()["detail"]
