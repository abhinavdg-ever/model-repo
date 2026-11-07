"""review-ui's own data on Cosmos (DB_BACKEND=cosmos, and the Cosmos copy under both).

Documents, all in database ``COSMOS_DATABASE``:

  ground_truth  {id: "page-<n>", chart_name, page_number, source_page_id, source_path,
                 <label columns>, user_id, created_at, updated_at,
                 entries: [{entry_mode, labels, source_path, user_id, created_at}]}
                (the shape db-utilities/dbutil/cosmos.py writes; a manual save sets the
                label columns and appends a manual_entry)
                {id: "chart", chart_name,
                 chart_review: {user_id, created_at, updated_at} | null,     (Reviewed mark)
                 chart_ground_truth: {member_decision, member_decision_reason, user_id,
                                      created_at, updated_at},
                 encounter_review: [{seq, start_page, end_page, page_type, encounter_type,
                                     dos_from, dos_to, user_id, created_at, updated_at}],
                 review_entries: [{action, fields, user_id, created_at}]}
                The chart-level review tables, keyed by chart name as in Postgres, so they
                survive a chart delete with the page ground truth.
  users         {id: "<users.id>", username, name, role, password_hash, is_active,
                 last_login_at, last_login_ip, last_login_user_agent, login_count,
                 created_at, updated_at, logins: [{ip_address, user_agent, created_at}]}
                pk /id; ``logins`` (the last 50) stands in for the user_login table.
  charts / pages / runs / manifest  as core-pipeline's CosmosStore writes them.

Each method mirrors the Postgres service of the same name; the services call them when
DB_BACKEND=cosmos (``app/services/backend.py``) and copy their writes here when it is both.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from app.core.schemas import (
    ChartGroundTruth,
    ChartGroundTruthUpdates,
    ChartReview,
    EncounterReview,
    EncounterReviewRow,
    GroundTruthUpdate,
    PageGroundTruth,
    ReviewLogEntry,
    ReviewLogResponse,
    StagedEncounter,
)
from app.store import rows as R
from app.store.cosmos.repository import CosmosFolderRepository, _TRUTH_COLUMNS, _as_datetime, _status

logger = logging.getLogger("review_ui.cosmos.review")

CHART_DOC = "chart"
META = "_meta"
SERVICE = "_service"
MAX_LOGINS = 50
_PEOPLE_TTL_SEC = 60.0
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

try:  # optimistic concurrency on replace; tests run without azure-core
    from azure.core import MatchConditions

    _IF_MATCH: Any = MatchConditions.IfNotModified
except ImportError:  # pragma: no cover
    _IF_MATCH = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _when(value: Any) -> datetime:
    """A stored time as an aware datetime (the epoch for none), for ordering."""
    dt = _as_datetime(value)
    if dt is None:
        return _EPOCH
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(value: Any) -> str | None:
    """A stored time as the Postgres services show it (datetime.isoformat())."""
    dt = _as_datetime(value)
    return dt.isoformat() if dt else None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def is_page_doc(doc: dict[str, Any]) -> bool:
    return str(doc.get("id") or "").startswith("page-") and doc.get("page_number") is not None


def entries(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The page's entries oldest first (ORDER BY created_at, id: ties keep append order)."""
    rows = [e for e in (doc.get("entries") or []) if isinstance(e, dict)]
    return sorted(rows, key=lambda e: _when(e.get("created_at")))


def truth_row(doc: dict[str, Any]) -> tuple:
    """A ground-truth page doc as a row of services/ground_truth._COLUMNS."""
    return (int(doc["page_number"]), R.json_text(doc.get("source_page_id")),
            *(R.json_text(doc.get(c)) for c in _TRUTH_COLUMNS))


