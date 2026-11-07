"""Cosmos DB (NoSQL) backend for the storage layer (2026-11-07, user: Postgres -> Cosmos).

``CosmosStore`` is ``MemoryStore`` with persistence: the same 69 methods and the same
behaviour (the tests cover MemoryStore), plus

* **load on first use:** a chart's documents are read from Cosmos the first time any
  method touches that chart (by chart id, chart name or page id);
* **write-through:** after every method that writes, the documents of the chart it touched
  are rebuilt and the ones that changed are upserted (a page doc nobody changed costs no RU);
* **ids from Cosmos:** chart, page, job and manifest ids come from one counter document,
  taken in blocks with an etag check, so two processes never hand out the same id.

Documents (database ``COSMOS_DATABASE``; containers created 2026-11-07):

  charts   {id: "chart", chart_name, chart, member_summary}           pk /chart_name
           {id: "counters", chart_name: "_meta", next_id}             (id allocation)
  pages    {id: "page-<page_id>", chart_name, page_id, page, stages, ocr, quality,
            blank_junk, dos, classification, encounter, sequencing, continuity,
            additional_details, imaging_final, provider_signature, member_extraction}
  runs     {id: "job-<id>", chart_name, type: "job", job}   (chart_name "_service" when none)
           {id: "usage-<run>", chart_name, type: "usage", usage}
  manifest {id: "m-<id>", chart_name: <record_id>, member}

One process caches the charts it has touched. Charts are processed by one process at a time
(the API's background tasks, or one batch), which is what makes the cache safe.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from typing import Any, Callable, Optional

from common.db.memory_store import MemoryStore

logger = logging.getLogger(__name__)

META = "_meta"
SERVICE = "_service"
ID_BLOCK = 200

# MemoryStore attribute -> page-doc field. Keys are page_id, or a tuple starting with it.
PAGE_FIELDS = {
    "quality": "quality",
    "dos": "dos",
    "page_classifications": "classification",
    "encounters": "encounter",
    "sequencing": "sequencing",
    "continuity": "continuity",
    "additional_details": "additional_details",
    "imaging_final": "imaging_final",
    "provider_signatures": "provider_signature",
    "member_extractions": "member_extraction",
}
TUPLE_FIELDS = {"ocr": "ocr", "blank_junk": "blank_junk", "page_stages": "stages"}
WRITE_PREFIXES = ("upsert_", "set_", "init_", "reset_", "clear_", "delete_", "mark_", "revise_",
                  "create_", "update_", "heartbeat_", "insert_", "prune_")
CROSS_CHART = {"list_stages", "list_jobs", "list_run_usage_daily", "list_finished_chart_names",
               "list_manifest_members", "count_manifest_members", "upsert_manifest_member",
               "upsert_manifest_members", "create_job", "update_job", "heartbeat_job", "insert_run_usage"}


def _json(value: Any) -> Any:
    """Round-trip through JSON (dates, Decimals -> str) so a doc is what Cosmos stores."""
    return json.loads(json.dumps(value, default=str))


def _digest(doc: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(doc, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _tuple_key(key: Any) -> str:
    return "|".join(str(k) for k in key[1:])


class CosmosStore(MemoryStore):
    name = "cosmos"

    def __init__(self, containers: dict[str, Any]) -> None:
        super().__init__()
        self._c = containers
        self._loaded: set[str] = set()
        self._digests: dict[tuple[str, str], str] = {}  # (chart_name, doc id) -> digest
        self._page_docs: dict[str, set[str]] = {}       # chart_name -> page doc ids stored
        self._name_by_id: dict[int, str] = {}
        self._ids: list[int] = []
        self._io = threading.RLock()

    # -- ids ----------------------------------------------------------------

    def _alloc(self) -> int:
        with self._io:
            if not self._ids:
                self._ids = list(self._take_ids(ID_BLOCK))
            return self._ids.pop(0)

    def _take_ids(self, n: int) -> range:
        charts = self._c["charts"]
        for _ in range(20):
            try:
                doc = charts.read_item("counters", partition_key=META)
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "status_code", None) != 404:
                    raise
                try:
                    charts.create_item({"id": "counters", "chart_name": META, "next_id": 1})
                except Exception as create_exc:  # noqa: BLE001  (another process created it)
                    if getattr(create_exc, "status_code", None) != 409:
                        raise
                continue
            start = int(doc["next_id"])
            doc["next_id"] = start + n
            try:
                charts.replace_item("counters", doc, etag=doc.get("_etag"), match_condition=_IF_MATCH)
                return range(start, start + n)
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "status_code", None) != 412:   # someone else took a block
                    raise
        raise RuntimeError("could not allocate ids from Cosmos (counter contention)")

    # -- loading ------------------------------------------------------------

    def _chart_name_for_id(self, chart_id: Optional[int]) -> Optional[str]:
        if chart_id is None:
            return None
        chart_id = int(chart_id)
        if chart_id in self.charts:
            return self.charts[chart_id]["chart_name"]
        if chart_id not in self._name_by_id:
            rows = list(self._c["charts"].query_items(
                "SELECT c.chart_name FROM c WHERE c.id = 'chart' AND c.chart.id = @id",
                parameters=[{"name": "@id", "value": chart_id}], enable_cross_partition_query=True))
            if not rows:
                return None
            self._name_by_id[chart_id] = rows[0]["chart_name"]
        return self._name_by_id[chart_id]

    def _chart_name_for_page(self, page_id: Optional[int]) -> Optional[str]:
        if page_id is None:
            return None
        page = self.pages.get(int(page_id))
        if page is not None:
            return self._chart_name_for_id(page["chart_id"])
        rows = list(self._c["pages"].query_items(
            "SELECT c.chart_name FROM c WHERE c.page_id = @id",
            parameters=[{"name": "@id", "value": int(page_id)}], enable_cross_partition_query=True))
        return rows[0]["chart_name"] if rows else None

    def load_chart(self, chart_name: str, *, force: bool = False) -> None:
        """Read one chart's documents into the in-memory tables (once per process)."""
        if not chart_name or (chart_name in self._loaded and not force):
            return
        with self._io, self._lock:
            try:
                doc = self._c["charts"].read_item("chart", partition_key=chart_name)
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "status_code", None) != 404:
                    raise
                doc = None
            if doc is not None:
                chart = doc["chart"]
                cid = int(chart["id"])
                self.charts[cid] = chart
                self.charts_by_name[chart_name] = cid
                self._name_by_id[cid] = chart_name
                if doc.get("member_summary"):
                    self.member_summaries[cid] = doc["member_summary"]
                self._remember(chart_name, "chart", self._chart_doc(chart_name))
                stored: set[str] = set()
                for page_doc in self._c["pages"].query_items(
                        "SELECT * FROM c", partition_key=chart_name):
                    self._absorb_page(page_doc)
                    stored.add(page_doc["id"])
                    self._remember(chart_name, page_doc["id"], self._page_doc(chart_name, page_doc["page_id"]))
                self._page_docs[chart_name] = stored
            for m in self._c["manifest"].query_items("SELECT * FROM c", partition_key=chart_name):
                row = m["member"]
                mid = int(row["id"])
                self.manifest_members[mid] = row
                ids = self.manifest_by_record.setdefault(row["record_id"], [])
                if mid not in ids:
                    ids.append(mid)
                self._remember(chart_name, m["id"], self._manifest_doc(row))
            self._loaded.add(chart_name)

    def _absorb_page(self, doc: dict[str, Any]) -> None:
        pid = int(doc["page_id"])
        page = doc["page"]
        self.pages[pid] = page
        ids = self.pages_by_chart.setdefault(int(page["chart_id"]), [])
        if pid not in ids:
            ids.append(pid)
        for attr, field in PAGE_FIELDS.items():
            if doc.get(field) is not None:
                getattr(self, attr)[pid] = doc[field]
        for key, row in (doc.get("ocr") or {}).items():
            self.ocr[(pid, key)] = row
        for key, row in (doc.get("blank_junk") or {}).items():
            self.blank_junk[(pid, int(key))] = row
        for key, row in (doc.get("stages") or {}).items():
            stage, pass_no = key.rsplit("|", 1)
            self.page_stages[(pid, stage, int(pass_no))] = row

    # -- documents ----------------------------------------------------------

    def _chart_doc(self, chart_name: str) -> Optional[dict[str, Any]]:
        cid = self.charts_by_name.get(chart_name)
        if cid is None or cid not in self.charts:
            return None
        return _json({"id": "chart", "chart_name": chart_name, "chart": self.charts[cid],
                      "member_summary": self.member_summaries.get(cid)})

    def _page_doc(self, chart_name: str, page_id: int) -> Optional[dict[str, Any]]:
        page = self.pages.get(page_id)
        if page is None:
            return None
        doc: dict[str, Any] = {"id": f"page-{page_id}", "chart_name": chart_name, "page_id": page_id, "page": page}
        for attr, field in PAGE_FIELDS.items():
            doc[field] = getattr(self, attr).get(page_id)
        for attr, field in TUPLE_FIELDS.items():
            doc[field] = {_tuple_key(k): v for k, v in getattr(self, attr).items() if k[0] == page_id}
        return _json(doc)

    def _manifest_doc(self, row: dict[str, Any]) -> dict[str, Any]:
        return _json({"id": f"m-{row['id']}", "chart_name": row["record_id"], "member": row})

    def _remember(self, chart_name: str, doc_id: str, doc: Optional[dict[str, Any]]) -> None:
        if doc is not None:
            self._digests[(chart_name, doc_id)] = _digest(doc)

    def _save(self, container: str, chart_name: str, doc: dict[str, Any]) -> None:
        key = (chart_name, doc["id"])
        digest = _digest(doc)
        if self._digests.get(key) == digest:
            return
        self._c[container].upsert_item(doc)
        self._digests[key] = digest

    def flush_chart(self, chart_name: str, page_ids: Optional[set[int]] = None) -> None:
        """Upsert this chart's changed documents; delete page docs of pages that are gone.
        With ``page_ids``, only those pages' documents (and the chart doc) are rebuilt."""
        with self._io, self._lock:
            chart = self._chart_doc(chart_name)
            if chart is None:
                return
            self._save("charts", chart_name, chart)
            if page_ids:
                for pid in page_ids:
                    doc = self._page_doc(chart_name, pid)
                    if doc is not None:
                        self._save("pages", chart_name, doc)
                        self._page_docs.setdefault(chart_name, set()).add(doc["id"])
                return
            cid = self.charts_by_name[chart_name]
            live = set()
            for pid in list(self.pages_by_chart.get(cid, [])):
                doc = self._page_doc(chart_name, pid)
                if doc is None:
                    continue
                live.add(doc["id"])
                self._save("pages", chart_name, doc)
            for gone in self._page_docs.get(chart_name, set()) - live:
                try:
                    self._c["pages"].delete_item(gone, partition_key=chart_name)
                except Exception as exc:  # noqa: BLE001
                    if getattr(exc, "status_code", None) != 404:
                        raise
                self._digests.pop((chart_name, gone), None)
            self._page_docs[chart_name] = live

    def _flush_manifest(self, record_ids: set[str]) -> None:
        for rid in record_ids:
            for mid in self.manifest_by_record.get(rid, []):
                row = self.manifest_members.get(mid)
                if row is not None:
                    self._save("manifest", rid, self._manifest_doc(row))

    def _flush_job(self, job_id: Optional[int]) -> None:
        job = self.jobs.get(int(job_id)) if job_id is not None else None
        if job is None:
            return
        name = self._chart_name_for_id(job.get("chart_id")) or SERVICE
        self._save("runs", name, _json({"id": f"job-{job['id']}", "chart_name": name, "type": "job", "job": job}))

    def ping(self) -> str:
        """Read the charts container's properties: raises when Cosmos is unreachable."""
        return str(self._c["charts"].read()["id"])

    # -- routing ------------------------------------------------------------

    def _charts_in_call(self, name: str, kwargs: dict[str, Any], args: tuple) -> set[str]:
        names: set[str] = set()
        if kwargs.get("chart_name"):
            names.add(str(kwargs["chart_name"]))
        for key in ("chart_id",):
            if kwargs.get(key) is not None:
                names.add(self._chart_name_for_id(kwargs[key]) or "")
        if kwargs.get("page_id") is not None:
            names.add(self._chart_name_for_page(kwargs["page_id"]) or "")
        for pid in (kwargs.get("page_ids") or [])[:1]:
            names.add(self._chart_name_for_page(pid) or "")
        if args:
            first = args[0]
            if name in ("get_chart_by_name",) and isinstance(first, str):
                names.add(first)
            elif isinstance(first, int) and not isinstance(first, bool):
                names.add(self._chart_name_for_id(first) or "")
        names.discard("")
        return names

    def _wrap(self, name: str, method: Callable[..., Any]) -> Callable[..., Any]:
        writes = name.startswith(WRITE_PREFIXES)

        def call(*args: Any, **kwargs: Any) -> Any:
            if name in CROSS_CHART:
                return self._cross_chart(name, method, args, kwargs)
            charts = self._charts_in_call(name, kwargs, args)
            for chart_name in charts:
                self.load_chart(chart_name)
            result = method(*args, **kwargs)
            if writes:
                if name == "upsert_chart":
                    charts.add(str(kwargs.get("chart_name") or (args[0] if args else "")))
                one_page = kwargs.get("page_id")
                only = {int(one_page)} if one_page is not None and not name.startswith(("delete_", "reset_", "clear_", "prune_")) else None
                for chart_name in charts:
                    self.flush_chart(chart_name, only)
            return result

        return call

    def _cross_chart(self, name: str, method: Callable[..., Any], args: tuple, kwargs: dict) -> Any:
        if name in ("list_manifest_members", "count_manifest_members"):
            rid = kwargs.get("record_id") or (args[0] if args and isinstance(args[0], str) else None)
            if rid is None and kwargs.get("chart_id") is not None:
                rid = self._chart_name_for_id(kwargs["chart_id"])
            if rid:
                self.load_chart(str(rid))
            return method(*args, **kwargs)
        if name in ("upsert_manifest_member", "upsert_manifest_members"):
            members = kwargs.get("members") or (args[0] if args else None)
            rids = ({str(m["record_id"]) for m in members} if isinstance(members, list)
                    else {str(kwargs.get("record_id"))})
            for rid in rids:
                self.load_chart(rid)
            result = method(*args, **kwargs)
            self._flush_manifest(rids)
            return result
        if name == "list_finished_chart_names":
            for chart_name in (kwargs.get("names") or (args[0] if args else [])):
                self.load_chart(str(chart_name))
            return method(*args, **kwargs)
        if name in ("create_job", "update_job", "heartbeat_job"):
            result = method(*args, **kwargs)
            job_id = result if name == "create_job" else (kwargs.get("job_id") or (args[0] if args else None))
            self._flush_job(job_id)
            return result
        if name == "insert_run_usage":
            row = kwargs.get("row") or args[0]
            chart_name = str(row.get("chart_name") or SERVICE)
            self._c["runs"].upsert_item(_json({"id": f"usage-{uuid.uuid4().hex}", "chart_name": chart_name,
                                               "type": "usage", "usage": row}))
            return None
        if name == "list_jobs":
            chart_id = kwargs.get("chart_id")
            limit = int(kwargs.get("limit") or 50)
            if chart_id is not None:
                rows = self._c["runs"].query_items(
                    "SELECT * FROM c WHERE c.type = 'job'",
                    partition_key=self._chart_name_for_id(chart_id) or SERVICE)
            else:
                rows = self._c["runs"].query_items(
                    "SELECT * FROM c WHERE c.type = 'job'", enable_cross_partition_query=True)
            jobs = sorted((r["job"] for r in rows), key=lambda j: j["id"], reverse=True)
            return jobs[:limit]
        return method(*args, **kwargs)

    def __getattribute__(self, name: str) -> Any:
        attr = object.__getattribute__(self, name)
        if name.startswith("_") or not callable(attr) or name in _OWN:
            return attr
        if name in _ROUTED:
            return object.__getattribute__(self, "_wrap")(name, attr)
        return attr


