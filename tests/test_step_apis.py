"""Phase 5: the /v1 step-by-step chart APIs (apps/orchestrator_api/steps.py, apps/worker/targets.py).

In-memory store, fake Service Bus, the run functions replaced: no network, no models.
"""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

OCR = ["ocr_quality:1", "ocr_prelim:1", "blank_junk:1", "ocr_final1:1", "ocr_final2:1", "section_headers:1"]
DOS = OCR + ["kv_extract:1", "blank_junk:2", "page_subtype:1", "dos_extract:1"]


# --- target -> stages ------------------------------------------------------------


class TestTargets:
    def test_chain_matches_the_runner_and_the_schema_seed(self):
        import re
        from pathlib import Path

        from apps.worker.runner import STAGE_NAMES
        from apps.worker.targets import CHAIN

        assert list(CHAIN) == STAGE_NAMES
        schema = (Path(__file__).resolve().parents[1] / "schema" / "schema.sql").read_text(encoding="utf-8")
        seed = schema[schema.index("INSERT INTO pipeline_stage"):]
        seeded = [f"{n}:{p}" for n, p in re.findall(r"\('(\w+)',\s*(\d)", seed)[: len(CHAIN)]]
        assert seeded == list(CHAIN)

    def test_each_target(self):
        from apps.worker.targets import CHAIN, stages_for

        assert stages_for("ocr") == OCR
        assert stages_for("page-type") == OCR + ["kv_extract:1", "blank_junk:2", "page_subtype:1"]
        assert stages_for("member-verify") == OCR + ["kv_extract:1", "blank_junk:2", "member_verify:1"]
        assert stages_for("dos") == DOS
        assert stages_for("full") == list(CHAIN) and len(CHAIN) == 15
        with pytest.raises(KeyError, match="known"):
            stages_for("nope")

    def test_every_token_resolves_in_the_runner(self):
        from apps.worker.runner import resolve_stage
        from apps.worker.targets import TARGETS, stages_for

        for target in TARGETS:
            for token in stages_for(target):
                resolve_stage(token)


# --- fixtures -----------------------------------------------------------------------


class FakeSender:
    def __init__(self, sent):
        self.sent = sent

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send_messages(self, message):
        self.sent.append(message)


class FakeClient(FakeSender):
    def get_queue_sender(self, queue_name):
        return FakeSender(self.sent)


@pytest.fixture
def api(monkeypatch):
    """The API (inline mode) on a MemoryStore, with the run functions recorded, not run."""
    from fastapi.testclient import TestClient

    import apps.orchestrator_api.main as main
    import apps.orchestrator_api.steps as steps
    import apps.worker.jobs.batch_intake as batch_intake
    import common.db as db
    from apps.worker import chart_run
    from common import queue as q
    from common.db.memory_store import MemoryStore

    mem = MemoryStore()

    @contextmanager
    def _connect():
        yield mem

    monkeypatch.setattr(db, "connect", _connect)
    monkeypatch.setattr(main, "connect", _connect)
    monkeypatch.setattr(main, "_require_db", lambda: None)
    monkeypatch.setattr(main, "RUN_MODE", "inline")
    monkeypatch.setattr(steps, "POLL_SECONDS", 0.02)

    calls: list = []

    def _imported(chart_id, payload):
        calls.append(("imported", chart_id, payload))

    def _execute(payload, on_chart=None):
        chart = mem.upsert_chart(chart_name=payload.chart_name)
        calls.append(("execute", chart["id"], payload))
        return {"chart_id": chart["id"], "chart_name": payload.chart_name}

    monkeypatch.setattr(chart_run, "run_imported_chart", _imported)
    monkeypatch.setattr(chart_run, "execute_run", _execute)

    sent: list = []
    monkeypatch.setattr(q, "get_client", lambda: FakeClient(sent))
    monkeypatch.setattr(q, "new_message", lambda body, sid: SimpleNamespace(body=body, session_id=sid))
    monkeypatch.setattr(batch_intake, "_pre_register", lambda sources, **kw: [
        {"chart_id": mem.upsert_chart(chart_name=name)["id"], "chart_name": name} for _s, name, _m in sources
    ])
    client = TestClient(main.app, raise_server_exceptions=False)
    return SimpleNamespace(client=client, mem=mem, calls=calls, sent=sent, main=main)


def _chart(mem, name="chart_x", pages=3, done=(), source="blob"):
    chart = mem.upsert_chart(
        chart_name=name, source=source,
        blob_container="imaging-pipeline" if source == "blob" else None,
        blob_path=f"Raw_Input/Run1/Batch1/{name}" if source == "blob" else None,
    )
    rows = mem.upsert_pages(chart["id"], [{"page_name": f"{i}.jpg", "page_number": i} for i in range(1, pages + 1)])
    for token in done:
        name_, _, pass_no = token.partition(":")
        for row in rows:
            mem.set_page_stage(chart_id=chart["id"], page_id=row["id"], stage_name=name_,
                               status="completed", pass_no=int(pass_no))
    return chart["id"], rows


