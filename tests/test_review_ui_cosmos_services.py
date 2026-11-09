"""review-ui's own services on Cosmos (DB_BACKEND=cosmos), and the Cosmos copy of their
writes (DB_BACKEND=both).

Charts are written through core-pipeline's CosmosStore (test_review_ui_cosmos._write_chart)
into fake containers; review-ui's documents through the services themselves. Where a Postgres
path can be fed equivalent rows, both must answer the same.
"""
from __future__ import annotations

import copy
import itertools
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from common.db.cosmos_store import CosmosStore

from app.core.schemas import ChartGroundTruth, ChartReview, EncounterReviewRow, PageGroundTruth, PageSequenceLabel
from app.services import backend
from app.services.auth import hash_password, verify_password
from app.store.cosmos.repository import CosmosFolderRepository

from test_review_ui_cosmos import _write_chart


# ---------------------------------------------------------------------------
# fake Cosmos: enough SQL for CosmosStore and review-ui
# ---------------------------------------------------------------------------

class _Err(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(status_code)
        self.status_code = status_code


_TS = itertools.count(1_000_000)


def _labelled(doc: dict, empty: list[str]) -> bool:
    from app.services.ground_truth import LABEL_COLUMNS

    for col in LABEL_COLUMNS:
        value = doc.get(col)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if isinstance(value, str) and value.strip().lower() not in empty:
            return True
    return False


class Fake:
    def __init__(self, name: str, pk: str = "chart_name") -> None:
        self.name, self.pk = name, pk
        self.items: dict[tuple[str, str], dict] = {}
        self.queries: list[tuple[str, str | None]] = []
        self.fail_writes = False
        self._etag = 0

    def _key(self, doc: dict) -> tuple[str, str]:
        return (str(doc[self.pk]), str(doc["id"]))

    def _put(self, doc: dict) -> dict:
        if self.fail_writes:
            raise _Err(503)
        self._etag += 1
        stored = copy.deepcopy(doc)
        stored["_etag"] = str(self._etag)
        stored["_ts"] = next(_TS)
        self.items[self._key(doc)] = stored
        return copy.deepcopy(stored)

    def read_item(self, item: str, partition_key: str) -> dict:
        if (str(partition_key), item) not in self.items:
            raise _Err(404)
        return copy.deepcopy(self.items[(str(partition_key), item)])

    def create_item(self, doc: dict) -> dict:
        if self._key(doc) in self.items:
            raise _Err(409)
        return self._put(doc)

    def replace_item(self, item: str, doc: dict, etag=None, match_condition=None) -> dict:
        current = self.items.get(self._key(doc))
        if current is None:
            raise _Err(404)
        if etag is not None and current["_etag"] != etag:
            raise _Err(412)
        return self._put(doc)

    def upsert_item(self, doc: dict) -> dict:
        return self._put(doc)

    def delete_item(self, item: str, partition_key: str) -> None:
        if self.fail_writes:
            raise _Err(503)
        if self.items.pop((str(partition_key), item), None) is None:
            raise _Err(404)

    def query_items(self, query: str, parameters=None, partition_key=None, enable_cross_partition_query=None):
        self.queries.append((query, partition_key))
        p = {x["name"]: x["value"] for x in parameters or []}
        docs = [copy.deepcopy(d) for (pk, _), d in self.items.items() if partition_key is None or pk == partition_key]
        q = query
        top = p.get("@n")
        if "COUNT(1)" in q:
            return [{"n": len(docs), "ts": max((d["_ts"] for d in docs), default=None)}]
        if "c.chart.id = @id" in q:
            return [{"chart_name": d["chart_name"]} for d in docs if d.get("id") == "chart" and d["chart"]["id"] == p["@id"]]
        if "c.page_id = @id" in q:
            return [{"chart_name": d["chart_name"]} for d in docs if d.get("page_id") == p["@id"]]
        if "c.type = 'job'" in q:
            return [d for d in docs if d.get("type") == "job"]
        if "LOWER(c.username) = @u" in q:
            return [d for d in docs if str(d.get("username") or "").lower() == p["@u"]]
        if "DISTINCT VALUE c.chart_name" in q:
            rows = [d for d in docs if str(d["id"]).startswith("page-")]
            if "@empty" in p:
                rows = [d for d in rows if _labelled(d, p["@empty"])]
            return list(dict.fromkeys(d["chart_name"] for d in rows))
        if "EXISTS(SELECT VALUE e FROM e IN c.entries" in q:
            return [d for d in docs if str(d["id"]).startswith("page-") and any(
                e.get("entry_mode") == "manual_entry" and ("@uid" not in p or e.get("user_id") == p["@uid"])
                for e in d.get("entries") or [])]
        if "c.chart.tags AS row_tags" in q:
            return [{"chart_name": d["chart_name"], "tags": d.get("tags"), "row_tags": d["chart"].get("tags")}
                    for d in docs if d.get("id") == "chart"
                    and (isinstance(d.get("tags"), list) or isinstance(d["chart"].get("tags"), list))]
        if "c.chart.run_id AS run_id" in q:
            out = []
            for d in docs:
                if d.get("id") != "chart":
                    continue
                c = d["chart"]
                if ("@names" in p and d["chart_name"] not in p["@names"]) or \
                        ("@run" in p and c.get("run_id") != p["@run"]) or ("@batch" in p and c.get("batch_id") != p["@batch"]):
                    continue
                out.append({"chart_name": d["chart_name"], "run_id": c.get("run_id"), "batch_id": c.get("batch_id")})
            return out
        if "c.member.source_name" in q:
            return [{"chart_name": d["chart_name"], **{k: d["member"].get(k) for k in
                     ("id", "source_name", "measure", "rr_method", "project_scope") if k in d["member"]}} for d in docs]
        if "CONTAINS(c.ocr.tesseract.raw_text" in q:
            out = [{"chart_name": d["chart_name"], "page_number": d["page"].get("page_number"),
                    "page_name": d["page"]["page_name"], "text": d["ocr"]["tesseract"]["raw_text"]}
                   for d in docs if p["@w"].lower() in str(((d.get("ocr") or {}).get("tesseract") or {}).get("raw_text") or "").lower()
                   and ("@names" not in p or d["chart_name"] in p["@names"])]
            return out[:top]
        if "JOIN h IN c.additional_details.section_headers" in q:
            out = [{"chart_name": d["chart_name"], "page_number": d["page"].get("page_number"),
                    "page_name": d["page"]["page_name"], "text": h["text"]}
                   for d in docs for h in ((d.get("additional_details") or {}).get("section_headers") or [])
                   if p["@w"].lower() in str(h.get("text") or "").lower()
                   and ("@names" not in p or d["chart_name"] in p["@names"])]
            return out[:top]
        if "c.id = 'chart'" in q:
            return [d for d in docs if d.get("id") == "chart"]
        return docs


NAMES = ("charts", "pages", "runs", "manifest", "ground_truth")


@pytest.fixture()
def containers():
    out = {name: Fake(name) for name in NAMES}
    out["users"] = Fake("users", pk="id")
    out["metrics"] = Fake("metrics", pk="period")
    return out


@pytest.fixture()
def store(containers):
    return CosmosFolderRepository(containers).review


@pytest.fixture()
def cosmos(store, monkeypatch):
    """DB_BACKEND=cosmos with the fake containers."""
    monkeypatch.setattr(backend, "mode", lambda: "cosmos")
    backend.set_cosmos(store)
    yield store
    backend.set_cosmos(None)


@pytest.fixture()
def both(store, monkeypatch):
    """DB_BACKEND=both: Postgres answers, the fake containers get the copies."""
    monkeypatch.setattr(backend, "mode", lambda: "both")
    backend.set_cosmos(store)
    yield store
    backend.set_cosmos(None)


def _user(containers, uid: int, username: str, name: str = "", role: str = "reviewer", password: str = "secret-pw") -> None:
    containers["users"].upsert_item({
        "id": str(uid), "username": username, "name": name, "role": role,
        "password_hash": hash_password(password, iterations=1000), "is_active": True, "login_count": 0})


def _bulk_doc(containers, chart: str, page: int, labels: dict, at: str = "2026-11-01T10:00:00+00:00") -> None:
    """A page as db-utilities/dbutil/cosmos.py loads it."""
    containers["ground_truth"].upsert_item({
        "id": f"page-{page}", "chart_name": chart, "page_number": page, "source_page_id": f"{page}.jpg",
        "source_path": "gt.xlsx", **labels, "created_at": at, "updated_at": at,
        "entries": [{"entry_mode": "bulk_upload", "labels": labels, "source_path": "gt.xlsx",
                     "user_id": None, "created_at": at}],
    })


# ---------------------------------------------------------------------------
# a Postgres connection answering canned rows
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, answer, log):
        self.answer, self.log, self.rows = answer, log, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self.log.append((sql, params))
        self.rows = self.answer(sql, params)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class _Conn:
    def __init__(self, answer, log):
        self.answer, self.log = answer, log

    def cursor(self):
        return _Cursor(self.answer, self.log)

    def commit(self):
        pass

    def rollback(self):
        pass


