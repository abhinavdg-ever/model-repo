#!/usr/bin/env python3
"""Copy the Postgres history into Cosmos DB (platform plan phase 7), with count checks.

For each chart it reads every result table from Postgres, places the rows exactly where
core-pipeline's CosmosStore keeps them, and writes the same documents CosmosStore writes
(chart doc, one doc per page, jobs, manifest), with the Postgres ids. Ground truth goes in
db-utilities' document shape. A chart is then read back from Cosmos and its counts compared
with Postgres; a mismatch is reported and the run exits 1.

    cd core-pipeline && source .venv/bin/activate          # Windows: .venv\\Scripts\\Activate.ps1
    python ../utilities/migrate_postgres_to_cosmos.py --dry-run --all        # counts only
    python ../utilities/migrate_postgres_to_cosmos.py --chart 52743839_44976074
    python ../utilities/migrate_postgres_to_cosmos.py --all                  # every chart
    python ../utilities/migrate_postgres_to_cosmos.py --all --skip-existing  # resume
    python ../utilities/migrate_postgres_to_cosmos.py --changed-since 2026-11-10  # incremental
    python ../utilities/migrate_postgres_to_cosmos.py --since-last           # since the last sync

Incremental (2026-11-14, user): --changed-since / --since-last copy only the charts with a
newer updated_at in Postgres (chart, pages, any result table, member summary, jobs, page
ground truth or its entries, manifest), plus every chart in the live mirror's failed list
(cosmos_mirror_failed.tsv in the log folder: DB_BACKEND=both copies that failed). Users are
copied when a user or a sign-in changed. --since-last remembers when the last successful sync
started (utilities/output/cosmos_sync_state.json); a sync with a count mismatch does not move
it, and synced charts leave the failed list. A changed chart is replaced in Cosmos whole.

Reads DATABASE_URL and COSMOS_* from core-pipeline/.env. Upsert (2026-11-14, user): every
document is upserted, then a chart's documents that Postgres no longer has are removed, so
re-running is always safe and a chart never disappears from Cosmos mid-copy. Besides charts,
pages, jobs, manifest and page ground truth it copies run_usage (runs, type usage), the
chart-level review tables (chart_review + entries, chart_ground_truth, encounter_review: the
ground_truth container's "chart" doc) and chart_deletion (runs, partition "_service"). Users go to the `users` container in review-ui's shape (store/cosmos/review.py),
with the last 50 sign-ins from user_login.
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

CORE = Path(__file__).resolve().parents[1] / "core-pipeline"
sys.path.insert(0, str(CORE))

try:
    from dotenv import load_dotenv

    load_dotenv(CORE / ".env", override=True)
except ImportError:
    pass

# Postgres table -> (MemoryStore attribute, key builder). Keys match MemoryStore's tables.
PAGE_TABLES: dict[str, tuple[str, Any]] = {
    "ocr_quality_results": ("quality", lambda r: r["page_id"]),
    "ocr_results": ("ocr", lambda r: (r["page_id"], r["ocr_type"])),
    "blank_junk_classification": ("blank_junk", lambda r: (r["page_id"], int(r["pass_no"]))),
    "page_stage_status": ("page_stages", lambda r: (r["page_id"], r["stage_name"], int(r["pass_no"]))),
    "dos_extraction_results": ("dos", lambda r: r["page_id"]),
    "page_classification": ("page_classifications", lambda r: r["page_id"]),
    "encounter_type_results": ("encounters", lambda r: r["page_id"]),
    "page_sequencing_results": ("sequencing", lambda r: r["page_id"]),
    "page_continuity_results": ("continuity", lambda r: r["page_id"]),
    "additional_page_details": ("additional_details", lambda r: r["page_id"]),
    "imaging_final": ("imaging_final", lambda r: r["page_id"]),
    "provider_signature_results": ("provider_signatures", lambda r: r["page_id"]),
    "member_extraction_results": ("member_extractions", lambda r: r["page_id"]),
}


def plain(value: Any) -> Any:
    """Decimal -> float (CosmosStore keeps floats); everything else as psycopg returns it."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def rows(conn: Any, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [{k: plain(v) for k, v in r.items()} for r in conn.execute(sql, params).fetchall()]