_OWN = {"load_chart", "flush_chart", "ping"}
_ROUTED: set[str] = set()
_IF_MATCH: Any = None


def routed_methods() -> set[str]:
    """The MemoryStore methods CosmosStore loads before and saves after."""
    return {n for n in dir(MemoryStore) if not n.startswith("_") and callable(getattr(MemoryStore, n))
            and n not in ("sort_key",)}


_ROUTED.update(routed_methods())

_STORE: Optional[CosmosStore] = None
_STORE_LOCK = threading.Lock()


def get_cosmos_store() -> CosmosStore:
    """The process's CosmosStore, built from COSMOS_ENDPOINT / COSMOS_KEY / COSMOS_DATABASE."""
    global _STORE, _IF_MATCH
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = CosmosStore(_containers())
            logger.info("storage: Cosmos database %s", os.environ.get("COSMOS_DATABASE") or "imaging")
        return _STORE


# ---------------------------------------------------------------------------
# DB_BACKEND=both: Postgres is the source of truth, every write is copied to Cosmos
# ---------------------------------------------------------------------------

class MirrorStore(CosmosStore):
    """A CosmosStore that takes its ids from Postgres. ``mirror()`` is called after each
    Postgres write with that write's result; a chart Cosmos has not seen is first copied
    from Postgres (chart row and page rows)."""

    name = "cosmos-mirror"

    def _adopt_chart(self, row: dict[str, Any]) -> None:
        with self._lock:
            cid = int(row["id"])
            self.charts[cid] = _json(row)
            self.charts_by_name[row["chart_name"]] = cid
            self._name_by_id[cid] = row["chart_name"]
            self._loaded.add(row["chart_name"])

    def _adopt_pages(self, rows: list[dict[str, Any]]) -> None:
        with self._lock:
            for row in rows:
                pid, cid = int(row["id"]), int(row["chart_id"])
                self.pages[pid] = _json(row)
                ids = self.pages_by_chart.setdefault(cid, [])
                if pid not in ids:
                    ids.append(pid)

    def seed_from_postgres(self, conn: Any, chart_id: Optional[int]) -> Optional[str]:
        """Make sure Cosmos has this chart: from the cache, from Cosmos, else from Postgres."""
        if chart_id is None:
            return None
        name = self._chart_name_for_id(chart_id)
        if name:
            self.load_chart(name)
            if int(chart_id) in self.charts:
                return name
        from common.db import get_chart, list_pages

        row = get_chart(conn, int(chart_id))
        if row is None:
            return None
        self._adopt_chart(row)
        self._adopt_pages(list_pages(conn, int(chart_id)))
        self.flush_chart(row["chart_name"])
        return row["chart_name"]

    def mirror(self, name: str, conn: Any, args: tuple, kwargs: dict, result: Any) -> None:
        if name == "upsert_chart" and isinstance(result, dict):
            self.load_chart(result["chart_name"])
            self._adopt_chart(result)
            self.flush_chart(result["chart_name"])
            return
        chart_id = kwargs.get("chart_id", args[0] if args and isinstance(args[0], int) else None)
        chart_name = self.seed_from_postgres(conn, chart_id) if chart_id is not None else None
        if name == "upsert_pages" and isinstance(result, list):
            self._adopt_pages(result)
            if chart_name:
                self.flush_chart(chart_name)
            return
        if name == "create_job" and result is not None:
            with self._lock:
                fields = {k: v for k, v in kwargs.items()}
                self.jobs[int(result)] = _json({"id": int(result), **fields, "status": kwargs.get("status", "queued")})
            self._flush_job(int(result))
            return
        method = getattr(self, name, None)
        if method is None:
            return
        method(*args, **kwargs)