def _postgres(monkeypatch, module, answer) -> list:
    log: list = []

    @contextmanager
    def connection(url, schema="public"):
        yield _Conn(answer, log)

    monkeypatch.setattr(module, "connection", connection)
    return log


PG = "postgresql://u:p@db:5432/x"
GT = dict(memberName="Yes", memberDob="No", dosFrom="03/15/2024", pageType="Progress Note",
          codeable="non codeable", providerSignature="yes", blankPage="No")


# ---------------------------------------------------------------------------
# 1. ground truth
# ---------------------------------------------------------------------------

def test_manual_save_writes_the_labels_and_a_manual_entry(cosmos, containers):
    from app.services import ground_truth as gt

    _user(containers, 7, "ann", "Ann Lee")
    _bulk_doc(containers, "C1", 1, {"member_name": "No", "rotation": "90"})
    saved = gt.upsert_page_ground_truth("", "C1", PageGroundTruth(pageNumber=1, **GT), user_id=7)

    doc = containers["ground_truth"].items[("C1", "page-1")]
    assert doc["member_name"] == "Yes" and doc["codeable"] == "Non-Codable" and doc["provider_signature"] == "Yes"
    assert doc["rotation"] is None  # a manual save sets every column it edits, as _UPSERT does
    assert doc["source_page_id"] == "1.jpg" and doc["source_path"] == "gt.xlsx"  # kept
    assert [e["entry_mode"] for e in doc["entries"]] == ["bulk_upload", "manual_entry"]
    manual = doc["entries"][-1]
    assert manual["user_id"] == 7 and manual["labels"]["member_name"] == "Yes"
    assert set(manual["labels"]) == set(gt._MANUAL_COLUMNS)
    assert saved.memberName == "Yes" and saved.codeable == "Non-Codable"
    assert saved.reviewedBy == "Ann Lee" and saved.reviewedAt and saved.bulkUploadedAt.startswith("2026-11-01T10:00")

    by_page = gt.ground_truth_by_page("", "C1")
    assert by_page[1].model_dump() == saved.model_dump()
    # Bulk view: the spreadsheet's labels, manual edits hidden.
    bulk = cosmos.latest_bulk_labels(["C1"])
    assert bulk[("C1", 1)][0] == {"member_name": "No", "rotation": "90", "source_page_id": "1.jpg"}
    assert gt.ground_truth_charts("") == ["C1"]
    assert gt.ground_truth_labels("", ["C1"], "bulk_upload")[0][2]["member_name"] == "No"
    assert gt.ground_truth_labels("", ["C1"], "latest")[0][2]["member_name"] == "Yes"