BLOB_SOURCE = {"input_type": "blob", "container_name": "imaging-pipeline", "input_path": "Raw_Input/Run1/Batch1"}


# --- inline ---------------------------------------------------------------------------


class TestOnlyMissingStages:
    def test_a_fresh_chart_asks_for_every_dos_stage(self, api, tmp_path):
        (tmp_path / "chart_new").mkdir()
        r = api.client.post("/v1/charts/chart_new/dos?wait=5",
                            json={"input_type": "local", "input_path": str(tmp_path)})
        assert r.status_code == 200, r.text
        reply = r.json()
        assert reply["status"] == "completed" and reply["stages"] == DOS
        ((kind, _cid, payload),) = api.calls
        assert kind == "execute" and payload.only == DOS and payload.chart_name == "chart_new"
        assert set(reply["result"]) == {"pages", "documents"}

    def test_a_chart_with_ocr_done_runs_only_the_rest(self, api):
        chart_id, _ = _chart(api.mem, done=OCR)
        r = api.client.post("/v1/charts/chart_x/dos?wait=5")
        assert r.status_code == 200, r.text
        ((kind, cid, payload),) = api.calls
        assert (kind, cid) == ("imported", chart_id)
        assert payload.only == ["kv_extract:1", "blank_junk:2", "page_subtype:1", "dos_extract:1"]
        job = api.mem.jobs[r.json()["run_id"]]
        assert (job["stage_name"], job["status"]) == ("v1_dos", "completed")

    def test_a_stage_missing_on_one_page_reruns_from_there(self, api):
        chart_id, rows = _chart(api.mem, done=OCR + ["kv_extract:1", "blank_junk:2", "page_subtype:1"])
        api.mem.set_page_stage(chart_id=chart_id, page_id=rows[1]["id"], stage_name="kv_extract",
                               status="failed", pass_no=1)
        api.client.post("/v1/charts/chart_x/page-type?wait=5")
        assert api.calls[0][2].only == ["kv_extract:1", "blank_junk:2", "page_subtype:1"]

    def test_second_call_when_all_complete_is_200_without_running(self, api):
        chart_id, rows = _chart(api.mem, done=DOS)
        api.mem.upsert_dos(chart_id=chart_id, page_id=rows[0]["id"], date_of_service_from="2024-03-15",
                           date_of_service_to="2024-03-15", date_of_service_from_doclevel="2024-03-15",
                           date_of_service_to_doclevel="2024-03-15", confidence=0.9)
        api.mem.upsert_dos(chart_id=chart_id, page_id=rows[1]["id"], date_of_service_from=None,
                           date_of_service_to=None, date_of_service_from_doclevel="2024-03-15",
                           date_of_service_to_doclevel="2024-03-15", confidence=None)
        jobs_before = len(api.mem.jobs)
        r = api.client.post("/v1/charts/chart_x/dos")
        assert r.status_code == 200, r.text
        reply = r.json()
        assert reply["status"] == "completed" and reply["stages_run"] == [] and "run_id" not in reply
        assert api.calls == [] and len(api.mem.jobs) == jobs_before
        result = reply["result"]
        assert [p["page_number"] for p in result["pages"]] == [1, 2]
        assert result["documents"] == [{"first_page": 1, "last_page": 2, "date_of_service_from": "2024-03-15",
                                        "date_of_service_to": "2024-03-15"}]

    def test_rerun_runs_every_stage_of_the_target(self, api):
        _chart(api.mem, done=DOS)
        r = api.client.post("/v1/charts/chart_x/ocr?wait=5", json={"rerun": True})
        assert r.status_code == 200, r.text
        assert api.calls[0][2].only == OCR

    def test_full_run_of_a_new_chart_is_a_plain_run(self, api):
        r = api.client.post("/v1/charts/run?wait=5", json={**BLOB_SOURCE, "chart_name": "chart_full"})
        assert r.status_code == 200, r.text
        payload = api.calls[0][2]
        assert payload.only is None and payload.is_blob and payload.input_path == "Raw_Input/Run1/Batch1"

    def test_without_wait_the_answer_is_202_with_a_run_id(self, api, monkeypatch):
        import apps.orchestrator_api.steps as steps

        monkeypatch.setattr(steps, "_start_inline", lambda *a, **k: None)
        _chart(api.mem)
        r = api.client.post("/v1/charts/chart_x/ocr")
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        assert r.json()["poll"] == f"/v1/runs/{run_id}"
        got = api.client.get(f"/v1/runs/{run_id}").json()
        assert got["status"] == "queued" and got["target"] == "ocr" and "result" not in got
        # A second call while it is queued points at the same run.
        again = api.client.post("/v1/charts/chart_x/ocr").json()
        assert again["run_id"] == run_id and again["already_running"] is True

    def test_v1_runs_returns_the_result_when_done(self, api):
        chart_id, rows = _chart(api.mem, done=OCR)
        api.mem.upsert_ocr_result(chart_id=chart_id, page_id=rows[0]["id"], ocr_type="docling", raw_text="hello")
        api.mem.upsert_ocr_result(chart_id=chart_id, page_id=rows[0]["id"], ocr_type="tesseract", raw_text="hel")
        run_id = api.mem.create_job(chart_id=chart_id, stage_name="v1_ocr", status="completed")
        got = api.client.get(f"/v1/runs/{run_id}?wait=1").json()
        page1 = got["result"]["pages"][0]
        assert (page1["engine"], page1["chars"]) == ("docling", 5)
        assert page1["chars_by_engine"] == {"docling": 5, "tesseract": 3}

    def test_unregistered_chart_without_a_source_is_404(self, api):
        assert api.client.post("/v1/charts/nobody/dos").status_code == 404
        assert api.calls == []

    def test_mismatched_chart_name_and_unknown_fields(self, api):
        _chart(api.mem)
        assert api.client.post("/v1/charts/chart_x/dos", json={"chart_name": "other"}).status_code == 400
        assert api.client.post("/v1/charts/chart_x/dos", json={"force": True}).status_code == 422
        assert api.client.post("/v1/charts/chart_x/dos?wait=61").status_code == 422

    def test_member_verify_result(self, api):
        chart_id, rows = _chart(api.mem, done=OCR + ["kv_extract:1", "blank_junk:2", "member_verify:1"])
        api.mem.upsert_member_summary(chart_id=chart_id, final_status="failed", document_decision="reject",
                                      wrong_member_pages=1)
        api.mem.upsert_member_extraction(chart_id=chart_id, page_id=rows[2]["id"], page_status="wrong_member",
                                         extracted_name="JANE ROE", other_names=["JANE ROE"])
        reply = api.client.post("/v1/charts/chart_x/member-verify").json()
        assert reply["result"]["summary"]["document_decision"] == "reject"
        assert [p["page_number"] for p in reply["result"]["wrong_member_pages"]] == [3]


