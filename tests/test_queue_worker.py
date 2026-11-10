"""Phase 4: chart runs through the Service Bus queue, the queue worker, and the API key.

No network and no azure-servicebus SDK: the client, sender, receiver and messages are fakes.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest


# --- fakes ---------------------------------------------------------------------


class FakeSender:
    def __init__(self, sent):
        self.sent = sent

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send_messages(self, messages):
        self.sent.extend(messages if isinstance(messages, list) else [messages])


class FakeClient:
    def __init__(self, sent):
        self.sent = sent
        self.queues = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_queue_sender(self, queue_name):
        self.queues.append(queue_name)
        return FakeSender(self.sent)


class FakeMessage:
    def __init__(self, body, session_id="chart_x", delivery_count=1):
        self.body = body if isinstance(body, (bytes, str)) else json.dumps(body).encode()
        self.session_id = session_id
        self.delivery_count = delivery_count


class FakeReceiver:
    def __init__(self, messages=()):
        self.pending = list(messages)
        self.completed, self.abandoned, self.dead = [], [], []

    def receive_messages(self, max_message_count=1, max_wait_time=None):
        return [self.pending.pop(0)] if self.pending else []

    def complete_message(self, message):
        self.completed.append(message)

    def abandon_message(self, message):
        self.abandoned.append(message)

    def dead_letter_message(self, message, reason=None, error_description=None):
        self.dead.append((message, reason))


@pytest.fixture
def store(monkeypatch):
    """One in-memory database for the API and the worker."""
    import common.db as db
    from common.db.memory_store import MemoryStore

    mem = MemoryStore()

    @contextmanager
    def _connect():
        yield mem

    monkeypatch.setattr(db, "connect", _connect)
    return mem, _connect


@pytest.fixture
def queue_api(monkeypatch, store):
    """The API in queue mode with a fake Service Bus; returns (client, sent messages, store)."""
    from fastapi.testclient import TestClient

    import apps.orchestrator_api.main as main
    import apps.worker.jobs.batch_intake as batch_intake
    from common import queue as q

    mem, connect = store
    sent: list = []
    monkeypatch.setattr(main, "RUN_MODE", "queue")
    monkeypatch.setattr(main, "_require_db", lambda: None)
    monkeypatch.setattr(main, "connect", connect)
    monkeypatch.setattr(main, "_bg_run", lambda payload: pytest.fail("inline run in queue mode"))
    monkeypatch.setattr(q, "get_client", lambda: FakeClient(sent))
    monkeypatch.setattr(q, "new_message", lambda body, sid: SimpleNamespace(body=body, session_id=sid))

    def _pre_register(sources, **kw):
        return [
            {"chart_id": mem.upsert_chart(chart_name=name)["id"], "chart_name": name}
            for _src, name, _mode in sources
        ]

    monkeypatch.setattr(batch_intake, "_pre_register", _pre_register)
    return TestClient(main.app, raise_server_exceptions=False), sent, mem


BLOB_RUN = {
    "input_type": "blob",
    "container_name": "imaging-pipeline",
    "input_path": "Raw_Input/Run1/Batch1",
    "chart_name": "chart_x",
    "output_path": "Processed/Run1",
}


# --- API, queue mode -----------------------------------------------------------


class TestQueueRun:
    def test_run_sends_one_message_with_the_chart_as_session(self, queue_api):
        client, sent, mem = queue_api
        r = client.post("/api/charts/run", json=BLOB_RUN)
        assert r.status_code == 202, r.text
        reply = r.json()
        assert reply["status"] == "queued" and reply["run_id"]
        assert reply["poll"] == f"/api/runs/{reply['run_id']}"

        assert len(sent) == 1
        message = sent[0]
        assert message.session_id == "chart_x"
        assert message.body["kind"] == "run"
        assert message.body["run_id"] == reply["run_id"]
        assert message.body["trigger_id"] == reply["trigger_id"]
        assert message.body["request"]["chart_name"] == "chart_x"
        assert message.body["request"]["input_path"] == "Raw_Input/Run1/Batch1"

        job = mem.jobs[reply["run_id"]]
        assert (job["stage_name"], job["status"]) == ("queued_run", "queued")
        assert job["chart_id"] == reply["chart_id"]

    def test_the_message_request_is_a_valid_run_request(self, queue_api):
        from apps.orchestrator_api.schemas import RunRequest

        client, sent, _ = queue_api
        client.post("/api/charts/run", json={**BLOB_RUN, "only": ["dos_extract"], "skip_ocr": True})
        payload = RunRequest.model_validate(sent[0].body["request"])
        assert payload.only == ["dos_extract"] and payload.skip_ocr and payload.is_blob

    def test_send_failure_is_503_and_the_job_failed(self, queue_api, monkeypatch):
        from common import queue as q

        client, _sent, mem = queue_api

        def _boom():
            raise ConnectionError("namespace unreachable")

        monkeypatch.setattr(q, "get_client", _boom)
        r = client.post("/api/charts/run", json=BLOB_RUN)
        assert r.status_code == 503
        (job,) = mem.jobs.values()
        assert job["status"] == "failed" and "namespace unreachable" in job["error_message"]

    def test_local_run_needs_the_folder_to_exist(self, queue_api, tmp_path):
        client, sent, _ = queue_api
        body = {"input_type": "local", "input_path": str(tmp_path), "chart_name": "nope"}
        assert client.post("/api/charts/run", json=body).status_code == 400
        assert sent == []

    def test_run_status_endpoint(self, queue_api):
        client, _sent, mem = queue_api
        run_id = client.post("/api/charts/run", json=BLOB_RUN).json()["run_id"]
        r = client.get(f"/api/runs/{run_id}")
        assert r.status_code == 200
        assert r.json()["status"] == "queued" and r.json()["done"] is False

        mem.update_job(run_id, status="completed", completed=True)
        assert client.get(f"/api/runs/{run_id}?wait=5").json()["done"] is True
        assert client.get("/api/runs/999999").status_code == 404
        assert client.get(f"/api/runs/{run_id}?wait=61").status_code == 422


class TestQueueBatch:
    def test_local_batch_sends_one_message_per_chart(self, queue_api, tmp_path):
        client, sent, _ = queue_api
        for name in ("chart_a", "chart_b", "chart_c"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "1.jpg").write_bytes(b"x")
        r = client.post("/api/charts/batch-run", json={"input_type": "local", "input_path": str(tmp_path)})
        assert r.status_code == 202, r.text
        reply = r.json()
        assert reply["charts_found"] == 3 and reply["charts_queued"] == 3
        assert sorted(m.session_id for m in sent) == ["chart_a", "chart_b", "chart_c"]
        assert len({m.body["run_id"] for m in sent}) == 3
        for m in sent:
            assert m.body["request"]["input_path"] == str(tmp_path.resolve())
            assert m.body["request"]["chart_name"] == m.session_id

    def test_chart_list_and_sample_apply(self, queue_api, tmp_path):
        client, sent, _ = queue_api
        for name in ("chart_a", "chart_b", "chart_c"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "1.jpg").write_bytes(b"x")
        r = client.post("/api/charts/batch-run", json={
            "input_type": "local", "input_path": str(tmp_path),
            "chart_list": ["chart_b", "chart_c", "missing"], "sample": 1,
        })
        assert r.status_code == 202, r.text
        assert r.json()["charts_queued"] == 1 and r.json()["charts_missing"] == ["missing"]
        assert [m.session_id for m in sent] == ["chart_b"]

    def test_blob_batch_is_listed_in_the_background(self, queue_api, monkeypatch):
        import common.db.blob_store as blob_store

        client, sent, _ = queue_api
        monkeypatch.setattr(blob_store, "ensure_blob_ready", lambda container: None)
        monkeypatch.setattr(
            blob_store, "list_chart_prefixes",
            lambda container, prefix: [f"{prefix}/c1", f"{prefix}/c2"],
        )
        body = {k: v for k, v in BLOB_RUN.items() if k != "chart_name"}
        r = client.post("/api/charts/batch-run", json=body)
        assert r.status_code == 202, r.text
        assert sorted(m.session_id for m in sent) == ["c1", "c2"]
        assert all(m.body["request"]["input_path"] == "Raw_Input/Run1/Batch1" for m in sent)


# --- worker ----------------------------------------------------------------------


def _run_body(run_id, chart="chart_x"):
    from common.queue import run_message

    return run_message(run_id, {**BLOB_RUN, "chart_name": chart}, "T-test")


class TestWorker:
    def test_a_message_runs_the_chart_and_is_completed(self, store):
        from apps.worker import queue_worker

        mem, _ = store
        run_id = mem.create_job(chart_id=None, stage_name="queued_run")
        message = FakeMessage(_run_body(run_id))
        receiver = FakeReceiver([message])
        calls = []

        def run(body):
            calls.append(body)
            return {"chart_id": 42}

        assert queue_worker.serve_session(receiver, run) == 1
        assert calls[0]["run_id"] == run_id
        assert receiver.completed == [message] and not receiver.abandoned
        assert mem.jobs[run_id]["status"] == "completed"
        assert mem.jobs[run_id]["chart_id"] == 42

    def test_a_failing_run_is_abandoned_and_the_job_failed(self, store):
        from apps.worker import queue_worker

        mem, _ = store
        run_id = mem.create_job(chart_id=None, stage_name="queued_run")
        first = FakeMessage(_run_body(run_id), delivery_count=2)
        second = FakeMessage(_run_body(run_id))
        receiver = FakeReceiver([first, second])

        def run(body):
            raise RuntimeError("stage blew up")

        # The session is released after a failure, so the redelivery can go to any worker.
        assert queue_worker.serve_session(receiver, run) == 1
        assert receiver.abandoned == [first] and not receiver.completed
        assert mem.jobs[run_id]["status"] == "failed"
        assert "stage blew up" in mem.jobs[run_id]["error_message"]
        assert "delivery 2" in mem.jobs[run_id]["error_message"]

    def test_unknown_kind_is_completed_without_running(self, store):
        from apps.worker import queue_worker

        receiver = FakeReceiver()
        message = FakeMessage({"kind": "smoke"}, session_id="_queue_smoke")
        outcome = queue_worker.handle_message(receiver, message, lambda body: pytest.fail("ran"))
        assert outcome == "ignored" and receiver.completed == [message]

    def test_a_body_that_is_not_json_is_dead_lettered(self, store):
        from apps.worker import queue_worker

        receiver = FakeReceiver()
        message = FakeMessage(b"not json")
        assert queue_worker.handle_message(receiver, message, lambda body: {}) == "dead_lettered"
        assert receiver.dead and not receiver.completed

    def test_run_message_body_calls_the_shared_run_function(self, store, monkeypatch):
        from apps.worker import chart_run, queue_worker

        mem, _ = store
        seen = {}

        def fake_execute(payload, on_chart=None):
            seen["payload"] = payload
            return {"chart_id": 7, "chart_name": payload.chart_name}

        monkeypatch.setattr(chart_run, "execute_run", fake_execute)
        result = queue_worker.run_message_body(_run_body(None))
        assert result["chart_id"] == 7
        assert seen["payload"].chart_name == "chart_x" and seen["payload"].is_blob

    def test_stop_request_ends_the_session_loop(self, store):
        from apps.worker import queue_worker

        receiver = FakeReceiver([FakeMessage({"kind": "smoke"})])
        queue_worker.request_stop()
        try:
            assert queue_worker.serve_session(receiver) == 0
        finally:
            queue_worker._stop.clear()


def test_mask_never_shows_the_key():
    from common.queue import mask

    masked = mask("Endpoint=sb://ns.servicebus.windows.net/;SharedAccessKeyName=x;SharedAccessKey=SECRET")
    assert "SECRET" not in masked and "ns.servicebus.windows.net" in masked


# --- API key -----------------------------------------------------------------------


class TestApiKey:
    @staticmethod
    def _client(monkeypatch, required, key):
        from fastapi.testclient import TestClient

        import apps.orchestrator_api.main as main

        monkeypatch.setattr(main, "API_KEY_REQUIRED", required)
        monkeypatch.setattr(main, "ORCHESTRATOR_API_KEY", key)
        monkeypatch.setattr(main, "_require_db", lambda: None)
        monkeypatch.setattr(main, "_bg_run", lambda payload: None)
        return TestClient(main.app, raise_server_exceptions=False)

    def test_post_without_the_key_is_401(self, monkeypatch):
        client = self._client(monkeypatch, True, "k1")
        assert client.post("/api/charts/run", json=BLOB_RUN).status_code == 401
        assert client.post("/api/charts/run", json=BLOB_RUN, headers={"X-API-Key": "wrong"}).status_code == 401

    def test_post_with_the_key_passes(self, monkeypatch):
        client = self._client(monkeypatch, True, "k1")
        r = client.post("/api/charts/run", json=BLOB_RUN, headers={"X-API-Key": "k1"})
        assert r.status_code == 202, r.text

    def test_gets_stay_open(self, monkeypatch):
        client = self._client(monkeypatch, True, "k1")
        assert client.get("/health").status_code == 200
        assert client.get("/docs").status_code == 200

    def test_not_required_by_default_or_without_a_key(self, monkeypatch):
        assert self._client(monkeypatch, False, "k1").post("/api/charts/run", json=BLOB_RUN).status_code == 202
        assert self._client(monkeypatch, True, "").post("/api/charts/run", json=BLOB_RUN).status_code == 202


class TestInlineUnchanged:
    def test_inline_mode_runs_in_the_background_and_sends_nothing(self, monkeypatch):
        from fastapi.testclient import TestClient

        import apps.orchestrator_api.main as main
        from common import queue as q

        ran = []
        monkeypatch.setattr(main, "RUN_MODE", "inline")
        monkeypatch.setattr(main, "_require_db", lambda: None)
        monkeypatch.setattr(main, "_bg_run", lambda payload: ran.append(payload.chart_name))
        monkeypatch.setattr(q, "get_client", lambda: pytest.fail("queue used in inline mode"))
        r = TestClient(main.app, raise_server_exceptions=False).post("/api/charts/run", json=BLOB_RUN)
        assert r.status_code == 202 and r.json()["status"] == "accepted" and "run_id" not in r.json()
        assert ran == ["chart_x"]

    def test_health_reports_the_run_mode(self, monkeypatch):
        from fastapi.testclient import TestClient

        import apps.orchestrator_api.main as main

        body = TestClient(main.app).get("/health").json()
        assert body["run_mode"] == "inline" and "queue" not in body


def test_the_worker_idles_in_inline_mode(monkeypatch):
    """2026-11-09: compose starts the worker with the api; with RUN_MODE=inline it never reads
    the queue or loads models, it just waits to be stopped."""
    import dotenv

    from apps.worker import queue_worker
    from common import config

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(config, "RUN_MODE", "inline")
    monkeypatch.setattr(queue_worker, "run_forever", lambda: (_ for _ in ()).throw(AssertionError("ran")))
    monkeypatch.setattr(queue_worker.signal, "signal", lambda *a, **k: None)
    queue_worker._stop.set()
    try:
        queue_worker.main()
    finally:
        queue_worker._stop.clear()


class _Ctx:
    def __init__(self, obj):
        self.obj = obj

    def __enter__(self):
        return self.obj

    def __exit__(self, *a):
        return False


def _probe(monkeypatch, conn, *, admin_ok=False, send_ok=True, queue="chart-runs"):
    import sys
    import types

    from apps.orchestrator_api import main
    from common import queue as q

    monkeypatch.setattr(q, "settings", lambda: (conn, queue))

    class Admin:
        @classmethod
        def from_connection_string(cls, c):
            return cls()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_queue_runtime_properties(self, name):
            if not admin_ok:
                raise RuntimeError("Operation returned an invalid status 'Unauthorized'")
            return types.SimpleNamespace(active_message_count=2, dead_letter_message_count=0)

    mod = types.ModuleType("azure.servicebus.management")
    mod.ServiceBusAdministrationClient = Admin
    monkeypatch.setitem(sys.modules, "azure.servicebus.management", mod)

    class Sender:
        def create_message_batch(self):
            if not send_ok:
                raise RuntimeError("Unauthorized: no Send claim")

    class Client:
        def get_queue_sender(self, name):
            return _Ctx(Sender())

    monkeypatch.setattr(q, "get_client", lambda: _Ctx(Client()))
    return main._probe_queue()


def test_a_send_listen_key_is_reachable_without_manage(monkeypatch):
    """2026-11-10: the management read needs Manage; a Send + Listen key falls back to a send link."""
    conn = "Endpoint=sb://ns.servicebus.windows.net/;SharedAccessKeyName=pipeline;SharedAccessKey=x"
    got = _probe(monkeypatch, conn)
    assert got["reachable"] is True and "Manage" in got["note"]
    assert _probe(monkeypatch, conn, admin_ok=True)["active_messages_at_start"] == 2
    assert _probe(monkeypatch, conn, send_ok=False)["reachable"] is False


def test_a_key_for_another_queue_is_named(monkeypatch):
    conn = "Endpoint=sb://ns.servicebus.windows.net/;SharedAccessKeyName=p;SharedAccessKey=x;EntityPath=chart-runs"
    got = _probe(monkeypatch, conn, queue="chart-run")
    assert got["reachable"] is False and "for queue 'chart-runs'" in got["reason"] and "chart-run" in got["reason"]


def test_a_worker_is_named_as_docker_ps_shows_it(monkeypatch):
    """2026-11-10 (user): 'worker-2', not a container id; outside Docker the host name."""
    from apps.worker import queue_worker as qw

    monkeypatch.setattr(qw.socket, "gethostname", lambda: "5128429608d6")
    monkeypatch.setattr(qw.socket, "gethostbyname", lambda h: "172.18.0.5")
    monkeypatch.setattr(qw.socket, "gethostbyaddr",
                        lambda ip: ("core-pipeline-worker-2.core-pipeline_default", [], [ip]))
    assert qw.worker_name() == "worker-2"

    def no_dns(ip):
        raise OSError("no reverse DNS")

    monkeypatch.setattr(qw.socket, "gethostbyaddr", no_dns)
    monkeypatch.setattr(qw.socket, "gethostname", lambda: "ADVINDLP856")
    assert qw.worker_name() == "worker-ADVINDLP856"


def test_a_run_ends_with_a_completion_line():
    """2026-11-10 (user): on completion, the chart, start, end and time taken."""
    from datetime import datetime, timedelta

    from apps.worker.run_stats import completion_line, took

    start = datetime(2026, 11, 10, 17, 20, 1)
    line = completion_line("C1", "completed", start, start + timedelta(seconds=703), 42)
    assert line == ("CHART COMPLETED: C1 | status completed | 42 page(s) | started 2026-11-10 17:20:01 | "
                    "ended 17:31:44 | took 11m 43s")
    failed = completion_line("C1", "failed", start, start + timedelta(seconds=5), 0, "boom")
    assert failed.startswith("CHART FAILED: C1") and failed.endswith("took 5s | error: boom")
    assert took(3725) == "1h 2m 5s"


def test_the_api_logs_which_worker_took_a_chart_and_when_it_finished(caplog):
    """2026-11-10 (user): the API's own log names the worker and the run's start, end, time."""
    import logging

    from apps.orchestrator_api.run_watch import report
    from common.db import create_job, update_job
    from common.db.memory_store import MemoryStore

    store = MemoryStore()
    run_id = create_job(store, chart_id=None, stage_name="queued_run", status="queued")
    runs = {run_id: {"chart_name": "C1", "seen": set()}}
    caplog.set_level(logging.INFO, logger="core-pipeline")
    report(store, runs)
    assert not caplog.messages  # still queued: nothing to say
    update_job(store, run_id, status="running", started=True, worker_id="worker-2")
    report(store, runs)
    report(store, runs)
    assert caplog.messages == [f"run {run_id}: chart C1 PICKED UP by worker-2"]
    update_job(store, run_id, status="completed", completed=True)
    report(store, runs)
    done = caplog.messages[-1]
    assert done.startswith(f"run {run_id}: CHART COMPLETED: C1 | status completed") and "took 0s" in done
    assert done.endswith("| worker-2") and not runs