def test_cosmos_ground_truth_reads_as_postgres_does(cosmos, containers, monkeypatch):
    """ground_truth_by_page: the same PageGroundTruth from a doc as from the Postgres row."""
    from app.services import ground_truth as gt

    _user(containers, 7, "ann", "Ann Lee")
    gt.upsert_page_ground_truth("", "C1", PageGroundTruth(pageNumber=2, sourcePageId="2.jpg", **GT), user_id=7)
    got = gt.ground_truth_by_page("", "C1")[2]
    doc = containers["ground_truth"].items[("C1", "page-2")]

    monkeypatch.setattr(backend, "mode", lambda: "postgres")
    row = (2, "2.jpg", "Yes", "No", "03/15/2024", None, None, "Progress Note", "Non-Codable", "No", None, None,
           None, None, None, None, "Yes") + (None,) * 11
    _postgres(monkeypatch, gt, lambda sql, params: [row])
    monkeypatch.setattr(gt, "_last_reviews", lambda *a: {2: ("Ann Lee", doc["entries"][-1]["created_at"], None)})
    expected = gt.ground_truth_by_page(PG, "C1")[2]
    assert got.model_dump() == expected.model_dump()


def test_landing_flag_counts_only_labelled_pages(cosmos, containers):
    from app.services.ground_truth import charts_with_ground_truth

    _bulk_doc(containers, "C1", 1, {"member_name": "Yes"})
    _bulk_doc(containers, "C2", 1, {"member_name": " NA ", "codeable": ""})
    containers["ground_truth"].upsert_item({"id": "chart", "chart_name": "C3", "chart_review": None})
    assert charts_with_ground_truth(None) == {"C1"}


def test_page_sequences_are_saved_alone(cosmos, containers):
    from app.services import ground_truth as gt

    _bulk_doc(containers, "C1", 1, {"member_name": "Yes"})
    out = gt.save_page_sequences("", "C1", [PageSequenceLabel(pageNumber=1, pageSequence="J1"),
                                            PageSequenceLabel(pageNumber=2, pageSequence="1")], user_id=3)
    assert out[1].pageSequence == "J1" and out[1].memberName == "Yes" and out[2].pageSequence == "1"
    assert containers["ground_truth"].items[("C1", "page-2")]["entries"][-1]["labels"] == {"page_sequence": "1"}
    with pytest.raises(ValueError):
        gt.save_page_sequences("", "C1", [PageSequenceLabel(pageNumber=1, pageSequence="x")])


def test_both_writes_postgres_then_cosmos(both, containers, monkeypatch):
    from app.services import ground_truth as gt

    monkeypatch.setattr(gt, "ensure_entries", lambda *a: None)
    monkeypatch.setattr(gt, "_last_reviews", lambda *a: {})
    row = (1, None, "Yes") + (None,) * 25
    log = _postgres(monkeypatch, gt, lambda sql, params: [row] if "INSERT INTO page_ground_truth" in sql else [])
    saved = gt.upsert_page_ground_truth(PG, "C1", PageGroundTruth(pageNumber=1, memberName="Yes"), user_id=5)
    assert saved.memberName == "Yes"
    assert any("INSERT INTO page_ground_truth" in sql for sql, _ in log)
    doc = containers["ground_truth"].items[("C1", "page-1")]
    assert doc["member_name"] == "Yes" and doc["entries"][-1]["user_id"] == 5


