"""Cosmos DB (NoSQL) writes for db-utilities (2026-11-07): manifests and ground truth.

Documents (database COSMOS_DATABASE, partition key /chart_name everywhere):

  manifest      {id: "m-<id>", chart_name: <record_id>, member: {...manifest_member_list row}}
                The shape core-pipeline's CosmosStore reads; ids come from the same counter
                document (charts container, id "counters", partition "_meta").
  ground_truth  {id: "page-<n>", chart_name, page_number, source_page_id, source_path,
                 <label columns>, created_at, updated_at,
                 entries: [{entry_mode, labels, source_path, user_id, created_at}]}
                One document per page: the page_ground_truth row plus its
                page_ground_truth_entry history.

The rules are the Postgres ones: a manifest member matches on MemberID, else on name + DOB,
and a blank metadata column keeps the stored value; a ground-truth row writes only the
columns its file has, and an unchanged page is not written (no new bulk_upload entry).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional, Sequence

from dbutil import db  # noqa: F401  (loads db-utilities/.env)

META = "_meta"
ID_BLOCK = 100
MANIFEST_FIELDS = ("member_name", "first_name", "middle_name", "last_name", "member_dob",
                   "external_member_id", "run_id", "batch_id", "source_file", "source_path")
MANIFEST_EXTRA = ("client_id", "project_id", "project_scope", "source_name", "rr_method", "measure", "service_note")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


class CosmosSink:
    """The containers db-utilities writes. Built from COSMOS_ENDPOINT / KEY / DATABASE."""

    def __init__(self, containers: dict[str, Any]) -> None:
        self.c = containers
        self._ids: list[int] = []

    def take_id(self) -> int:
        if not self._ids:
            self._ids = list(_take_ids(self.c["charts"], ID_BLOCK))
        return self._ids.pop(0)


@lru_cache(maxsize=1)
def sink() -> CosmosSink:
    from azure.cosmos import CosmosClient

    endpoint = (os.environ.get("COSMOS_ENDPOINT") or "").strip()
    if not endpoint:
        raise RuntimeError("DB_BACKEND=cosmos needs COSMOS_ENDPOINT in db-utilities/.env")
    database = CosmosClient(endpoint, credential=cosmos_credential("db-utilities/.env")).get_database_client(
        (os.environ.get("COSMOS_DATABASE") or "imaging").strip())
    return CosmosSink({n: database.get_container_client(n) for n in ("charts", "manifest", "ground_truth")})


def _take_ids(charts: Any, n: int) -> range:
    """A block of ids from the counter core-pipeline's CosmosStore also uses."""
    try:
        from azure.core import MatchConditions

        if_match: Any = MatchConditions.IfNotModified
    except ImportError:  # tests with a fake container
        if_match = None
    for _ in range(20):
        try:
            doc = charts.read_item("counters", partition_key=META)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status_code", None) != 404:
                raise
            try:
                charts.create_item({"id": "counters", "chart_name": META, "next_id": 1})
            except Exception as create_exc:  # noqa: BLE001
                if getattr(create_exc, "status_code", None) != 409:
                    raise
            continue
        start = int(doc["next_id"])
        doc["next_id"] = start + n
        try:
            charts.replace_item("counters", doc, etag=doc.get("_etag"), match_condition=if_match)
            return range(start, start + n)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status_code", None) != 412:
                raise
    raise RuntimeError("could not allocate ids from Cosmos (counter contention)")


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def _member_key(m: dict[str, Any]) -> tuple:
    mid = (m.get("external_member_id") or "").strip()
    if mid:
        return ("id", mid)
    return ("name", (m.get("member_name") or "").casefold(), str(m.get("member_dob") or ""))


def upsert_manifest_members(s: CosmosSink, members: list[dict[str, Any]]) -> dict[str, int]:
    inserted = updated = 0
    by_record: dict[str, list[dict[str, Any]]] = {}
    for m in members:
        by_record.setdefault(str(m["record_id"]), []).append(m)
    container = s.c["manifest"]
    for record_id, group in by_record.items():
        stored = {_member_key(d["member"]): d for d in container.query_items(
            "SELECT * FROM c", partition_key=record_id)}
        for m in group:
            doc = stored.get(_member_key(m))
            if doc is None:
                row = {"id": s.take_id(), "record_id": record_id, "created_at": _now()}
                inserted += 1
            else:
                row = dict(doc["member"])
                updated += 1
            row.update({f: m.get(f) for f in MANIFEST_FIELDS})
            for f in MANIFEST_EXTRA:          # a blank keeps what is stored
                if m.get(f) not in (None, ""):
                    row[f] = m[f]
                row.setdefault(f, None)
            row["updated_at"] = _now()
            new_doc = _json({"id": f"m-{row['id']}", "chart_name": record_id, "member": row})
            container.upsert_item(new_doc)
            stored[_member_key(m)] = new_doc
    return {"inserted": inserted, "updated": updated}