def postgres_counts(conn: Any, chart_id: int) -> dict[str, int]:
    out = {"pages": conn.execute("SELECT count(*) AS n FROM page_list WHERE chart_id = %s", (chart_id,)).fetchone()["n"]}
    for table in PAGE_TABLES:
        out[table] = conn.execute(f"SELECT count(*) AS n FROM {table} WHERE chart_id = %s", (chart_id,)).fetchone()["n"]
    return out


def cosmos_counts(store: Any, chart_name: str) -> dict[str, int]:
    """Counts read back from Cosmos through a fresh CosmosStore (what a new process would see)."""
    from common.db.cosmos_store import CosmosStore

    fresh = CosmosStore(store._c)
    fresh.load_chart(chart_name)
    cid = fresh.charts_by_name.get(chart_name)
    if cid is None:
        return {"pages": 0}
    pages = set(fresh.pages_by_chart.get(cid, []))
    out = {"pages": len(pages)}
    for table, (attr, _) in PAGE_TABLES.items():
        table_rows = getattr(fresh, attr)
        out[table] = sum(1 for key in table_rows if (key[0] if isinstance(key, tuple) else key) in pages)
    return out


def migrate_chart(conn: Any, store: Any, chart: dict[str, Any], *, dry_run: bool) -> tuple[bool, dict, dict]:
    from common.db.cosmos_store import MirrorStore

    chart_id, name = int(chart["id"]), chart["chart_name"]
    expected = postgres_counts(conn, chart_id)
    if dry_run:
        return True, expected, {}
    # Upsert (2026-11-14, user): every document is upserted first, then the chart's documents
    # Postgres no longer has (pages gone, jobs or usage rows with other ids, e.g. from a run with
    # DB_BACKEND=cosmos) are removed. The chart is never missing from Cosmos while it is copied.
    mirror = MirrorStore(store._c)
    mirror._adopt_chart(plain(dict(chart)))
    mirror._adopt_pages(rows(conn, "SELECT * FROM page_list WHERE chart_id = %s ORDER BY id", (chart_id,)))
    for table, (attr, key) in PAGE_TABLES.items():
        target = getattr(mirror, attr)
        for r in rows(conn, f"SELECT * FROM {table} WHERE chart_id = %s", (chart_id,)):
            target[key(r)] = r
    summary = conn.execute("SELECT * FROM member_verification_summary WHERE chart_id = %s", (chart_id,)).fetchone()
    if summary:
        mirror.member_summaries[chart_id] = plain(dict(summary))
    mirror.flush_chart(name)
    keep_pages = {f"page-{int(pid)}" for pid in mirror.pages_by_chart.get(chart_id, [])}
    keep_runs: set[str] = set()
    for job in rows(conn, "SELECT * FROM pipeline_jobs WHERE chart_id = %s", (chart_id,)):
        mirror.jobs[int(job["id"])] = job
        mirror._flush_job(int(job["id"]))
        keep_runs.add(f"job-{int(job['id'])}")
    keep_runs |= migrate_usage(conn, store, chart_id, name)
    prune(store._c["charts"], name, {"chart"})
    prune(store._c["pages"], name, keep_pages)
    prune(store._c["runs"], name, keep_runs)
    got = cosmos_counts(store, name)
    return got == expected, expected, got


_TABLES: Optional[set[str]] = None


def has_table(conn: Any, table: str) -> bool:
    """Whether the database has this table (review-ui creates its review tables on first use,
    so an older database may lack chart_ground_truth, encounter_review, chart_review ...)."""
    global _TABLES
    if _TABLES is None:
        _TABLES = {r["table_name"] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()").fetchall()}
    return table in _TABLES


def prune(container: Any, partition: str, keep: set[str]) -> int:
    """Delete the documents in one partition whose ids are not in ``keep`` (after the upserts)."""
    gone = 0
    for doc in list(container.query_items("SELECT c.id FROM c", partition_key=partition)):
        if doc["id"] not in keep:
            try:
                container.delete_item(doc["id"], partition_key=partition)
                gone += 1
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "status_code", None) != 404:
                    raise
    return gone