def test_a_cosmos_failure_never_fails_a_both_save(both, containers, monkeypatch, caplog):
    from app.services import ground_truth as gt

    monkeypatch.setattr(gt, "ensure_entries", lambda *a: None)
    monkeypatch.setattr(gt, "_last_reviews", lambda *a: {})
    _postgres(monkeypatch, gt, lambda sql, params: [(1, None, "Yes") + (None,) * 25])
    containers["ground_truth"].fail_writes = True
    saved = gt.upsert_page_ground_truth(PG, "C1", PageGroundTruth(pageNumber=1, memberName="Yes"), user_id=5)
    assert saved.memberName == "Yes"
    assert containers["ground_truth"].items == {}
    assert "Cosmos copy of ground truth page save failed" in caplog.text


# ---------------------------------------------------------------------------
# 2. users
# ---------------------------------------------------------------------------

def test_users_on_cosmos(cosmos, containers):
    from app.core.config import Settings
    from app.services import auth, user_admin

    created = user_admin.create_user("", "", username="Priya", name="Priya N", password="long-enough", role="admin")
    assert created == {"username": "Priya", "name": "Priya N", "role": "admin"}
    (doc,) = containers["users"].items.values()
    assert doc["password_hash"].startswith("pbkdf2_sha256$600000$") and verify_password("long-enough", doc["password_hash"])
    assert int(doc["id"]) >= 1  # from the shared counter
    with pytest.raises(user_admin.UserError) as err:
        user_admin.create_user("", "", username="priya", name="", password="long-enough", role="viewer")
    assert err.value.status == 409

    settings = Settings(_env_file=None, db_backend="cosmos")
    assert auth.authenticate(settings, "PRIYA", "wrong-password", ip_address=None, user_agent=None) is None
    user = auth.authenticate(settings, "priya", "long-enough", ip_address="10.0.0.1", user_agent="ua")
    assert (user.id, user.username, user.name, user.role) == (int(doc["id"]), "Priya", "Priya N", "admin")
    stored = containers["users"].items[(doc["id"], doc["id"])]
    assert stored["login_count"] == 1 and stored["last_login_ip"] == "10.0.0.1" and len(stored["logins"]) == 1

    user_admin.reset_password("", "", username="PRIYA", password="another-one")
    assert auth.authenticate(settings, "priya", "another-one", ip_address=None, user_agent=None) is not None
    with pytest.raises(user_admin.UserError) as err:
        user_admin.reset_password("", "", username="nobody", password="another-one")
    assert err.value.status == 404
    listed = user_admin.list_users("", "")
    assert listed[0]["username"] == "Priya" and "password_hash" not in listed[0] and listed[0]["lastLoginAt"]


def test_both_copies_a_new_user_with_its_postgres_id(both, containers, monkeypatch):
    from app.services import user_admin

    row = {"id": 42, "username": "ravi", "name": "Ravi", "role": "reviewer", "password_hash": "pbkdf2_sha256$1$a$b",
           "is_active": True, "login_count": 0, "created_at": "2026-11-08T00:00:00+00:00"}

    def answer(sql, params):
        if "to_jsonb(u)" in sql:
            return [(row,)]
        return []

    _postgres(monkeypatch, user_admin, answer)
    user_admin.create_user(PG, "public", username="ravi", name="Ravi", password="long-enough", role="reviewer")
    assert containers["users"].items[("42", "42")]["password_hash"] == "pbkdf2_sha256$1$a$b"


# ---------------------------------------------------------------------------
# 3. landing list
# ---------------------------------------------------------------------------

def _two_charts(containers):
    _write_chart(containers)
    store = CosmosStore(containers)
    chart = store.upsert_chart(chart_name="C2", page_count=1, status="completed", source="blob", run_id="R1", batch_id="B2")
    store.upsert_pages(int(chart["id"]), [{"page_name": "1.jpg", "page_number": 1}])
    store.upsert_manifest_members([{"record_id": "C2", "member_name": "X", "member_dob": "1960-01-01",
                                    "external_member_id": "M2", "source_name": "Fax", "project_scope": "HEDIS"}])
    doc = containers["charts"].items[("C1", "chart")]
    containers["charts"].upsert_item({**doc, "tags": ["b", "A", "a"]})