# ---------------------------------------------------------------------------
# ground truth
# ---------------------------------------------------------------------------

# 2026-11-14 (user): several provider names / credentials are a JSON array in Cosmos (review-ui
# app/store/cosmos/gt_lists.py reads them back as "MD, NP"); one value, Not Found: text.
LIST_COLUMNS = ("rendering_provider", "provider_credentials")


def _doc_value(column: str, value: Any) -> Any:
    if column not in LIST_COLUMNS or not isinstance(value, str):
        return value
    parts = [p.strip() for p in value.split(",") if p.strip()]
    return parts if len(parts) > 1 else value


def upsert_ground_truth(s: CosmosSink, rows: list[dict[str, Any]], columns_for) -> int:
    """``columns_for(row)`` = the label columns that row's file has. Returns pages written."""
    container = s.c["ground_truth"]
    written = 0
    by_chart: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_chart.setdefault(str(row["chart_name"]), []).append(row)
    for chart_name, group in by_chart.items():
        stored = {d["id"]: d for d in container.query_items("SELECT * FROM c", partition_key=chart_name)}
        for row in group:
            columns: Sequence[str] = columns_for(row)
            doc_id = f"page-{int(row['page_number'])}"
            doc = stored.get(doc_id)
            incoming_page_id = row.get("source_page_id")
            if doc is not None:
                same_page_id = (incoming_page_id or doc.get("source_page_id")) == doc.get("source_page_id")
                if same_page_id and all(doc.get(c) == _doc_value(c, row.get(c)) for c in columns):
                    continue                   # unchanged: no write, no bulk entry
                doc = dict(doc)
            else:
                doc = {"id": doc_id, "chart_name": chart_name, "page_number": int(row["page_number"]),
                       "created_at": _now(), "entries": []}
            if incoming_page_id is not None:
                doc["source_page_id"] = incoming_page_id
            for c in columns:
                doc[c] = _doc_value(c, row.get(c))
            doc["source_path"] = row.get("source_path")
            doc["updated_at"] = _now()
            last_bulk: dict[str, Any] = {}
            for entry in doc.get("entries") or []:
                if entry.get("entry_mode") == "bulk_upload":
                    last_bulk = entry.get("labels") or {}
            doc.setdefault("entries", []).append({
                "entry_mode": "bulk_upload",
                "labels": {**last_bulk, **{c: _doc_value(c, row.get(c)) for c in columns}},
                "source_path": row.get("source_path"),
                "user_id": None,
                "created_at": _now(),
            })
            container.upsert_item(_json(doc))
            stored[doc_id] = doc
            written += 1
    return written


def backend() -> str:
    value = (os.environ.get("DB_BACKEND") or "postgres").strip().casefold()
    if value not in ("postgres", "cosmos", "both"):
        raise ValueError(f"DB_BACKEND={value!r}: use postgres, cosmos or both")
    return value


def wants_postgres() -> bool:
    return backend() in ("postgres", "both")


def wants_cosmos() -> bool:
    return backend() in ("cosmos", "both")


__all__ = ["CosmosSink", "sink", "upsert_manifest_members", "upsert_ground_truth", "backend",
           "wants_postgres", "wants_cosmos"]
_ = Optional  # typing re-export guard


# How to sign in to Cosmos (2026-11-11, user): the account key (COSMOS_AUTH=key, the default) or
# the host's managed identity (COSMOS_AUTH=managed_identity: no key; COSMOS_CLIENT_ID, else
# AZURE_CLIENT_ID, names a user-assigned identity, blank = system-assigned). The identity needs
# the data-plane role "Cosmos DB Built-in Data Contributor" on the account (infra/main.bicep).
COSMOS_MANAGED_IDENTITY = frozenset({"managed_identity", "managed-identity", "managed_entity", "mi", "msi"})


def cosmos_auth() -> str:
    auth = (os.environ.get("COSMOS_AUTH") or "key").strip().lower()
    return "managed_identity" if auth in COSMOS_MANAGED_IDENTITY else "key"


def cosmos_credential(where: str = ".env") -> Any:
    """The credential CosmosClient takes: the key, or a ManagedIdentityCredential."""
    if cosmos_auth() == "managed_identity":
        from azure.identity import ManagedIdentityCredential

        client_id = (os.environ.get("COSMOS_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID") or "").strip()
        return ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
    key = (os.environ.get("COSMOS_KEY") or "").strip()
    if not key:
        raise RuntimeError(f"Cosmos needs COSMOS_KEY (or COSMOS_AUTH=managed_identity) in {where}")
    return key