def migrate_usage(conn: Any, store: Any, chart_id: int, name: str) -> set[str]:
    """run_usage rows of one chart -> runs {id: "usage-pg-<id>", type: "usage", usage}. Returns the ids."""
    from common.db.cosmos_store import _json

    ids: set[str] = set()
    if not has_table(conn, "run_usage"):
        return ids
    usage = rows(conn, "SELECT * FROM run_usage WHERE chart_id = %s OR chart_name = %s", (chart_id, name))
    for u in usage:
        doc_id = f"usage-pg-{int(u['id'])}"
        store._c["runs"].upsert_item(_json({"id": doc_id, "chart_name": name, "type": "usage", "usage": u}))
        ids.add(doc_id)
    return ids


def chart_review_doc(conn: Any, name: str) -> Optional[dict[str, Any]]:
    """The ground_truth container's chart doc (review-ui store/cosmos/review.py) from Postgres:
    chart_review (Reviewed mark), chart_ground_truth (member decision), encounter_review rows,
    review_entries (chart_review_entry). None when the chart has none of them."""
    def many(table: str, sql: str) -> list[dict[str, Any]]:
        return rows(conn, sql, (name,)) if has_table(conn, table) else []

    def one(table: str, sql: str) -> Optional[dict[str, Any]]:
        found = many(table, sql)
        return found[0] if found else None

    review = one("chart_review", "SELECT user_id, created_at, updated_at FROM chart_review WHERE chart_name = %s")
    member = one("chart_ground_truth", "SELECT member_decision, member_decision_reason, user_id, created_at, "
                 "updated_at FROM chart_ground_truth WHERE chart_name = %s")
    encounters = many("encounter_review", "SELECT seq, start_page, end_page, page_type, encounter_type, dos_from, "
                      "dos_to, user_id, created_at, updated_at FROM encounter_review WHERE chart_name = %s ORDER BY seq")
    entries = many("chart_review_entry", "SELECT action, fields, user_id, created_at FROM chart_review_entry "
                   "WHERE chart_name = %s ORDER BY id")
    if not (review or member or encounters or entries):
        return None
    return {"id": "chart", "chart_name": name, "chart_review": review, "chart_ground_truth": member,
            "encounter_review": encounters, "review_entries": entries}


def migrate_deletions(conn: Any, *, dry_run: bool, since: Any = None) -> int:
    """chart_deletion rows -> runs (partition "_service") {id: "deletion-pg-<id>", type: "chart_deletion"}."""
    from common.db.cosmos_store import SERVICE, _json

    sql, params = "SELECT * FROM chart_deletion", ()
    if since is not None:
        sql, params = sql + " WHERE updated_at > %s", (since,)
    if not has_table(conn, "chart_deletion"):
        return 0
    deletions = rows(conn, sql + " ORDER BY id", params)
    if not dry_run:
        container = _container("runs")
        for d in deletions:
            container.upsert_item(_json({"id": f"deletion-pg-{int(d['id'])}", "chart_name": SERVICE,
                                         "type": "chart_deletion", "deletion": d}))
    return len(deletions)


def raise_id_counter(conn: Any, store: Any) -> int:
    """Move the Cosmos id counter past every Postgres id, so ids Cosmos hands out later never
    collide with the copied history. Returns the counter's next id."""
    from common.db import cosmos_store

    highest = max(
        conn.execute(f"SELECT COALESCE(max(id), 0) AS n FROM {table}").fetchone()["n"]
        for table in ("chart_list", "page_list", "pipeline_jobs", "manifest_member_list")
    )
    charts = store._c["charts"]
    store._take_ids(0)                      # creates the counter document when missing
    for _ in range(20):
        doc = charts.read_item("counters", partition_key=cosmos_store.META)
        if int(doc["next_id"]) > highest:
            return int(doc["next_id"])
        doc["next_id"] = int(highest) + 1
        try:
            charts.replace_item("counters", doc, etag=doc.get("_etag"), match_condition=cosmos_store._IF_MATCH)
            return int(doc["next_id"])
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status_code", None) != 412:
                raise
    raise RuntimeError("could not raise the Cosmos id counter")