def test_landing_list_matches_postgres(cosmos, containers, monkeypatch):
    from app.services import folder_list as fl

    _two_charts(containers)
    _user(containers, 7, "ann", "Ann Lee")
    _bulk_doc(containers, "C1", 1, {"member_name": "Yes"})
    cosmos.set_chart_review("C2", True, 7)
    params = fl.FolderListParams()
    got = fl.build_folder_list(database_url=None, data_root=None, full_local_rows=None, params=params)

    mark = cosmos.load_chart_review("C2")
    assert mark.reviewed and mark.reviewedBy == "Ann Lee" and mark.reviewedById == 7
    summaries = cosmos.repo.list_folders()
    metadata = {"C1": {"source_name": None, "measure": "COL", "rr_method": None, "project_scope": None},
                "C2": {"source_name": "Fax", "measure": None, "rr_method": None, "project_scope": "HEDIS"}}
    assert fl.chart_metadata_map(None) == metadata
    monkeypatch.setattr(backend, "mode", lambda: "postgres")
    monkeypatch.setattr(fl, "fetch_chart_summaries_from_db", lambda *a, **k: summaries)
    monkeypatch.setattr(fl, "chart_metadata_map", lambda *a, **k: metadata)
    monkeypatch.setattr(fl, "charts_with_ground_truth", lambda *a: {"C1"})
    monkeypatch.setattr(fl, "reviewed_charts", lambda *a: {"C2": mark})
    monkeypatch.setattr(fl, "chart_tags_map", lambda *a: {"C1": ["A", "b"]})
    expected = fl.build_folder_list(database_url=PG, data_root=None, full_local_rows=None, params=params)

    assert got == expected
    by_name = {row.name: row for row in got.items}
    assert by_name["C1"].tags == ["A", "b"] and by_name["C1"].ground_truth_available
    assert by_name["C2"].reviewed and by_name["C2"].project_scope == "HEDIS" and by_name["C1"].measure == "COL"
    # No page document is read for the list.
    assert not [q for q in containers["pages"].queries if q[0] == "SELECT * FROM c"]


def test_tags_match_the_postgres_cleaning(cosmos, containers):
    from app.services import chart_tags

    _two_charts(containers)
    assert chart_tags.chart_tags_map(None) == chart_tags._tag_map([("C1", ["b", "A", "a"])]) == {"C1": ["A", "b"]}


# ---------------------------------------------------------------------------
# 4. review log, chart-level reviews, encounters, continuity, extraction
# ---------------------------------------------------------------------------

def test_review_mark_freezes_and_is_logged(cosmos, containers):
    from app.services import chart_review

    _user(containers, 7, "ann", "Ann Lee")
    assert chart_review.load_chart_review("", "C1") == ChartReview()
    chart_review.set_chart_review("", "C1", True, user_id=7)
    chart_review.set_chart_review("", "C1", True, user_id=7)  # no change, no entry
    with pytest.raises(chart_review.ChartFrozen):
        chart_review.ensure_not_frozen("", "C1")
    assert set(chart_review.reviewed_charts("")) == {"C1"}
    chart_review.set_chart_review("", "C1", False, user_id=7)
    chart_review.ensure_not_frozen("", "C1")
    doc = containers["ground_truth"].items[("C1", "chart")]
    assert [e["action"] for e in doc["review_entries"]] == ["reviewed", "review_cleared"]


def test_review_log_matches_postgres(cosmos, containers, monkeypatch):
    from app.services import chart_review, ground_truth as gt

    _user(containers, 7, "ann", "Ann Lee")
    _user(containers, 8, "bo", "")
    _bulk_doc(containers, "C1", 1, {"member_name": "No", "codeable": "Codable"})
    gt.upsert_page_ground_truth("", "C1", PageGroundTruth(pageNumber=1, memberName="Yes", codeable="Codable"), user_id=7)
    gt.upsert_page_ground_truth("", "C2", PageGroundTruth(pageNumber=3, dosFrom="01/02/2024"), user_id=8)
    chart_review.set_chart_review("", "C1", True, user_id=7)
    got_all = chart_review.review_log("", user_id=7, all_users=True)
    got_mine = chart_review.review_log("", user_id=7)

    # The same entries as the Postgres rows.
    rows, actions = [], []
    for (chart, _id), doc in sorted(containers["ground_truth"].items.items()):
        if _id.startswith("page-"):
            for i, e in enumerate(doc["entries"]):
                uid = e["user_id"]
                rows.append((i, (chart, doc["page_number"]), chart, doc["page_number"], e["entry_mode"], e["labels"],
                             datetime.fromisoformat(e["created_at"]), uid,
                             {7: "Ann Lee", 8: "bo"}.get(uid)))
        else:
            for e in doc["review_entries"]:
                actions.append((chart, datetime.fromisoformat(e["created_at"]), e["action"], e["fields"],
                                "Ann Lee", e["user_id"]))
    monkeypatch.setattr(backend, "mode", lambda: "postgres")
    monkeypatch.setattr(chart_review, "ensure_entries", lambda *a: None)
    monkeypatch.setattr(chart_review, "_ensure_table", lambda *a: None)

    def answer(sql, params):
        if "to_regclass" in sql:
            return [(True,)]
        if "FROM page_ground_truth_entry e" in sql:
            # every entry of the pages the user saved by hand
            pages = {r[1] for r in rows if r[4] == "manual_entry" and (not params or r[7] == params[0])}
            return [r for r in rows if r[1] in pages]
        if "FROM chart_review_entry r" in sql:
            return [a[:5] for a in actions if not params or a[5] == params[0]]
        if "FROM users" in sql:
            return [(7, "Ann Lee"), (8, "bo")]
        raise AssertionError(sql)

    _postgres(monkeypatch, chart_review, answer)
    assert got_all == chart_review.review_log(PG, user_id=7, all_users=True)
    assert got_mine == chart_review.review_log(PG, user_id=7)
    assert [i.action for i in got_mine.items] == ["reviewed", "edit"]
    edit = got_mine.items[1]
    assert edit.fields == [{"field": "member_name", "before": "No", "after": "Yes"}]