# --- queue mode and the API key ------------------------------------------------------


class TestQueueMode:
    def test_one_message_with_only_and_target(self, api, monkeypatch):
        monkeypatch.setattr(api.main, "RUN_MODE", "queue")
        chart_id, _ = _chart(api.mem, done=OCR)
        r = api.client.post("/v1/charts/chart_x/member-verify")
        assert r.status_code == 202, r.text
        assert api.calls == []
        (message,) = api.sent
        assert message.session_id == "chart_x"
        body = message.body
        assert body["target"] == "member-verify" and body["run_id"] == r.json()["run_id"]
        assert body["request"]["only"] == ["kv_extract:1", "blank_junk:2", "member_verify:1"]
        assert body["request"]["input_type"] == "blob"
        assert body["request"]["input_path"] == "Raw_Input/Run1/Batch1"
        job = api.mem.jobs[body["run_id"]]
        assert (job["stage_name"], job["status"]) == ("v1_member-verify", "queued")

    def test_a_local_chart_needs_its_source_in_queue_mode(self, api, monkeypatch):
        monkeypatch.setattr(api.main, "RUN_MODE", "queue")
        _chart(api.mem, source="local")
        assert api.client.post("/v1/charts/chart_x/dos").status_code == 400
        assert api.sent == []


class TestApiKey:
    def test_key_required_on_v1_posts(self, api, monkeypatch):
        monkeypatch.setattr(api.main, "API_KEY_REQUIRED", True)
        monkeypatch.setattr(api.main, "ORCHESTRATOR_API_KEY", "s3cret")
        _chart(api.mem, done=DOS)
        for path in ("/v1/charts/chart_x/ocr", "/v1/charts/chart_x/page-type",
                     "/v1/charts/chart_x/member-verify", "/v1/charts/chart_x/dos"):
            assert api.client.post(path).status_code == 401
            assert api.client.post(path, headers={"X-API-Key": "wrong"}).status_code == 401
        assert api.client.post("/v1/charts/run", json={"chart_name": "chart_x"}).status_code == 401
        ok = api.client.post("/v1/charts/chart_x/ocr", headers={"X-API-Key": "s3cret"})
        assert ok.status_code == 200, ok.text
        run_id = api.mem.create_job(chart_id=None, stage_name="v1_dos")
        assert api.client.get(f"/v1/runs/{run_id}").status_code == 200  # GETs stay open