def migrate_manifest_and_ground_truth(conn: Any, store: Any, names: list[str], *, dry_run: bool) -> dict[str, int]:
    """Manifest rows (record_id = chart name) and ground truth, for the given chart names."""
    from common.db.cosmos_store import _json

    manifest = rows(conn, "SELECT * FROM manifest_member_list WHERE record_id = ANY(%s)", (names,))
    gt = rows(conn, "SELECT * FROM page_ground_truth WHERE chart_name = ANY(%s)", (names,))
    entries: dict[int, list[dict[str, Any]]] = {}
    if gt:
        for e in rows(conn, "SELECT * FROM page_ground_truth_entry WHERE page_ground_truth_id = ANY(%s) ORDER BY id",
                      ([g["id"] for g in gt],)):
            entries.setdefault(int(e["page_ground_truth_id"]), []).append(
                {k: e.get(k) for k in ("entry_mode", "labels", "source_path", "user_id", "created_at")})
    reviews = {n: chart_review_doc(conn, n) for n in names}
    if not dry_run:
        keep_manifest: dict[str, set[str]] = {n: set() for n in names}
        for m in manifest:
            doc_id = f"m-{m['id']}"
            store._c["manifest"].upsert_item(_json({"id": doc_id, "chart_name": m["record_id"], "member": m}))
            keep_manifest.setdefault(m["record_id"], set()).add(doc_id)
        gt_container = _ground_truth_container()
        keep_gt: dict[str, set[str]] = {n: set() for n in names}
        for g in gt:
            doc = {k: v for k, v in g.items() if k != "id"}
            doc.update({"id": f"page-{int(g['page_number'])}", "entries": entries.get(int(g["id"]), [])})
            gt_container.upsert_item(_json(doc))
            keep_gt.setdefault(g["chart_name"], set()).add(doc["id"])
        for n, review in reviews.items():
            if review is not None:
                gt_container.upsert_item(_json(review))
                keep_gt.setdefault(n, set()).add("chart")
        # Upsert first, then drop what Postgres no longer has, chart by chart.
        for n in names:
            prune(store._c["manifest"], n, keep_manifest.get(n, set()))
            prune(gt_container, n, keep_gt.get(n, set()))
    return {"manifest_rows": len(manifest), "ground_truth_pages": len(gt),
            "chart_reviews": sum(1 for r in reviews.values() if r is not None)}


STATE_FILE = Path(__file__).resolve().parent / "output" / "cosmos_sync_state.json"

# Chart-keyed tables whose updated_at says the chart changed (besides PAGE_TABLES).
_CHART_ID_TABLES = ("page_list", "member_verification_summary", "pipeline_jobs")


def changed_charts(conn: Any, since: Any) -> tuple[set[int], set[str]]:
    """Charts with anything newer than ``since``: (chart ids in chart_list, chart names with
    newer ground truth / manifest, which may have no chart_list row yet)."""
    parts = ["SELECT id AS chart_id FROM chart_list WHERE updated_at > %(since)s"]
    for table in (*_CHART_ID_TABLES, *PAGE_TABLES):
        parts.append(f"SELECT DISTINCT chart_id FROM {table} WHERE updated_at > %(since)s AND chart_id IS NOT NULL")
    ids = {int(r["chart_id"]) for r in conn.execute(" UNION ".join(parts), {"since": since}).fetchall()}
    named = ["SELECT chart_name AS n FROM page_ground_truth WHERE updated_at > %(since)s",
             "SELECT record_id FROM manifest_member_list WHERE updated_at > %(since)s"]
    if has_table(conn, "page_ground_truth_entry"):
        named.append("SELECT g.chart_name FROM page_ground_truth_entry e JOIN page_ground_truth g "
                     "ON g.id = e.page_ground_truth_id WHERE e.updated_at > %(since)s")
    for table in ("chart_review", "chart_review_entry", "chart_ground_truth", "encounter_review", "run_usage"):
        if has_table(conn, table):
            named.append(f"SELECT chart_name FROM {table} WHERE updated_at > %(since)s")
    names = {r["n"] for r in conn.execute(" UNION ".join(named), {"since": since}).fetchall()}
    return ids, names