def test_member_decision_encounters_and_updates(cosmos, containers):
    from app.services import chart_ground_truth, encounter_review, ground_truth_updates as updates

    _write_chart(containers)
    _user(containers, 7, "ann", "Ann Lee")
    _bulk_doc(containers, "C1", 1, {"member_name": "Yes", "page_sequence": "1"})
    first = updates.ground_truth_updates("", "C1")
    assert first.page.bulk and first.sequencing.bulk and first.encounter.bulk and first.member.bulk
    assert first.missing == ["Page Details: 2 of 3 pages have no ground truth",
                             "Page Sequencing: 2 of 3 pages have no sequence",
                             "Encounter: no encounters saved", "Rejection Rules: no Accept / Reject decision"]

    saved = chart_ground_truth.save_chart_ground_truth("", "C1", ChartGroundTruth(memberDecision="Reject",
                                                                                  memberDecisionReason="wrong member"), user_id=7)
    assert (saved.memberDecision, saved.memberDecisionReason, saved.savedBy) == ("reject", "wrong member", "ann")
    rows = [EncounterReviewRow(startPage=3, endPage=3, pageType="Lab"), EncounterReviewRow(startPage=1, endPage=2, dosFrom=" ")]
    review = encounter_review.save_encounter_review("", "C1", rows, user_id=7)
    assert [(r.seq, r.startPage, r.dosFrom, r.pageType) for r in review.rows] == [(1, 1, None, None), (2, 3, None, "Lab")]
    assert review.savedBy == "ann" and encounter_review.load_encounter_review(None, "C1") == review
    with pytest.raises(ValueError):
        encounter_review.save_encounter_review("", "C1", [EncounterReviewRow(startPage=2, endPage=1)])

    later = updates.ground_truth_updates("", "C1")
    assert later.encounter.by == "Ann Lee" and later.member.by == "Ann Lee"
    assert later.missing == ["Page Details: 2 of 3 pages have no ground truth",
                             "Page Sequencing: 2 of 3 pages have no sequence"]
    actions = [e["action"] for e in containers["ground_truth"].items[("C1", "chart")]["review_entries"]]
    assert actions == ["member_decision", "encounters"]


def test_encounters_and_extraction_from_page_docs(cosmos, containers):
    from app.services.extraction_review import load_extraction_pages_from_db
    from app.services.staged_continuity import load_encounters_from_db

    ids = _write_chart(containers)
    store = CosmosStore(containers)
    cid, (p1, p2, p3) = ids["chart_id"], ids["pages"]
    store.upsert_encounter(chart_id=cid, page_id=p1, encounter_type="outpatient_f2f", confidence=0.9,
                           encounter_seq=1, decided_by="rule", reason="pn")
    store.upsert_encounter(chart_id=cid, page_id=p2, encounter_type="outpatient_f2f", confidence=0.9, encounter_seq=1)
    store.upsert_encounter(chart_id=cid, page_id=p3, encounter_type="not_applicable", encounter_seq=2)
    store.upsert_page_classification(chart_id=cid, page_id=p2, page_subtype=None, classification_category="codeable",
                                     axes={"l1": "Progress Note"})
    store.upsert_additional_page_details(chart_id=cid, page_id=p1, fields={
        "printed_page_no": "1", "printed_page_total": "3",
        "section_headers": [{"text": "ASSESSMENT", "level": 2, "norm": {"left": 0.1, "top": 0.2, "width": 0.3, "height": 0.05}}]})

    got = load_encounters_from_db("", "public", "C1")
    assert [(e.seq, e.pages, e.encounter_label, e.document, e.page_family, e.dos_from) for e in got] == [
        (1, ["1.jpg", "2.jpg"], "Outpatient (F2F)", 1, "Progress Note", "2024-03-15"),
        (2, ["3.jpg"], None, None, None, None)]
    assert got[0].decided_by == "rule" and got[0].confidence == 0.9

    pages = load_extraction_pages_from_db("", "public", "C1")
    assert pages.available and list(pages.pages) == ["1.jpg"]
    page_no = next(f for f in pages.pages["1.jpg"].fields if f.id == "page_no")
    assert page_no.extracted == "1 of 3" and pages.pages["1.jpg"].section_headers[0].text


# ---------------------------------------------------------------------------
# 5. chart delete
# ---------------------------------------------------------------------------