_MIRROR: Optional[MirrorStore] = None
_MIRROR_FAILURES = 0


def mirror_write(name: str, conn: Any, args: tuple, kwargs: dict, result: Any) -> None:
    """Copy one Postgres write to Cosmos. Never raises: a failure is logged and counted."""
    global _MIRROR, _MIRROR_FAILURES
    if not name.startswith(WRITE_PREFIXES):
        return
    try:
        with _STORE_LOCK:
            if _MIRROR is None:
                _MIRROR = MirrorStore(_containers())
        _MIRROR.mirror(name, conn, args, kwargs, result)
    except Exception as exc:  # noqa: BLE001 - the Postgres run must not fail on the copy
        _MIRROR_FAILURES += 1
        logger.warning("DB_BACKEND=both: Cosmos copy of %s failed (%d so far): %s",
                       name, _MIRROR_FAILURES, str(exc).splitlines()[0][:200])


def mirror_failures() -> int:
    return _MIRROR_FAILURES


def _containers() -> dict[str, Any]:
    from azure.cosmos import CosmosClient

    global _IF_MATCH
    from azure.core import MatchConditions

    _IF_MATCH = MatchConditions.IfNotModified
    endpoint = (os.environ.get("COSMOS_ENDPOINT") or "").strip()
    key = (os.environ.get("COSMOS_KEY") or "").strip()
    if not endpoint or not key:
        raise RuntimeError("DB_BACKEND=cosmos / both needs COSMOS_ENDPOINT and COSMOS_KEY in .env")
    database = CosmosClient(endpoint, credential=key).get_database_client(
        (os.environ.get("COSMOS_DATABASE") or "imaging").strip())
    return {name: database.get_container_client(name) for name in ("charts", "pages", "runs", "manifest")}