class CosmosReviewStore:
    def __init__(self, repo: CosmosFolderRepository) -> None:
        self.repo = repo
        self._people: tuple[float, dict[int, tuple[str, str]]] | None = None
        self._lock = threading.Lock()

    # -- Cosmos access ------------------------------------------------------

    def c(self, name: str) -> Any:
        return self.repo._container(name)

    def query(self, container: str, sql: str, partition_key: str, parameters: list | None = None) -> list[dict]:
        return list(self.c(container).query_items(sql, parameters=parameters or [], partition_key=partition_key))

    def query_all(self, container: str, sql: str, parameters: list | None = None) -> list[Any]:
        """A cross-partition query: every partition is asked, so keep these rare and narrow."""
        return list(self.c(container).query_items(
            sql, parameters=parameters or [], enable_cross_partition_query=True))

    def read(self, container: str, doc_id: str, partition_key: str) -> dict[str, Any] | None:
        try:
            return self.c(container).read_item(doc_id, partition_key=partition_key)
        except Exception as exc:
            if _status(exc) == 404:
                return None
            raise

    def modify(self, container: str, doc_id: str, partition_key: str,
               change: Callable[[dict[str, Any] | None], dict[str, Any] | None]) -> dict[str, Any] | None:
        """Read, ``change`` (None: leave it), write back with an etag check; retried when
        another writer got there first."""
        for _ in range(8):
            current = self.read(container, doc_id, partition_key)
            new = change(None if current is None else dict(current))
            if new is None:
                return current
            for key in ("_rid", "_self", "_attachments", "_ts"):
                new.pop(key, None)
            etag = new.pop("_etag", None)
            try:
                if current is None:
                    return self.c(container).create_item(new)
                return self.c(container).replace_item(doc_id, new, etag=etag, match_condition=_IF_MATCH)
            except Exception as exc:
                if _status(exc) not in (409, 412):
                    raise
        raise RuntimeError(f"Cosmos {container}/{doc_id}: too many concurrent writes")

    # -- users --------------------------------------------------------------

    def people(self) -> dict[int, tuple[str, str]]:
        """users.id -> (username, display name: name, else username), cached a minute."""
        with self._lock:
            hit = self._people
            if hit and time.monotonic() - hit[0] < _PEOPLE_TTL_SEC:
                return hit[1]
        out: dict[int, tuple[str, str]] = {}
        for doc in self.query_all("users", "SELECT c.id, c.username, c.name FROM c"):
            uid = _int(doc.get("id"))
            if uid is None:
                continue
            username = str(doc.get("username") or "")
            out[uid] = (username, (str(doc.get("name") or "").strip() or username))
        with self._lock:
            self._people = (time.monotonic(), out)
        return out

    def display_name(self, user_id: Any) -> str | None:
        """COALESCE(NULLIF(btrim(u.name), ''), u.username), or None."""
        uid = _int(user_id)
        hit = self.people().get(uid) if uid is not None else None
        return hit[1] if hit else None

    def username(self, user_id: Any) -> str | None:
        uid = _int(user_id)
        hit = self.people().get(uid) if uid is not None else None
        return hit[0] if hit else None

    def find_user(self, username: str) -> dict[str, Any] | None:
        rows = self.query_all("users", "SELECT * FROM c WHERE LOWER(c.username) = @u",
                              [{"name": "@u", "value": (username or "").strip().lower()}])
        return min(rows, key=lambda d: _int(d.get("id")) or 0) if rows else None

    def record_login(self, user_id: int, ip_address: str | None, user_agent: str | None) -> None:
        now = _now()

        def change(doc):
            if doc is None:
                return None
            doc.update(last_login_at=now, last_login_ip=ip_address, last_login_user_agent=user_agent,
                       login_count=int(doc.get("login_count") or 0) + 1, updated_at=now)
            logins = list(doc.get("logins") or [])
            logins.append({"ip_address": ip_address, "user_agent": user_agent, "created_at": now})
            doc["logins"] = logins[-MAX_LOGINS:]
            return doc

        self.modify("users", str(user_id), str(user_id), change)

    def list_users(self) -> list[dict[str, Any]]:
        docs = self.query_all("users", "SELECT c.username, c.name, c.role, c.is_active, c.last_login_at FROM c")
        docs.sort(key=lambda d: str(d.get("username") or "").lower())
        return [
            {"username": d.get("username"), "name": d.get("name"), "role": d.get("role"),
             "isActive": bool(d.get("is_active")), "lastLoginAt": _iso(d.get("last_login_at"))}
            for d in docs
        ]

    def put_user(self, row: dict[str, Any]) -> dict[str, Any]:
        """Upsert a users row (DB_BACKEND=both copies Postgres rows here, ids kept)."""
        doc = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
        doc["id"] = str(row["id"])
        doc.setdefault("login_count", 0)
        doc.setdefault("created_at", _now())
        doc.setdefault("updated_at", doc["created_at"])
        existing = self.read("users", doc["id"], doc["id"])
        if existing and existing.get("logins"):
            doc.setdefault("logins", existing["logins"])
        self.c("users").upsert_item(doc)
        self._people = None
        return doc

    def create_user(self, *, username: str, name: str, role: str, password_hash: str,
                    user_id: int | None = None) -> dict[str, Any]:
        """A new users doc; its id from the counter core-pipeline's CosmosStore uses."""
        now = _now()
        uid = user_id if user_id is not None else self.take_id()
        return self.put_user({
            "id": uid, "username": username, "name": name, "role": role,
            "password_hash": password_hash, "is_active": True, "last_login_at": None,
            "last_login_ip": None, "last_login_user_agent": None, "login_count": 0,
            "created_at": now, "updated_at": now,
        })

    def set_password(self, username: str, password_hash: str) -> dict[str, Any] | None:
        found = self.find_user(username)
        if found is None:
            return None

        def change(doc):
            if doc is None:
                return None
            doc.update(password_hash=password_hash, updated_at=_now())
            return doc

        return self.modify("users", str(found["id"]), str(found["id"]), change)

    def take_id(self) -> int:
        """One id from the charts container's counter document (shared with core-pipeline)."""
        taken: list[int] = []

        def change(doc):
            taken.clear()
            if doc is None:
                doc = {"id": "counters", "chart_name": META, "next_id": 1}
            start = int(doc["next_id"])
            taken.append(start)
            doc["next_id"] = start + 1
            return doc

        self.modify("charts", "counters", META, change)
        return taken[0]

    # -- page ground truth --------------------------------------------------

    def truth_docs(self, chart: str) -> list[dict[str, Any]]:
        return [d for d in self.query("ground_truth", "SELECT * FROM c", chart) if is_page_doc(d)]

    def last_reviews(self, docs: Iterable[dict[str, Any]]) -> dict[int, tuple[str | None, str | None, str | None]]:
        """chart_review.last_reviews: page -> (who, when) of its last manual save, and when
        its last spreadsheet load was."""
        out: dict[int, tuple[str | None, str | None, str | None]] = {}
        for doc in docs:
            manual = [e for e in entries(doc) if e.get("entry_mode") == "manual_entry"]
            bulk = [e for e in entries(doc) if e.get("entry_mode") == "bulk_upload"]
            if not manual and not bulk:
                continue
            last = manual[-1] if manual else None
            out[int(doc["page_number"])] = (
                self.display_name(last.get("user_id")) if last else None,
                _iso(last.get("created_at")) if last else None,
                _iso(max((_when(e.get("created_at")) for e in bulk))) if bulk else None,
            )
        return out

    def ground_truth_by_page(self, chart: str) -> dict[int, PageGroundTruth]:
        from app.services.ground_truth import _ground_truth_from_row, _with_review

        docs = self.truth_docs(chart)
        try:
            reviews = self.last_reviews(docs)
        except Exception as exc:  # the labels still show
            logger.warning("last review lookup failed: %s", exc)
            reviews = {}
        out: dict[int, PageGroundTruth] = {}
        for doc in docs:
            try:
                parsed = _with_review(_ground_truth_from_row(truth_row(doc)), reviews)
            except Exception as exc:  # one bad doc does not hide the others
                logger.warning("ground_truth doc %s skipped for %s: %s", doc.get("id"), chart, exc)
                continue
            out[parsed.pageNumber] = parsed
        return out

    def upsert_page_ground_truth(self, chart: str, page_number: int, source_page_id: str | None,
                                 labels: dict[str, Any], user_id: int | None) -> PageGroundTruth:
        """ground_truth._UPSERT: set the label columns, keep a stored source page id when
        none is given, append a manual_entry with the labels."""
        from app.services.ground_truth import _ground_truth_from_row, _with_review

        saved = self._save_page(chart, page_number, source_page_id, labels, user_id)
        return _with_review(_ground_truth_from_row(truth_row(saved)), self.last_reviews([saved]))

    def _save_page(self, chart: str, page_number: int, source_page_id: str | None,
                   labels: dict[str, Any], user_id: int | None) -> dict[str, Any]:
        now = _now()

        def change(doc):
            if doc is None:
                doc = {"id": f"page-{page_number}", "chart_name": chart, "page_number": page_number,
                       "source_page_id": None, "source_path": None, "created_at": now, "entries": []}
            if source_page_id is not None:
                doc["source_page_id"] = source_page_id
            doc.update(labels)
            doc["user_id"] = user_id
            doc["updated_at"] = now
            doc["entries"] = list(doc.get("entries") or []) + [{
                "entry_mode": "manual_entry", "labels": dict(labels), "source_path": None,
                "user_id": user_id, "created_at": now,
            }]
            return doc

        return self.modify("ground_truth", f"page-{page_number}", chart, change)

    def save_page_sequences(self, chart: str, pages: list[tuple[int, str | None, str | None]],
                            user_id: int | None) -> None:
        """(page number, source page id, sequence) each: page_sequence only, as a manual_entry."""
        for number, source_page_id, value in pages:
            self._save_page(chart, number, source_page_id, {"page_sequence": value}, user_id)

    def latest_bulk_labels(self, charts: list[str]) -> dict[tuple[str, int], tuple[dict, datetime | None]]:
        out: dict[tuple[str, int], tuple[dict, datetime | None]] = {}
        for chart in dict.fromkeys(charts):
            for doc in self.truth_docs(chart):
                bulk = [e for e in entries(doc) if e.get("entry_mode") == "bulk_upload"]
                if bulk:
                    last = bulk[-1]
                    out[(str(chart), int(doc["page_number"]))] = (
                        {**(last.get("labels") or {}), "source_page_id": doc.get("source_page_id")},
                        _as_datetime(last.get("created_at")),
                    )
        return out

    def ground_truth_charts(self) -> list[str]:
        names = self.query_all(
            "ground_truth", "SELECT DISTINCT VALUE c.chart_name FROM c WHERE STARTSWITH(c.id, 'page-')")
        return sorted({str(n) for n in names if n})

    def ground_truth_labels(self, charts: list[str], source: str) -> list[tuple[str, int, dict]]:
        if source == "bulk_upload":
            bulk = self.latest_bulk_labels(charts)
            return [(chart, n, labels) for (chart, n), (labels, _at) in sorted(bulk.items())]
        out: list[tuple[str, int, dict]] = []
        for chart in sorted(set(charts)):
            for doc in sorted(self.truth_docs(chart), key=lambda d: int(d["page_number"])):
                labels = {k: v for k, v in doc.items() if k != "entries" and not k.startswith("_")}
                out.append((str(chart), int(doc["page_number"]), labels))
        return out

    def charts_with_ground_truth(self) -> set[str]:
        """Charts with a page holding any label value (ground_truth._labelled_row_sql). One
        cross-partition query that returns chart names only."""
        from app.services.ground_truth import LABEL_COLUMNS, _EMPTY

        cells = " OR ".join(
            f"(IS_NUMBER(c.{col}) OR (IS_STRING(c.{col}) AND NOT ARRAY_CONTAINS(@empty, LOWER(TRIM(c.{col})))))"
            for col in LABEL_COLUMNS
        )
        names = self.query_all(
            "ground_truth",
            f"SELECT DISTINCT VALUE c.chart_name FROM c WHERE STARTSWITH(c.id, 'page-') AND ({cells})",
            [{"name": "@empty", "value": sorted(_EMPTY)}],
        )
        return {str(n) for n in names if n}

    # -- chart-level review doc --------------------------------------------

    def chart_doc(self, chart: str) -> dict[str, Any]:
        return self.read("ground_truth", CHART_DOC, chart) or {}

    def modify_chart(self, chart: str, change: Callable[[dict[str, Any]], bool]) -> dict[str, Any]:
        """Change the chart's review doc in place (``change`` returns False to leave it)."""
        def apply(doc):
            doc = doc or {"id": CHART_DOC, "chart_name": chart, "created_at": _now()}
            if not change(doc):
                return None
            doc["updated_at"] = _now()
            return doc

        return self.modify("ground_truth", CHART_DOC, chart, apply) or {}

    @staticmethod
    def add_entry(doc: dict[str, Any], action: str, fields: list | None, user_id: int | None) -> None:
        doc["review_entries"] = list(doc.get("review_entries") or []) + [
            {"action": action, "fields": list(fields or []), "user_id": user_id, "created_at": _now()}]

    # chart_review

    def _review(self, doc: dict[str, Any]) -> ChartReview:
        mark = doc.get("chart_review")
        if not mark:
            return ChartReview()
        return ChartReview(reviewed=True, reviewedBy=self.display_name(mark.get("user_id")),
                           reviewedAt=_iso(mark.get("updated_at")), reviewedById=_int(mark.get("user_id")))

    def load_chart_review(self, chart: str) -> ChartReview:
        return self._review(self.chart_doc(chart))

    def reviewed_charts(self) -> dict[str, ChartReview]:
        docs = self.query_all("ground_truth",
                              "SELECT c.chart_name, c.chart_review FROM c WHERE c.id = 'chart'")
        return {str(d["chart_name"]): self._review(d) for d in docs if d.get("chart_review")}

    def set_chart_review(self, chart: str, reviewed: bool, user_id: int | None) -> ChartReview:
        def change(doc):
            marked = bool(doc.get("chart_review"))
            if marked == reviewed:
                return False  # marking a marked chart again is not a change
            now = _now()
            doc["chart_review"] = {"user_id": user_id, "created_at": now, "updated_at": now} if reviewed else None
            self.add_entry(doc, "reviewed" if reviewed else "review_cleared", None, user_id)
            return True

        return self._review(self.modify_chart(chart, change) or self.chart_doc(chart))

    # chart_ground_truth (member decision)

    def load_chart_ground_truth(self, chart: str) -> ChartGroundTruth:
        row = self.chart_doc(chart).get("chart_ground_truth")
        if not row:
            return ChartGroundTruth()
        return ChartGroundTruth(memberDecision=row.get("member_decision"),
                                memberDecisionReason=row.get("member_decision_reason"),
                                savedAt=_iso(row.get("updated_at")), savedBy=self.username(row.get("user_id")))

    def save_chart_ground_truth(self, chart: str, decision: str | None, reason: str | None,
                                user_id: int | None,
                                changes: Callable[[tuple, str | None, str | None], list]) -> ChartGroundTruth:
        def change(doc):
            row = dict(doc.get("chart_ground_truth") or {})
            before = (row.get("member_decision"), row.get("member_decision_reason"))
            now = _now()
            row.setdefault("created_at", now)
            row.update(member_decision=decision, member_decision_reason=reason, user_id=user_id, updated_at=now)
            doc["chart_ground_truth"] = row
            changed = changes(before, decision, reason)
            if changed:
                self.add_entry(doc, "member_decision", changed, user_id)
            return True

        self.modify_chart(chart, change)
        return self.load_chart_ground_truth(chart)

    # encounter_review

    def load_encounter_review(self, chart: str) -> EncounterReview:
        return self._encounters(self.chart_doc(chart))

    def _encounters(self, doc: dict[str, Any]) -> EncounterReview:
        rows = sorted((r for r in doc.get("encounter_review") or [] if isinstance(r, dict)),
                      key=lambda r: int(r.get("seq") or 0))
        latest = max(rows, key=lambda r: _when(r.get("updated_at"))) if rows else None
        return EncounterReview(
            rows=[EncounterReviewRow(seq=int(r["seq"]), startPage=int(r["start_page"]), endPage=int(r["end_page"]),
                                     pageType=r.get("page_type"), encounterType=r.get("encounter_type"),
                                     dosFrom=r.get("dos_from"), dosTo=r.get("dos_to")) for r in rows],
            savedAt=_iso(latest.get("updated_at")) if latest else None,
            savedBy=self.username(latest.get("user_id")) if latest else None,
        )

    def save_encounter_review(self, chart: str, rows: list[dict[str, Any]], user_id: int | None,
                              changes: Callable[[list, list], list]) -> EncounterReview:
        """Replace the chart's encounters with ``rows`` (seq, start_page, ... already cleaned)."""
        def change(doc):
            before = self._encounters(doc).rows
            now = _now()
            doc["encounter_review"] = [{**r, "user_id": user_id, "created_at": now, "updated_at": now} for r in rows]
            changed = changes(before, self._encounters(doc).rows)
            if changed:
                self.add_entry(doc, "encounters", changed, user_id)
            return True

        return self._encounters(self.modify_chart(chart, change))

    # ground_truth_updates

    def ground_truth_updates(self, chart: str) -> ChartGroundTruthUpdates:
        docs = self.truth_docs(chart)
        flat = sorted(((e, doc) for doc in docs for e in entries(doc)), key=lambda pair: _when(pair[0].get("created_at")))

        def newest(keep: Callable[[dict], bool]) -> GroundTruthUpdate | None:
            for entry, _doc in reversed(flat):
                if keep(entry):
                    if entry.get("entry_mode") == "bulk_upload":
                        return GroundTruthUpdate(bulk=True, at=_iso(entry.get("created_at")))
                    return GroundTruthUpdate(by=self.display_name(entry.get("user_id")), at=_iso(entry.get("created_at")))
            return None

        labels = lambda e: e.get("labels") if isinstance(e.get("labels"), dict) else {}  # noqa: E731
        bulk = newest(lambda e: e.get("entry_mode") == "bulk_upload") or GroundTruthUpdate()
        review = self.chart_doc(chart)
        enc_rows = [r for r in review.get("encounter_review") or [] if isinstance(r, dict)]
        enc = max(enc_rows, key=lambda r: _when(r.get("updated_at"))) if enc_rows else None
        member = review.get("chart_ground_truth") or None
        return ChartGroundTruthUpdates(
            page=newest(lambda e: any(k != "page_sequence" for k in labels(e))) or bulk,
            sequencing=newest(lambda e: "page_sequence" in labels(e)) or bulk,
            encounter=(GroundTruthUpdate(by=self.display_name(enc.get("user_id")), at=_iso(enc.get("updated_at")))
                       if enc else bulk),
            member=(GroundTruthUpdate(by=self.display_name(member.get("user_id")), at=_iso(member.get("updated_at")))
                    if member else bulk),
            missing=self._missing(chart, docs, bool(enc_rows), bool(member and member.get("member_decision"))),
        )

    def _missing(self, chart: str, docs: list[dict], has_encounters: bool, has_decision: bool) -> list[str]:
        fingerprint = self.repo._fingerprint(chart) if self.repo._chart_doc(chart) else ()
        pages = int(fingerprint[0]) if fingerprint else 0

        def filled(doc, col):
            return bool(str(doc.get(col) or "").strip(" "))

        detail = ("member_name", "member_dob", "dos_from", "blank_page", "junk_page", "encounter_type", "codeable")
        labelled = sum(1 for d in docs if any(filled(d, c) for c in detail))
        sequenced = sum(1 for d in docs if filled(d, "page_sequence"))
        out: list[str] = []
        if pages and labelled < pages:
            out.append(f"Page Details: {pages - labelled} of {pages} pages have no ground truth")
        if pages and sequenced < pages:
            out.append(
                "Page Sequencing: not saved" if sequenced == 0
                else f"Page Sequencing: {pages - sequenced} of {pages} pages have no sequence"
            )
        if not has_encounters:
            out.append("Encounter: no encounters saved")
        if not has_decision:
            out.append("Rejection Rules: no Accept / Reject decision")
        return out

    # review log

    def review_log(self, *, user_id: int | None, all_users: bool, limit: int, offset: int) -> ReviewLogResponse:
        """chart_review.review_log from the ground_truth container: the page docs with a
        manual entry (of the user), and the chart docs' review_entries. Cross-partition."""
        from app.services.chart_review import log_entries

        if not all_users and user_id is None:
            return ReviewLogResponse(items=[], total=0)
        where = "e.entry_mode = 'manual_entry'" + ("" if all_users else " AND e.user_id = @uid")
        params = [] if all_users else [{"name": "@uid", "value": user_id}]
        docs = self.query_all(
            "ground_truth",
            "SELECT * FROM c WHERE STARTSWITH(c.id, 'page-') AND "
            f"EXISTS(SELECT VALUE e FROM e IN c.entries WHERE {where})",
            params,
        )
        rows = []
        for doc in sorted((d for d in docs if is_page_doc(d)), key=lambda d: (str(d["chart_name"]), int(d["page_number"]))):
            key = (str(doc["chart_name"]), int(doc["page_number"]))
            for i, e in enumerate(entries(doc)):
                rows.append((i, key, doc["chart_name"], doc["page_number"], e.get("entry_mode"),
                             e.get("labels") or {}, _as_datetime(e.get("created_at")), _int(e.get("user_id")),
                             self.display_name(e.get("user_id"))))
        items = log_entries(rows, user_id=user_id, all_users=all_users)
        charts = self.query_all("ground_truth",
                                "SELECT c.chart_name, c.review_entries FROM c WHERE c.id = 'chart'")
        for doc in charts:
            for e in doc.get("review_entries") or []:
                if not all_users and _int(e.get("user_id")) != user_id:
                    continue
                items.append(ReviewLogEntry(chartName=str(doc["chart_name"]), at=_iso(e.get("created_at")) or "",
                                            user=self.display_name(e.get("user_id")), action=str(e.get("action")),
                                            fields=list(e.get("fields") or [])))
        users: list[dict[str, str | int]] = []
        if all_users:
            users = [{"id": uid, "name": shown} for uid, (_u, shown) in self.people().items()]
            users.sort(key=lambda u: str(u["name"]))
        items.sort(key=lambda item: item.at, reverse=True)
        return ReviewLogResponse(items=items[offset: offset + limit], total=len(items), users=users)

    # -- landing list -------------------------------------------------------

    def chart_tags(self) -> dict[str, list[Any]]:
        """chart name -> raw tags: the chart doc's ``tags`` (else its chart row's)."""
        docs = self.query_all(
            "charts",
            "SELECT c.chart_name, c.tags, c.chart.tags AS row_tags FROM c "
            "WHERE c.id = 'chart' AND (IS_ARRAY(c.tags) OR IS_ARRAY(c.chart.tags))",
        )
        out: dict[str, list[Any]] = {}
        for d in docs:
            tags = d.get("tags") if isinstance(d.get("tags"), list) else d.get("row_tags")
            if isinstance(tags, list) and tags:
                out[str(d["chart_name"])] = tags
        return out

    def manifest_metadata(self) -> list[tuple]:
        """(record_id, id, source_name, measure, rr_method, project_scope) per manifest doc."""
        docs = self.query_all(
            "manifest",
            "SELECT c.chart_name, c.member.id AS id, c.member.source_name AS source_name, "
            "c.member.measure AS measure, c.member.rr_method AS rr_method, "
            "c.member.project_scope AS project_scope FROM c",
        )
        return [(d.get("chart_name"), _int(d.get("id")) or 0, d.get("source_name"), d.get("measure"),
                 d.get("rr_method"), d.get("project_scope")) for d in docs]

    # -- pipeline output read through the chart views' cache ---------------

    def additional_rows(self, chart: str) -> dict[str, dict]:
        """additional_page_details rows by page name, from the page docs."""
        loaded = self.repo._chart(chart)
        return {loaded.names[int(d["page_id"])]: d["additional_details"]
                for d in loaded.pages if isinstance(d.get("additional_details"), dict)}

    def encounters(self, chart: str, labels: dict[str, str]) -> list[StagedEncounter]:
        """staged_continuity._ENCOUNTERS_SQL over the chart's page docs."""
        loaded = self.repo._chart(chart)
        groups: dict[int, list[dict]] = {}
        for d in loaded.pages:  # page order (page_number, id)
            seq = _int((d.get("encounter") or {}).get("encounter_seq"))
            if seq is not None:
                groups.setdefault(seq, []).append(d)
        out: list[StagedEncounter] = []
        for seq in sorted(groups):
            docs = groups[seq]
            first = docs[0]
            decision = first.get("encounter") or {}
            setting = str(decision.get("encounter_type") or "")
            family = next((l1 for l1 in (((d.get("classification") or {}).get("axes") or {}).get("l1") for d in docs
                                         if isinstance((d.get("classification") or {}).get("axes"), dict))
                           if l1 not in (None, "", "Unknown")), None)
            dos = first.get("dos") or {}
            try:
                out.append(StagedEncounter(
                    seq=seq, document=(first.get("continuity") or {}).get("document_seq"),
                    page_family=family, pages=[loaded.names[int(d["page_id"])] for d in docs],
                    dos_from=str(dos["date_of_service_from"]) if dos.get("date_of_service_from") else None,
                    dos_to=str(dos["date_of_service_to"]) if dos.get("date_of_service_to") else None,
                    encounter_type=setting or None, encounter_label=labels.get(setting, setting) or None,
                    confidence=decision.get("confidence"), decided_by=decision.get("decided_by"),
                    reason=decision.get("reason"), matched_keyword=decision.get("matched_keyword"),
                ))
            except ValueError:
                continue
        return out

    # -- chart delete -------------------------------------------------------

    def delete_chart(self, chart: str, user_id: int | None) -> dict[str, Any] | None:
        """Delete the chart's docs in charts, pages and runs; ground_truth and manifest stay.
        The deletion is recorded in runs (partition "_service", type chart_deletion).
        None when there is no chart doc."""
        doc = self.repo._chart_doc(chart)
        if doc is None:
            return None
        row = doc.get("chart") or {}
        for container in ("pages", "runs"):
            for item in self.query(container, "SELECT c.id FROM c", chart):
                try:
                    self.c(container).delete_item(item["id"], partition_key=chart)
                except Exception as exc:
                    if _status(exc) != 404:
                        raise
        try:
            self.c("charts").delete_item(CHART_DOC, partition_key=chart)
        except Exception as exc:
            if _status(exc) != 404:
                raise
        with self.repo._lock:
            self.repo._charts.pop(chart, None)
        self.c("runs").upsert_item({
            "id": f"deletion-{uuid.uuid4().hex}", "chart_name": SERVICE, "type": "chart_deletion",
            "deletion": {"chart_name": chart, "page_count": row.get("page_count"), "run_id": row.get("run_id"),
                         "batch_id": row.get("batch_id"), "user_id": user_id, "created_at": _now()},
        })
        return {"chart_id": _int(row.get("id")), "chart_name": chart, "page_count": row.get("page_count"),
                "run_id": row.get("run_id"), "batch_id": row.get("batch_id")}

    # -- OCR search ---------------------------------------------------------

    def chart_runs(self, names: Optional[list[str]] = None, run_id: str | None = None,
                   batch_id: str | None = None) -> dict[str, tuple[Any, Any]]:
        """chart name -> (run_id, batch_id), for the named charts or every chart."""
        sql = ("SELECT c.chart_name, c.chart.run_id AS run_id, c.chart.batch_id AS batch_id "
               "FROM c WHERE c.id = 'chart'")
        params: list = []
        if names is not None:
            sql += " AND ARRAY_CONTAINS(@names, c.chart_name)"
            params.append({"name": "@names", "value": list(names)})
        if run_id:
            sql += " AND c.chart.run_id = @run"
            params.append({"name": "@run", "value": run_id})
        if batch_id:
            sql += " AND c.chart.batch_id = @batch"
            params.append({"name": "@batch", "value": batch_id})
        return {str(d["chart_name"]): (d.get("run_id"), d.get("batch_id")) for d in self.query_all("charts", sql, params)}

    def page_count(self, chart: str) -> int:
        fingerprint = self.repo._fingerprint(chart)
        return int(fingerprint[0]) if fingerprint else 0