def test_chart_delete_keeps_ground_truth_and_manifest(cosmos, containers):
    from app.services.chart_delete import ChartNotFound, delete_chart

    ids = _write_chart(containers)
    CosmosStore(containers).create_job(chart_id=ids["chart_id"], stage_name="ocr_prelim")
    _bulk_doc(containers, "C1", 1, {"member_name": "Yes"})
    cosmos.repo.get_folder("C1")  # cache the chart
    removed = delete_chart("", "C1", user_id=7)
    assert removed["chart_name"] == "C1" and removed["chart_id"] == ids["chart_id"] and removed["page_count"] == 3
    assert removed["kept"] == ["ground truth", "manifest", "files on disk"]
    assert not [k for k in containers["pages"].items if k[0] == "C1"]
    assert not [k for k in containers["runs"].items if k[0] == "C1"]
    assert ("C1", "chart") not in containers["charts"].items
    assert ("C1", "page-1") in containers["ground_truth"].items and containers["manifest"].items
    (log,) = [d for d in containers["runs"].items.values() if d.get("type") == "chart_deletion"]
    assert log["chart_name"] == "_service" and log["deletion"]["user_id"] == 7
    with pytest.raises(ChartNotFound):
        delete_chart("", "C1")
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        cosmos.repo.get_folder("C1")


# ---------------------------------------------------------------------------
# 6. search, accuracy, Manual Review
# ---------------------------------------------------------------------------

def test_ocr_search_matches_postgres(cosmos, containers, monkeypatch):
    from app.services import ocr_search

    ids = _write_chart(containers)
    CosmosStore(containers).upsert_additional_page_details(
        chart_id=ids["chart_id"], page_id=ids["pages"][2], fields={"section_headers": [{"text": "Page Three Notes"}]})
    got = ocr_search.search_tesseract("", "public", "three")
    assert got["total_pages"] == 1 and got["total_header_hits"] == 1 and got["charts"][0]["total_pages"] == 3

    monkeypatch.setattr(backend, "mode", lambda: "postgres")

    def answer(sql, params):
        if "o.ocr_type = 'tesseract'" in sql:
            return [("C1", "R1", "B1", 3, "3.jpg", "page three")]
        if "FROM additional_page_details a" in sql:
            return [("C1", "R1", "B1", 3, "3.jpg", "Page Three Notes")]
        if "FROM ocr_results o" in sql:
            return []
        if "count(p.id)" in sql:
            return [("C1", 3)]
        raise AssertionError(sql)

    _postgres(monkeypatch, ocr_search, answer)
    assert got == ocr_search.search_tesseract(PG, "public", "three")
    # A run filter that matches no chart finds nothing.
    monkeypatch.setattr(backend, "mode", lambda: "cosmos")
    assert ocr_search.search_tesseract("", "public", "three", run_id="R9")["total_pages"] == 0


