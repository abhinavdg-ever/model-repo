"""db-utilities on Cosmos: manifest members and ground truth, against a fake container."""
from __future__ import annotations

import copy

from dbutil import cosmos


class _Err(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.status_code = code


class Fake:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.writes = 0

    def read_item(self, item, partition_key):
        if (partition_key, item) not in self.items:
            raise _Err(404)
        return copy.deepcopy(self.items[(partition_key, item)])

    def create_item(self, doc):
        self.items[(doc["chart_name"], doc["id"])] = copy.deepcopy(doc)

    def replace_item(self, item, doc, etag=None, match_condition=None):
        self.items[(doc["chart_name"], item)] = copy.deepcopy(doc)

    def upsert_item(self, doc):
        self.writes += 1
        self.items[(doc["chart_name"], doc["id"])] = copy.deepcopy(doc)

    def query_items(self, query, partition_key=None, **_):
        return [copy.deepcopy(d) for (pk, _), d in self.items.items() if pk == partition_key]


def _sink() -> cosmos.CosmosSink:
    return cosmos.CosmosSink({"charts": Fake(), "manifest": Fake(), "ground_truth": Fake()})


def test_manifest_members_match_on_member_id_and_keep_metadata_on_a_blank():
    s = _sink()
    first = cosmos.upsert_manifest_members(s, [{"record_id": "R1", "member_name": "A B", "external_member_id": "M1",
                                                "measure": "CBP"}])
    again = cosmos.upsert_manifest_members(s, [{"record_id": "R1", "member_name": "A B", "external_member_id": "M1",
                                                "measure": None, "rr_method": "Fax"}])
    assert (first, again) == ({"inserted": 1, "updated": 0}, {"inserted": 0, "updated": 1})
    (doc,) = s.c["manifest"].items.values()
    assert doc["chart_name"] == "R1" and doc["id"] == f"m-{doc['member']['id']}"
    assert (doc["member"]["measure"], doc["member"]["rr_method"]) == ("CBP", "Fax")


def test_ground_truth_writes_only_changes_and_keeps_a_bulk_history():
    s = _sink()
    row = {"chart_name": "C1", "page_number": 3, "source_page_id": "3.jpg", "page_type": "Lab", "source_path": "/x"}
    cols = lambda r: ("page_type",)  # noqa: E731
    assert cosmos.upsert_ground_truth(s, [row], cols) == 1
    assert cosmos.upsert_ground_truth(s, [row], cols) == 0          # same sheet again: nothing written
    assert cosmos.upsert_ground_truth(s, [{**row, "page_type": "Progress Note"}], cols) == 1
    doc = s.c["ground_truth"].items[("C1", "page-3")]
    assert doc["page_type"] == "Progress Note"
    assert [e["labels"]["page_type"] for e in doc["entries"]] == ["Lab", "Progress Note"]


def test_backend_setting(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "both")
    assert cosmos.wants_postgres() and cosmos.wants_cosmos()
    monkeypatch.setenv("DB_BACKEND", "cosmos")
    assert not cosmos.wants_postgres()