def users_changed(conn: Any, since: Any) -> bool:
    try:
        row = conn.execute(
            """SELECT EXISTS (SELECT 1 FROM users WHERE updated_at > %(since)s)
                   OR EXISTS (SELECT 1 FROM user_login WHERE created_at > %(since)s) AS changed""",
            {"since": since}).fetchone()
        return bool(row["changed"])
    except Exception:  # noqa: BLE001 - an older database without user_login
        conn.rollback()
        row = conn.execute("SELECT EXISTS (SELECT 1 FROM users WHERE updated_at > %(since)s) AS changed",
                           {"since": since}).fetchone()
        return bool(row["changed"])


def failed_path(override: str | None = None) -> Path:
    if override:
        return Path(override)
    from common.db.cosmos_store import mirror_failed_path

    return mirror_failed_path()


def read_failed(path: Path) -> tuple[set[str], int]:
    """Chart names in the failed list, and how many lines name no chart ('?')."""
    names, unknown = set(), 0
    if not path.is_file():
        return names, 0
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            if parts[1] == "?":
                unknown += 1
            elif parts[1]:
                names.add(parts[1])
    return names, unknown


def clear_failed(path: Path, synced: set[str], started: str) -> int:
    """Drop the lines for charts synced now, keeping any failure logged after the sync started."""
    if not path.is_file():
        return 0
    keep, dropped = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] in synced and parts[0] < started:
            dropped += 1
        elif line.strip():
            keep.append(line)
    path.write_text("".join(f"{line}\n" for line in keep), encoding="utf-8")
    return dropped


def load_state() -> dict[str, Any]:
    import json

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(started: str, charts: int) -> None:
    import json

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"last_sync_started": started, "charts": charts}, indent=2) + "\n",
                          encoding="utf-8")


def migrate_users(conn: Any, *, dry_run: bool) -> int:
    """Every users row as review-ui's Cosmos users doc (id = users.id as text, same hash)."""
    from common.db.cosmos_store import _json

    users = rows(conn, "SELECT * FROM users ORDER BY id")
    if dry_run or not users:
        return len(users)
    logins: dict[int, list[dict[str, Any]]] = {}
    try:
        for r in rows(conn, "SELECT user_id, ip_address, user_agent, created_at FROM user_login ORDER BY id"):
            logins.setdefault(int(r["user_id"]), []).append(
                {"ip_address": r.get("ip_address"), "user_agent": r.get("user_agent"), "created_at": r.get("created_at")})
    except Exception:  # noqa: BLE001 - an older database without user_login
        conn.rollback()
    container = _container("users")
    for u in users:
        doc = dict(u)
        doc["id"] = str(u["id"])
        doc.setdefault("login_count", 0)
        doc["logins"] = logins.get(int(u["id"]), [])[-50:]
        container.upsert_item(_json(doc))
    return len(users)


def _ground_truth_container() -> Any:
    return _container("ground_truth")