def test_value_search_reads_cosmos(cosmos, containers):
    """2026-11-08 (user): Search by value on Cosmos, matched as on Postgres."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.config import Settings, get_settings
    from app.routers import search
    from app.routers.deps import current_user
    from app.services.auth import SignedInUser

    _write_chart(containers)
    cosmos.upsert_page_ground_truth("C1", 1, None, {"page_type": "Progress Note", "codeable": "Codable"}, None)
    app = FastAPI()
    app.include_router(search.router, prefix="/api")
    settings = Settings(_env_file=None, db_backend="cosmos", cosmos_endpoint="https://x", cosmos_key="k")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[current_user] = lambda: SignedInUser(id=1, username="a", name="a", role="admin")
    client = TestClient(app)

    res = client.post("/api/search/values", json={"conditions": [
        {"column": "page_type", "side": "prediction", "op": "is", "value": " clinical  NOTES "},
        {"column": "page_type", "side": "ground_truth", "op": "contains", "value": "progress"}]})
    assert res.status_code == 200, res.text
    found = res.json()
    assert found["total_pages"] == 1 and found["charts"][0]["chart"] == "C1"
    assert found["charts"][0]["total_pages"] == 3 and found["charts"][0]["run"] == "R1"
    page = found["charts"][0]["pages"][0]
    assert page["page_number"] == 1
    assert page["values"]["page_type"] == {"prediction": "Clinical Notes", "ground_truth": "Progress Note"}
    # a prediction-side Yes / No column, and "empty"
    res = client.post("/api/search/values", json={"conditions": [
        {"column": "provider_signature", "side": "prediction", "op": "is", "value": "yes"}]}).json()
    assert [p["page_number"] for p in res["charts"][0]["pages"]] == [1]
    res = client.post("/api/search/values", json={"conditions": [
        {"column": "page_type", "side": "ground_truth", "op": "empty"}]}).json()
    assert [p["page_number"] for p in res["charts"][0]["pages"]] == [2, 3]
    # another run: nothing
    res = client.post("/api/search/values", json={"run": "R9", "conditions": [
        {"column": "page_type", "side": "prediction", "op": "empty"}]}).json()
    assert res["total_pages"] == 0
    opts = client.get("/api/search/values/options", params={"column": "page_type", "side": "prediction"}).json()
    assert opts["options"] == [{"value": "Clinical Notes", "pages": 1}]
    assert client.get("/api/search/values/columns").status_code == 200


def test_the_accuracy_report_reads_cosmos(cosmos, containers, monkeypatch):
    """2026-11-08 (user): the Accuracy View on Cosmos, chart by chart from the imaging document;
    ground truth latest or bulk_upload only."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.config import Settings, get_settings
    from app.routers import accuracy
    from app.routers.deps import current_user
    from app.services import accuracy_cache
    from app.services.auth import SignedInUser

    ids = _write_chart(containers)
    CosmosStore(containers).set_chart_status(ids["chart_id"], "completed")   # a running chart is left out
    cosmos.upsert_page_ground_truth("C1", 1, None, {"page_type": "Progress Note", "blank_page": "No"}, None)
    cosmos.upsert_page_ground_truth("C1", 3, None, {"page_type": "Lab"}, None)
    # page 1 also had a spreadsheet load before the manual save
    doc = next(d for (pk, _), d in containers["ground_truth"].items.items() if d.get("page_number") == 1)
    doc["entries"].insert(0, {"entry_mode": "bulk_upload", "labels": {"page_type": "Consult"},
                              "created_at": "2026-11-01T00:00:00+00:00"})
    containers["ground_truth"].upsert_item(doc)

    app = FastAPI()
    app.include_router(accuracy.router, prefix="/api")
    settings = Settings(_env_file=None, db_backend="cosmos", cosmos_endpoint="https://x", cosmos_key="k")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[current_user] = lambda: SignedInUser(id=1, username="a", name="a", role="admin")
    client = TestClient(app)
    accuracy_cache.clear_report_cache()
    res = client.get("/api/accuracy", params={"refresh": True})
    assert res.status_code == 200, res.text
    chart = res.json()["charts"][0]
    assert chart["chartName"] == "C1" and chart["lastVerifiedAt"]
    truth = {p["pageNumber"]: (p.get("groundTruth") or {}).get("pageType") for p in chart["document"]["pages"]}
    assert truth == {1: "Progress Note", 2: None, 3: "Lab"}
    assert chart["document"]["pages"][0]["memberName"]           # pipeline values come along
    accuracy_cache.clear_report_cache()
    bulk = client.get("/api/accuracy", params={"ground_truth": "bulk_upload", "refresh": True}).json()
    truth = {p["pageNumber"]: (p.get("groundTruth") or {}).get("pageType") for p in bulk["charts"][0]["document"]["pages"]}
    assert truth == {1: "Consult", 2: None, 3: None}


def test_cosmos_summaries_are_reused_until_the_chart_changes(cosmos, containers, monkeypatch):
    """2026-11-09 (user): precomputed per-chart summaries in the metrics container, rebuilt only
    when the chart, its ground truth or its manifest documents changed."""
    from app.store.cosmos import summaries

    ids = _write_chart(containers)
    cosmos.upsert_page_ground_truth("C1", 1, None, {"page_type": "Progress Note"}, None)
    built = []
    real_build = summaries.build
    monkeypatch.setattr(summaries, "build", lambda r, c, f: built.append(c) or real_build(r, c, f))

    first = summaries.current(cosmos)
    assert built == ["C1"] and ("summary", "summary-C1") in containers["metrics"].items
    doc = summaries.document(first["C1"])
    assert {p.pageNumber: (p.groundTruth.pageType if p.groundTruth else None) for p in doc.pages} == \
        {1: "Progress Note", 2: None, 3: None}
    assert [r["page_number"] for r in summaries.value_rows(first["C1"])] == [1, 2, 3]

    summaries.current(cosmos)                       # nothing changed: reused
    assert built == ["C1"]
    cosmos.upsert_page_ground_truth("C1", 3, None, {"page_type": "Lab"}, None)
    again = summaries.current(cosmos)               # a ground-truth save: rebuilt
    assert built == ["C1", "C1"]
    assert summaries.document(again["C1"]).pages[2].groundTruth.pageType == "Lab"
    CosmosStore(containers).set_chart_status(ids["chart_id"], "completed")   # a pipeline write
    summaries.current(cosmos)
    assert built == ["C1", "C1", "C1"]

    # no metrics container (or it fails): built for the request, not saved, still answers
    containers["metrics"].fail_writes = True
    cosmos.upsert_page_ground_truth("C1", 2, None, {"page_type": "Lab"}, None)
    assert "C1" in summaries.current(cosmos)


def test_manual_review_reads_cosmos(cosmos, containers):
    from app.routers.excel_compare import _PipelineCharts
    from app.core.config import Settings

    _write_chart(containers)
    settings = Settings(_env_file=None, db_backend="cosmos", cosmos_endpoint="https://x", cosmos_key="k")
    charts = _PipelineCharts(cosmos.repo, settings, ["C1", "missing"])
    body = charts.load("C1")
    assert body and body.count(b"\n") >= 4  # header + 3 pages
    assert charts.load("missing") is None
    assert charts.last_run("C1").startswith(str(datetime.now(timezone.utc).year))
