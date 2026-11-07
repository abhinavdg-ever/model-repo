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

Reads DATABASE_URL and COSMOS_* from core-pipeline/.env. Re-running is safe: documents are
upserted. Users go to the `users` container in review-ui's shape (store/cosmos/review.py),
with the last 50 sign-ins from user_login.
"""
from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

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
    # A chart Cosmos already holds (e.g. one run with DB_BACKEND=cosmos) is replaced, not merged:
    # its page and job documents carry other ids.
    for container in ("charts", "pages", "runs"):
        for doc in list(store._c[container].query_items("SELECT c.id FROM c", partition_key=name)):
            store._c[container].delete_item(doc["id"], partition_key=name)
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
    for job in rows(conn, "SELECT * FROM pipeline_jobs WHERE chart_id = %s", (chart_id,)):
        mirror.jobs[int(job["id"])] = job
        mirror._flush_job(int(job["id"]))
    got = cosmos_counts(store, name)
    return got == expected, expected, got


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
    if not dry_run:
        for m in manifest:
            store._c["manifest"].upsert_item(_json({"id": f"m-{m['id']}", "chart_name": m["record_id"], "member": m}))
        gt_container = _ground_truth_container()
        for g in gt:
            doc = {k: v for k, v in g.items() if k != "id"}
            doc.update({"id": f"page-{int(g['page_number'])}", "entries": entries.get(int(g["id"]), [])})
            gt_container.upsert_item(_json(doc))
    return {"manifest_rows": len(manifest), "ground_truth_pages": len(gt)}


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

    db = CosmosClient(os.environ["COSMOS_ENDPOINT"], credential=os.environ["COSMOS_KEY"]).get_database_client(
        (os.environ.get("COSMOS_DATABASE") or "imaging").strip())
    return db.get_container_client(name)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    pick = ap.add_mutually_exclusive_group(required=True)
    pick.add_argument("--chart", action="append", help="chart name (repeatable)")
    pick.add_argument("--all", action="store_true", help="every chart in Postgres")
    ap.add_argument("--dry-run", action="store_true", help="Postgres counts only; write nothing")
    ap.add_argument("--skip-existing", action="store_true", help="skip charts Cosmos already has")
    ap.add_argument("--limit", type=int, help="at most this many charts")
    args = ap.parse_args(argv)

    import os

    os.environ["DB_BACKEND"] = "postgres"           # read Postgres whatever .env says
    from common.db import connect
    from common.db.cosmos_store import _containers, CosmosStore

    store = None if args.dry_run else CosmosStore(_containers())
    with connect() as conn:
        if args.all:
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
        extra = migrate_manifest_and_ground_truth(conn, store, [c["chart_name"] for c in charts], dry_run=args.dry_run)
        users = migrate_users(conn, dry_run=args.dry_run) if args.all else 0
    print(f"charts: {len(charts)} | copied {ok}, mismatched {bad}, skipped {skipped} | "
          f"manifest rows {extra['manifest_rows']}, ground-truth pages {extra['ground_truth_pages']}, users {users}"
          f"{' (dry run: nothing written)' if args.dry_run else ''}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