def _container(name: str) -> Any:
    import os

    from azure.cosmos import CosmosClient

    from common.db.cosmos_store import cosmos_credential

    db = CosmosClient(os.environ["COSMOS_ENDPOINT"], credential=cosmos_credential()).get_database_client(
        (os.environ.get("COSMOS_DATABASE") or "imaging").strip())
    return db.get_container_client(name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    pick = ap.add_mutually_exclusive_group(required=True)
    pick.add_argument("--chart", action="append", help="chart name (repeatable)")
    pick.add_argument("--all", action="store_true", help="every chart in Postgres")
    pick.add_argument("--changed-since", metavar="WHEN",
                      help="charts changed in Postgres after WHEN (YYYY-MM-DD or ISO time), plus failed copies")
    pick.add_argument("--since-last", action="store_true",
                      help="charts changed since the last successful sync, plus failed copies")
    ap.add_argument("--dry-run", action="store_true", help="Postgres counts only; write nothing")
    ap.add_argument("--skip-existing", action="store_true", help="skip charts Cosmos already has")
    ap.add_argument("--limit", type=int, help="at most this many charts")
    ap.add_argument("--failed-file", help="the live mirror's failed list (default: core-pipeline log folder)")
    args = ap.parse_args(argv)

    import os

    os.environ["DB_BACKEND"] = "postgres"           # read Postgres whatever .env says
    from common.db import connect
    from common.db.cosmos_store import _containers, CosmosStore

    store = None if args.dry_run else CosmosStore(_containers())
    incremental = bool(args.changed_since or args.since_last)
    failed_file = failed_path(args.failed_file)
    with connect() as conn:
        from datetime import timezone

        # UTC, as the failed list writes its times (compared as text).
        started = conn.execute("SELECT now() AS t").fetchone()["t"].astimezone(timezone.utc).isoformat(timespec="seconds")
        gt_only: set[str] = set()
        since = None
        sync_users = bool(args.all)
        if incremental:
            if args.since_last:
                since = load_state().get("last_sync_started")
                if not since:
                    print(f"No earlier sync recorded in {STATE_FILE}. Run --all once (or --changed-since).")
                    return 2
            else:
                since = args.changed_since
            ids, names = changed_charts(conn, since)
            failed, unknown = read_failed(failed_file)
            charts = rows(conn, "SELECT * FROM chart_list WHERE id = ANY(%s) OR chart_name = ANY(%s) ORDER BY id",
                          (sorted(ids), sorted(names | failed)))
            gt_only = (names | failed) - {c["chart_name"] for c in charts}
            sync_users = users_changed(conn, since)
            print(f"since {since}: {len(ids)} chart(s) with newer results, {len(names)} with newer ground truth / "
                  f"manifest, {len(failed)} in the failed list ({failed_file.name})")
            if unknown:
                print(f"  {unknown} failed copy line(s) name no chart: run --all to be sure")
        elif args.all:
            charts = rows(conn, "SELECT * FROM chart_list ORDER BY id")
        else:
            charts = rows(conn, "SELECT * FROM chart_list WHERE chart_name = ANY(%s) ORDER BY id", (args.chart,))
            missing = sorted(set(args.chart) - {c["chart_name"] for c in charts})
            if missing:
                print(f"not in Postgres: {', '.join(missing)}")
        if args.limit:
            charts = charts[: args.limit]
        if store is not None:
            print(f"Cosmos id counter at {raise_id_counter(conn, store)} (above every Postgres id)")
        ok = bad = skipped = 0
        for chart in charts:
            name = chart["chart_name"]
            if args.skip_existing and store is not None:
                store.load_chart(name)
                if name in store.charts_by_name:
                    skipped += 1
                    continue
            good, expected, got = migrate_chart(conn, store, chart, dry_run=args.dry_run)
            if args.dry_run:
                print(f"{name}: {expected['pages']} pages, {sum(v for k, v in expected.items() if k != 'pages')} result rows")
            elif good:
                ok += 1
                print(f"{name}: copied, counts match ({expected['pages']} pages)")
            else:
                bad += 1
                diff = {k: (expected.get(k), got.get(k)) for k in expected if expected.get(k) != got.get(k)}
                print(f"{name}: COUNT MISMATCH postgres vs cosmos {diff}")
        extra = migrate_manifest_and_ground_truth(
            conn, store, sorted({c["chart_name"] for c in charts} | gt_only), dry_run=args.dry_run)
        users = migrate_users(conn, dry_run=args.dry_run) if sync_users else 0
        deletions = (migrate_deletions(conn, dry_run=args.dry_run, since=since if incremental else None)
                     if (args.all or incremental) else 0)
    if not args.dry_run and not bad:
        synced = {c["chart_name"] for c in charts} | gt_only
        dropped = clear_failed(failed_file, synced, started)
        if dropped:
            print(f"failed list: {dropped} line(s) cleared")
        if incremental or args.all:
            save_state(started, len(charts))
            print(f"next --since-last starts from {started}")
    print(f"charts: {len(charts)} | copied {ok}, mismatched {bad}, skipped {skipped}"
          f"{f', ground-truth-only {len(gt_only)}' if gt_only else ''} | "
          f"manifest rows {extra['manifest_rows']}, ground-truth pages {extra['ground_truth_pages']}, "
          f"chart reviews {extra['chart_reviews']}, chart deletions {deletions}, users {users}"
          f"{' (dry run: nothing written)' if args.dry_run else ''}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
