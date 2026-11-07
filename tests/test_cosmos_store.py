"""CosmosStore (common/db/cosmos_store.py): MemoryStore with write-through to Cosmos.

A fake Cosmos container stands in for the real one. The test writes a chart through the
common.db functions with one store, then reads everything back with a fresh store (a new
process): what the second sees is what was persisted.
"""
from __future__ import annotations

import copy

import pytest

from common.db.cosmos_store import CosmosStore


class _Err(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(status_code)
        self.status_code = status_code


class FakeContainer:
    def __init__(self, name: str) -> None:
        self.name = name
        self.items: dict[tuple[str, str], dict] = {}
        self.writes = 0
        self._etag = 0

    def _put(self, doc: dict) -> dict:
        self._etag += 1
        stored = copy.deepcopy(doc)
        stored["_etag"] = str(self._etag)
        self.items[(doc["chart_name"] if "chart_name" in doc else doc["period"], doc["id"])] = stored
        self.writes += 1
        return copy.deepcopy(stored)

    def read(self) -> dict:
        return {"id": self.name}

    def read_item(self, item: str, partition_key: str) -> dict:
        if (partition_key, item) not in self.items:
            raise _Err(404)
        return copy.deepcopy(self.items[(partition_key, item)])

    def create_item(self, doc: dict) -> dict:
        if (doc["chart_name"], doc["id"]) in self.items:
            raise _Err(409)
        return self._put(doc)

    def replace_item(self, item: str, doc: dict, etag=None, match_condition=None) -> dict:
        current = self.items.get((doc["chart_name"], item))
        if current is None:
            raise _Err(404)
        if etag is not None and current["_etag"] != etag:
            raise _Err(412)
        return self._put(doc)

    def upsert_item(self, doc: dict) -> dict:
        return self._put(doc)

    def delete_item(self, item: str, partition_key: str) -> None:
        if self.items.pop((partition_key, item), None) is None:
            raise _Err(404)

    def query_items(self, query: str, parameters=None, partition_key=None, enable_cross_partition_query=None):
        value = (parameters or [{}])[0].get("value")
        rows = [copy.deepcopy(d) for (pk, _), d in self.items.items() if partition_key is None or pk == partition_key]
        if "c.chart.id = @id" in query:
            return [{"chart_name": d["chart_name"]} for d in rows if d.get("id") == "chart" and d["chart"]["id"] == value]
        if "c.page_id = @id" in query:
            return [{"chart_name": d["chart_name"]} for d in rows if d.get("page_id") == value]
        if "c.type = 'job'" in query:
            return [d for d in rows if d.get("type") == "job"]
        return rows


@pytest.fixture()
def containers():
    return {name: FakeContainer(name) for name in ("charts", "pages", "runs", "manifest")}


def _write_a_chart(store: CosmosStore) -> tuple[int, list[int]]:
    import common.db as db

    chart = db.upsert_chart(store, chart_name="C1", page_count=2, status="processing")
    chart_id = int(chart["id"] if isinstance(chart, dict) else chart)
    pages = db.upsert_pages(store, chart_id, [{"page_name": "1.jpg", "page_number": 1},
                                              {"page_name": "2.jpg", "page_number": 2}])
    page_ids = [int(p["id"]) for p in pages]
    db.upsert_ocr_result(store, chart_id=chart_id, page_id=page_ids[0], ocr_type="tesseract", raw_text="hello page one")
    db.upsert_quality(store, chart_id=chart_id, page_id=page_ids[1], printed_or_handwritten="printed")
    db.set_page_stage(store, chart_id=chart_id, page_id=page_ids[0], stage_name="ocr_prelim", status="completed")
    db.upsert_member_summary(store, chart_id=chart_id, status="accepted")
    store.upsert_manifest_members([{"record_id": "C1", "member_name": "A B", "external_member_id": "M1"}])
    db.create_job(store, chart_id=chart_id, stage_name="pipeline_full", status="running")
    return chart_id, page_ids


def test_a_fresh_store_reads_back_what_was_written(containers):
    import common.db as db

    chart_id, page_ids = _write_a_chart(CosmosStore(containers))
    fresh = CosmosStore(containers)

    assert db.get_chart_by_name(fresh, "C1")["id"] == chart_id
    assert [p["page_name"] for p in db.list_pages(fresh, chart_id)] == ["1.jpg", "2.jpg"]
    assert db.get_ocr_texts(fresh, chart_id, "tesseract") == {page_ids[0]: "hello page one"}
    assert db.get_quality_map(fresh, chart_id)[page_ids[1]]["printed_or_handwritten"] == "printed"
    assert db.get_member_summary(fresh, chart_id)["status"] == "accepted"
    assert [m["member_name"] for m in db.list_manifest_members(fresh, record_id="C1")] == ["A B"]
    assert [j["stage_name"] for j in db.list_jobs(fresh, chart_id=chart_id)] == ["pipeline_full"]
    assert db.get_stage_status_map(fresh, chart_id, "ocr_prelim")[page_ids[0]] == "completed"


def test_documents_follow_the_plan(containers):
    _write_a_chart(CosmosStore(containers))
    assert ("C1", "chart") in containers["charts"].items
    page_docs = [d for (pk, _), d in containers["pages"].items.items() if pk == "C1"]
    assert sorted(d["id"] for d in page_docs) == sorted(f"page-{d['page_id']}" for d in page_docs)
    assert all(d["page"]["chart_id"] for d in page_docs)
    assert [pk for pk, _ in containers["manifest"].items] == ["C1"]


def test_unchanged_documents_are_not_written_again(containers):
    import common.db as db

    store = CosmosStore(containers)
    chart_id, page_ids = _write_a_chart(store)
    before = containers["pages"].writes
    db.upsert_ocr_result(store, chart_id=chart_id, page_id=page_ids[0], ocr_type="tesseract", raw_text="hello page one")
    db.upsert_ocr_result(store, chart_id=chart_id, page_id=page_ids[0], ocr_type="tesseract", raw_text="changed")
    assert containers["pages"].writes - before <= 2   # the page doc at most twice, never the other page


def test_two_stores_never_hand_out_the_same_id(containers):
    a, b = CosmosStore(containers), CosmosStore(containers)
    ids = {a._alloc() for _ in range(5)} | {b._alloc() for _ in range(5)}
    assert len(ids) == 10


def test_both_mode_copies_postgres_writes_with_postgres_ids(containers):
    """DB_BACKEND=both: MirrorStore copies each write, keeping the source's ids; a chart the
    mirror has not seen is first copied from the source. (A MemoryStore stands in for Postgres.)"""
    import common.db as db
    from common.db.cosmos_store import MirrorStore
    from common.db.memory_store import MemoryStore

    pg, mirror = MemoryStore(), MirrorStore(containers)
    chart = pg.upsert_chart(chart_name="C9", page_count=1)
    pages = pg.upsert_pages(chart["id"], [{"page_name": "1.jpg", "page_number": 1}])
    pid = pages[0]["id"]
    # the mirror sees only the later page write: it copies the chart and its pages first
    kwargs = {"chart_id": chart["id"], "page_id": pid, "ocr_type": "tesseract", "raw_text": "copied"}
    pg.upsert_ocr_result(**kwargs)
    mirror.mirror("upsert_ocr_result", pg, (), kwargs, None)

    fresh = CosmosStore(containers)
    assert db.get_chart_by_name(fresh, "C9")["id"] == chart["id"]
    assert db.get_ocr_texts(fresh, chart["id"], "tesseract") == {pid: "copied"}


def test_a_failed_copy_never_breaks_the_postgres_write(monkeypatch):
    from common.db import cosmos_store

    class Boom(cosmos_store.MirrorStore):
        def mirror(self, *a, **k):
            raise RuntimeError("cosmos down")

    monkeypatch.setattr(cosmos_store, "_MIRROR", Boom({}))
    before = cosmos_store.mirror_failures()
    cosmos_store.mirror_write("upsert_ocr_result", None, (), {}, None)   # logs, does not raise
    assert cosmos_store.mirror_failures() == before + 1
