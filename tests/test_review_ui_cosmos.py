"""review-ui on Cosmos (DB_BACKEND=cosmos): CosmosFolderRepository shows a chart exactly as
PostgresFolderRepository does.

A small chart is written through core-pipeline's CosmosStore into fake Cosmos containers, so
the documents have the shapes the pipeline really writes. The same chart is given to the
Postgres repository as the rows its SQL would return. Both must build the same responses.
"""
from __future__ import annotations

import copy
import itertools
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from common.db.cosmos_store import CosmosStore

from app.store.cosmos.repository import CosmosFolderRepository, PHASE1_STAGES
from app.store.postgres import repository as pg_mod
from app.store.postgres.repository import PostgresFolderRepository


# ---------------------------------------------------------------------------
# fake Cosmos
# ---------------------------------------------------------------------------

class _Err(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(status_code)
        self.status_code = status_code


_TS = itertools.count(1_000_000)


class FakeContainer:
    """Enough of a Cosmos container client for CosmosStore and the repository."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.items: dict[tuple[str, str], dict] = {}
        self.queries: list[tuple[str, str | None]] = []
        self._etag = 0

    def _put(self, doc: dict) -> dict:
        self._etag += 1
        stored = copy.deepcopy(doc)
        stored["_etag"] = str(self._etag)
        stored["_ts"] = next(_TS)
        self.items[(doc["chart_name"], doc["id"])] = stored
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
        self.queries.append((query, partition_key))
        value = (parameters or [{}])[0].get("value")
        rows = [copy.deepcopy(d) for (pk, _), d in self.items.items() if partition_key is None or pk == partition_key]
        if "COUNT(1)" in query:
            return [{"n": len(rows), "ts": max((d["_ts"] for d in rows), default=None)}]
        if "c.chart.id = @id" in query:
            return [{"chart_name": d["chart_name"]} for d in rows if d.get("id") == "chart" and d["chart"]["id"] == value]
        if "c.page_id = @id" in query:
            return [{"chart_name": d["chart_name"]} for d in rows if d.get("page_id") == value]
        if "c.id = 'chart'" in query:
            return [{"chart": d["chart"]} for d in rows if d.get("id") == "chart"]
        if "c.id = @id" in query:
            return [d for d in rows if d.get("id") == value]
        if "c.type = 'job'" in query:
            return [d for d in rows if d.get("type") == "job"]
        return rows


@pytest.fixture()
def containers():
    return {name: FakeContainer(name) for name in ("charts", "pages", "runs", "manifest", "ground_truth")}


@pytest.fixture(autouse=True)
def _settled(monkeypatch):
    """Fake _ts values are far in the past, so the page cache is used as in production."""
    pg_mod._OCR_TEXT_CACHE.clear()
    yield


# ---------------------------------------------------------------------------
# one small chart, written the way core-pipeline writes it
# ---------------------------------------------------------------------------

DOCLING_P1 = '{"content": "Progress Note\\nPatient seen today.", "pages": []}'


def _write_chart(containers) -> dict:
    store = CosmosStore(containers)
    chart = store.upsert_chart(chart_name="C1", page_count=3, status="processing", source="blob",
                               blob_container="imaging", blob_path="Raw_Input/R1/B1/C1",
                               output_path="Processed/R1", run_id="R1", batch_id="B1")
    cid = int(chart["id"])
    pages = store.upsert_pages(cid, [{"page_name": f"{n}.jpg", "page_number": n} for n in (1, 2, 3)])
    p1, p2, p3 = (int(p["id"]) for p in pages)

    store.upsert_ocr_result(chart_id=cid, page_id=p1, ocr_type="tesseract", raw_text="hello one")
    store.upsert_ocr_result(chart_id=cid, page_id=p2, ocr_type="tesseract", raw_text="   ")
    store.upsert_ocr_result(chart_id=cid, page_id=p3, ocr_type="tesseract", raw_text="page three")
    store.upsert_ocr_result(chart_id=cid, page_id=p1, ocr_type="docling", raw_text=DOCLING_P1)
    store.upsert_ocr_result(chart_id=cid, page_id=p3, ocr_type="docling", raw_text="plain final one")
    store.upsert_ocr_result(chart_id=cid, page_id=p1, ocr_type="azuredocintel", raw_text="Azure text p1")
    for name, pass_no in PHASE1_STAGES:
        store.set_page_stage(chart_id=cid, page_id=p1, stage_name=name, pass_no=pass_no, status="completed")
    store.set_page_stage(chart_id=cid, page_id=p2, stage_name="ocr_final1", status="skipped",
                         skip_reason="blank_junk_pass1")
    store.set_page_stage(chart_id=cid, page_id=p3, stage_name="ocr_final2", status="skipped",
                         skip_reason="high_quality_printed")

    store.upsert_quality(chart_id=cid, page_id=p1, printed_or_handwritten="printed", hw_confidence=0.91,
                         quality_tag="high", quality_score=0.8, orientation_angle=0.0, tilt_angle=1.5,
                         mirrored=False, document_type="typed", is_visible=True,
                         handwritten_area_pct=2.0, quality_detail={"confidences": {"rotation": 0.8}})
    store.upsert_quality(chart_id=cid, page_id=p2, printed_or_handwritten="handwritten", hw_confidence=0.7,
                         quality_tag="low", quality_score=0.5)

    store.upsert_blank_junk(chart_id=cid, page_id=p1, blank_junk_flag="not_blank_junk", pass_no=1, ocr_source="t")
    store.upsert_blank_junk(chart_id=cid, page_id=p1, blank_junk_flag="not_blank_junk", pass_no=2,
                            ocr_source="f", confidence=0.88)
    store.upsert_blank_junk(chart_id=cid, page_id=p2, blank_junk_flag="blank", pass_no=1, ocr_source="t",
                            confidence=0.99, reason="no text")
    store.upsert_blank_junk(chart_id=cid, page_id=p3, blank_junk_flag="not_blank_junk", pass_no=2,
                            ocr_source="f", duplicate_of_page_id=p1, confidence=0.7,
                            reason="partial", duplicate_confidence=0.96)
    store.mark_blank_junk_final(cid)

    store.upsert_member_extraction(chart_id=cid, page_id=p1, extracted_name="JANE DOE",
                                   extracted_dob="1950-01-02", extracted_member_id="M1", confidence=0.9,
                                   page_status="match", other_names=["Bob Roe", " "])
    members = store.upsert_manifest_members([{
        "record_id": "C1", "member_name": "JANE DOE", "member_dob": "1950-01-02",
        "external_member_id": "M1", "measure": "COL", "service_note": "2024-01-01 to 2024-12-31",
    }])
    mid = int(store.list_manifest_members(record_id="C1")[0]["id"])
    store.upsert_member_summary(chart_id=cid, final_status="accepted", matched_name="JANE DOE",
                                matched_member_list_id=mid, confidence=0.95, pages_matched=1,
                                pages_checked=2, decision_reason="name and dob")

    for pid in (p1, p2):
        store.upsert_dos(chart_id=cid, page_id=pid, date_of_service_from="2024-03-15",
                         date_of_service_to="2024-03-16", date_of_service_from_doclevel="2024-03-15",
                         date_of_service_to_doclevel="2024-03-16", confidence=0.95)
    store.upsert_page_classification(chart_id=cid, page_id=p1, page_subtype="Progress Note",
                                     classification_category="codeable", confidence=0.8,
                                     page_type="Clinical Notes",
                                     axes={"specialty": "Cardiology", "specialty_confidence": 0.7,
                                           "l2": "Progress Note", "l2_confidence": 0.6})
    store.upsert_encounter(chart_id=cid, page_id=p1, encounter_type="outpatient_f2f", confidence=0.9)
    store.upsert_sequencing(chart_id=cid, page_id=p1, original_page_number=1, seq=1, confidence=1.0)
    store.upsert_sequencing(chart_id=cid, page_id=p3, original_page_number=3, seq=2, confidence=0.6)
    continuity = {"document_seq": 1, "seq": 1, "position": "start", "relation": "new", "decided_by": "rule",
                  "confidence_level": "high", "confidence": 0.9, "evidence": "header", "label": None}
    store.upsert_continuity(chart_id=cid, page_id=p1, row=continuity)
    store.upsert_provider_signature(chart_id=cid, page_id=p1, fields={
        "provider_name": "Ann Lee", "signature_present": True, "provider_credentials": "MD",
        "signers": None, "provider_version": "v2", "confidence": 0.8})
    final = {"page_type": "Clinical Notes", "classification_category": "codeable", "dos_from": "2024-03-15",
             "dos_to": "2024-03-16", "page_subtype": "Progress Note", "specialty": "Cardiology",
             "provider_name": "Ann Lee", "signature_present": True, "provider_credentials": "MD",
             "signers": None, "provider_version": "v2", "provider_confidence": 0.8,
             "confidences": {"dos": 0.9, "note": "x"}, "review_fields": ["dos"]}
    store.upsert_imaging_final(chart_id=cid, page_id=p1, row=final)
    store.set_chart_status(cid, "processing", current_stage="page_sequencing")
    assert members is not None
    return {"chart_id": cid, "pages": (p1, p2, p3), "member_id": mid}


# ---------------------------------------------------------------------------
# the same chart as the Postgres queries return it
# ---------------------------------------------------------------------------

def _postgres_rows(ids: dict):
    cid, mid = ids["chart_id"], ids["member_id"]
    D = Decimal
    status_row = (cid, "processing", "page_sequencing", "R1", "B1")
    pages = [(1, "1.jpg"), (2, "2.jpg"), (3, "3.jpg")]
    flags = [("1.jpg", "tesseract"), ("3.jpg", "tesseract"), ("1.jpg", "docling"), ("3.jpg", "docling"),
             ("1.jpg", "azuredocintel")]
    overlay = {
        "FROM ocr_quality_results q": [
            ("1.jpg", "printed", D("0.9100"), D("0.8000"), "high", D("0.00"), D("1.50"), False,
             "typed", True, D("2.00"), {"rotation": 0.8}),
            ("2.jpg", "handwritten", D("0.7000"), D("0.5000"), "low", None, None, None, None, None, None, None),
        ],
        "FROM v_page_blank_junk_final": [
            ("1.jpg", "not_blank_junk", None, D("0.8800"), None, None, None),
            ("2.jpg", "blank", None, D("0.9900"), None, None, "no text"),
            ("3.jpg", "not_blank_junk", None, D("0.7000"), "1.jpg", "0.9600", "partial"),
        ],
        "FROM member_extraction_results m": [
            ("1.jpg", "JANE DOE", date(1950, 1, 2), "M1", D("0.9000"), "match", ["Bob Roe", " "]),
        ],
        "FROM dos_extraction_results d": [
            (n, date(2024, 3, 15), date(2024, 3, 16), date(2024, 3, 15), date(2024, 3, 16), D("0.9500"))
            for n in ("1.jpg", "2.jpg")
        ],
        "FROM page_classification pc": [
            ("1.jpg", "Progress Note", "codeable", D("0.8000"), "Clinical Notes", "Cardiology", "0.7",
             {"specialty": "Cardiology", "specialty_confidence": 0.7, "l2": "Progress Note", "l2_confidence": 0.6}),
        ],
        "FROM encounter_type_results e": [("1.jpg", "outpatient_f2f", D("0.9000"))],
        "FROM page_sequencing_results s": [("1.jpg", 1, 1, 1, D("1.0000")), ("3.jpg", 3, 3, 2, D("0.6000"))],
        "FROM page_continuity_results k": [("1.jpg", {
            "document_seq": 1, "seq": 1, "position": "start", "relation": "new", "decided_by": "rule",
            "confidence_level": "high", "confidence": 0.9, "evidence": "header", "label": None})],
        "FROM provider_signature_results s": [("1.jpg", "Ann Lee", True, "MD", None, "v2", D("0.8000"))],
        "FROM imaging_final f": [
            ("1.jpg", "Clinical Notes", "codeable", date(2024, 3, 15), date(2024, 3, 16), "Progress Note",
             "Cardiology", "Ann Lee", "true", "MD", None, "v2", "0.8", {"dos": 0.9, "note": "x"}, ["dos"]),
        ],
    }
    ocr = {
        "tesseract": [("1.jpg", "hello one", None), ("2.jpg", "   ", None), ("3.jpg", "page three", None)],
        "docling": [("1.jpg", DOCLING_P1, None), ("2.jpg", None, "blank_junk_pass1"),
                    ("3.jpg", "plain final one", None)],
        "azuredocintel": [("1.jpg", "Azure text p1", None), ("2.jpg", None, None),
                          ("3.jpg", None, "high_quality_printed")],
    }

    def answer(sql: str, params) -> list[tuple]:
        if "count(p.id)" in sql:
            return []
        if "SELECT c.blob_container" in sql:
            return [("imaging", "Raw_Input/R1/B1/C1", "Processed/R1", f"{n}.jpg", n, False, f"pages/{n}.jpg")
                    for n in (1, 2, 3)]
        if "LEFT JOIN page_stage_status pss" in sql:
            return ocr[params[0]]
        if "NULL::text AS skip_reason" in sql:
            return ocr[params[1]]
        if "SELECT id, status, current_stage" in sql:
            return [status_row]
        if "SELECT id FROM chart_list" in sql:
            return [(cid,)]
        if "EXISTS(" in sql:
            # member, dos, quality, junk, codeable, encounter, sequencing
            return [(True, True, True, True, True, True, True)]
        if "FROM pipeline_stage s" in sql:
            return [("1.jpg",)]
        if "SELECT p.page_name, o.ocr_type" in sql:
            return flags
        if "SELECT p.page_number, p.page_name" in sql:
            return pages
        if "->> 'measure'" in sql:
            return [("JANE DOE", date(1950, 1, 2), "M1", "COL", "2024-01-01 to 2024-12-31")]
        if "FROM manifest_member_list" in sql:
            return [("JANE DOE", date(1950, 1, 2), "M1")]
        if "FROM member_verification_summary mv" in sql:
            return [("accepted", "JANE DOE", "M1", D("0.9500"), 1, 2, "name and dob")]
        for marker, rows in overlay.items():
            if marker in sql:
                return rows
        raise AssertionError(f"unexpected SQL: {sql[:80]}")

    assert mid
    return answer


class _Cursor:
    def __init__(self, answer):
        self.answer, self.rows = answer, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.rows = self.answer(sql, params)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, answer):
        self.answer = answer

    def cursor(self):
        return _Cursor(self.answer)


@pytest.fixture()
def both(containers, monkeypatch):
    ids = _write_chart(containers)
    answer = _postgres_rows(ids)
    pg = PostgresFolderRepository("postgresql://u:p@db:5432/x")

    @contextmanager
    def connect():
        yield _Conn(answer)

    monkeypatch.setattr(pg, "_connect", connect)
    monkeypatch.setattr(pg, "_table_exists", lambda table: True)
    monkeypatch.setattr(pg_mod, "attach_ground_truth", lambda pages, *a, **k: pages)
    cosmos = CosmosFolderRepository({k: v for k, v in containers.items() if k != "runs"})
    return pg, cosmos, containers, ids


# ---------------------------------------------------------------------------
# the same answers
# ---------------------------------------------------------------------------

def test_get_folder_matches_postgres(both):
    pg, cosmos, _c, _ids = both
    expected = pg.get_folder("C1")
    got = cosmos.get_folder("C1")
    assert got.model_dump() == expected.model_dump()
    assert got.ocr_processed == 3 and got.imaging_processed == 1
    assert [p.has_preliminary_ocr for p in got.pages] == [True, False, True]
    assert got.manifest.member == "JANE DOE" and got.manifest.dob == "1950-01-02"


@pytest.mark.parametrize("kind", ["preliminary", "final1", "final2"])
def test_get_ocr_text_matches_postgres(both, kind):
    pg, cosmos, _c, _ids = both
    expected = pg.get_ocr_text("C1", kind)
    pg_mod._OCR_TEXT_CACHE.clear()
    got = cosmos.get_ocr_text("C1", kind)
    assert got.model_dump() == expected.model_dump()


def test_get_imaging_matches_postgres(both):
    pg, cosmos, _c, _ids = both
    expected = pg.get_imaging("C1").model_dump()
    got = cosmos.get_imaging("C1").model_dump()
    for a, b in zip(got["pages"], expected["pages"]):
        assert a == b, a["fileName"]
    assert got == expected
    page1 = got["pages"][0]
    assert page1["encounterType"] == "Outpatient (F2F)" and page1["finalDosFrom"] == "2024-03-15"
    assert got["pages"][1]["blankOrJunk"] == "Yes (Blank)" and got["pages"][1]["dosFrom"] is None
    # No Partials since 2026-11-11: a Main page naming another is not one.
    assert got["pages"][2]["isPartialDuplicate"] is False and got["pages"][2]["duplicateOf"] == "1.jpg"
    assert got["verification"]["matchedMemberId"] == "M1"


def test_page_blob_location_matches_postgres(both, monkeypatch):
    pg, cosmos, _c, _ids = both
    from app.services import blob_store

    monkeypatch.setattr(blob_store, "list_image_blobs",
                        lambda container, prefix: [] if prefix.startswith("Processed")
                        else [(f"{prefix}/{n}.jpg", f"e{n}") for n in (1, 2, 3)])
    for n in (1, 2, 3):
        assert cosmos.resolve_page_blob("C1", n) == pg.resolve_page_blob("C1", n)
    assert cosmos.resolve_page_blob("C1", 2)["key"] == "Raw_Input/R1/B1/C1/2.jpg"
    assert cosmos.resolve_page_blob("C1", 9) is None and pg.resolve_page_blob("C1", 9) is None
    assert cosmos.no_blob_reason("C1", 9) == pg.no_blob_reason("C1", 9) == "no page_list row for this page"


# ---------------------------------------------------------------------------
# Cosmos specifics
# ---------------------------------------------------------------------------

def test_list_folders_reads_only_chart_documents(both):
    _pg, cosmos, containers, _ids = both
    # A manifest-only chart with no pages stays out, as in Postgres.
    containers["charts"].upsert_item({"id": "chart", "chart_name": "M9", "chart": {
        "id": 99, "chart_name": "M9", "source": "manifest", "page_count": 0, "status": "received"}})
    containers["pages"].queries.clear()
    rows = cosmos.list_folders()
    assert [r.name for r in rows] == ["C1"]
    assert rows[0].ocr_status == "IMAGING_IN_PROGRESS" and rows[0].page_count == 3
    assert rows[0].run_id == "R1" and isinstance(rows[0].last_updated_at, datetime)
    assert containers["pages"].queries == []


def test_page_documents_are_read_once_while_unchanged(both):
    _pg, cosmos, containers, ids = both
    cosmos.get_folder("C1")
    cosmos.get_imaging("C1")
    cosmos.get_ocr_text("C1", "final1")
    full_reads = [q for q, _pk in containers["pages"].queries if q == "SELECT * FROM c"]
    assert len(full_reads) == 1
    # A page changes: the next view reads them again.
    store = CosmosStore(containers)
    store.upsert_encounter(chart_id=ids["chart_id"], page_id=ids["pages"][2], encounter_type="inpatient")
    containers["pages"].queries.clear()  # the writer's own load
    assert cosmos.get_imaging("C1").pages[2].encounterType == "Inpatient"
    assert [q for q, _pk in containers["pages"].queries if q == "SELECT * FROM c"] == ["SELECT * FROM c"]


def test_unknown_chart_is_404_and_empty_imaging(both):
    _pg, cosmos, _c, _ids = both
    with pytest.raises(HTTPException) as err:
        cosmos.get_folder("nope")
    assert err.value.status_code == 404
    with pytest.raises(HTTPException) as err:
        cosmos.get_ocr_text("nope", "preliminary")
    assert err.value.status_code == 404
    doc = cosmos.get_imaging("nope")
    assert doc.pages == [] and doc.verification is None


def test_ground_truth_comes_from_the_ground_truth_container(both):
    _pg, cosmos, containers, _ids = both
    containers["ground_truth"].upsert_item({
        "id": "page-1", "chart_name": "C1", "page_number": 1, "source_page_id": None,
        "member_name": "Yes", "dos_from": "03/15/2024", "page_type": "Progress Note",
        "codeable": "Codeable", "entries": []})
    pages = cosmos.get_imaging("C1").pages
    assert pages[0].groundTruth is not None and pages[0].groundTruth.memberName == "Yes"
    assert pages[0].groundTruth.dosFrom == "03/15/2024"
    assert pages[1].groundTruth is None


def test_factory_picks_cosmos_only_for_db_backend_cosmos():
    from app.core.config import Settings
    from app.store.factory import build_repository

    base = dict(database_url="postgresql://u:p@db:5432/x", cosmos_endpoint="https://x.documents.azure.com:443/",
                cosmos_key="k", _env_file=None)
    assert isinstance(build_repository(Settings(db_backend="cosmos", **base)), CosmosFolderRepository)
    assert isinstance(build_repository(Settings(db_backend="both", **base)), PostgresFolderRepository)
    assert isinstance(build_repository(Settings(db_backend="postgres", **base)), PostgresFolderRepository)


def test_missing_cosmos_settings_are_a_503():
    repo = CosmosFolderRepository(endpoint="", key="")
    with pytest.raises(HTTPException) as err:
        repo.get_folder("C1")
    assert err.value.status_code == 503 and "COSMOS_ENDPOINT" in err.value.detail


def test_stamps_are_iso_strings_in_documents(containers):
    """What the conversion relies on: CosmosStore stores dates and times as strings."""
    _write_chart(containers)
    chart = containers["charts"].items[("C1", "chart")]["chart"]
    assert isinstance(chart["updated_at"], str)
    assert datetime.fromisoformat(chart["updated_at"]).tzinfo == timezone.utc
