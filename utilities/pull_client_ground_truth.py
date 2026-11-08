#!/usr/bin/env python3
"""Bring the client's ground truth into our database (2026-11-08, user): back ours up, then
replace it, chart by chart, with what the client's review database holds.

    python utilities/pull_client_ground_truth.py                     # dry run: what would change
    python utilities/pull_client_ground_truth.py --apply             # back up, then copy
    python utilities/pull_client_ground_truth.py --apply --charts-file charts.txt   # only these
    python utilities/pull_client_ground_truth.py --list-backups
    python utilities/pull_client_ground_truth.py --restore gt_backup_20261108_093000  # undo

The ground truth is every table review-ui writes, all keyed by chart name:

  page_ground_truth (+ page_ground_truth_entry, its save history)   page labels
  chart_ground_truth                                                 member decision
  encounter_review                                                   the reviewer's encounters
  chart_review (+ chart_review_entry, the Review Log)                Reviewed mark

Which charts: every chart the client has any ground truth for (or only those in
``--charts-file``: one chart name per line, commas / spaces also separate, ``#`` comments).
For each, our rows in all six tables are deleted and the client's copied in, so a page the
client changed, cleared or added comes out as theirs. A chart the client has no ground truth
for keeps ours untouched.

Backup first, always (``--apply``): a schema ``gt_backup_<YYYYMMDD_HHMMSS>`` in our database
with a full copy of the six tables (every chart, not only the replaced ones), plus the same
tables as CSV files under ``--backup-dir`` (default utilities/output/gt_backup_<stamp>/).
``--restore <schema>`` puts a backup back: the six tables are emptied and refilled from it.
The copy runs in one transaction: it lands whole or not at all.

Who saved a row (user_id) is matched by username. A client account we do not have is left
empty (the row stays, without a name) unless ``--add-users``: those accounts are then created
here, inactive, so the Review Log shows their names and nobody can sign in with them.

Needs CLIENT_DATABASE_URL (the client's review database: read only) and DATABASE_URL (ours,
the one review-ui and core-pipeline use; also read from core-pipeline/.env).
Postgres only: run it with DB_BACKEND=postgres or both, then re-mirror to Cosmos if needed.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

REPO = Path(__file__).resolve().parents[1]

try:
    from dotenv import load_dotenv

    load_dotenv(REPO / "core-pipeline" / ".env")
except ImportError:
    pass

# Parents before children: inserted in this order, deleted in reverse.
TABLES = ("page_ground_truth", "page_ground_truth_entry", "chart_ground_truth",
          "encounter_review", "chart_review", "chart_review_entry")
CHART_TABLES = ("page_ground_truth", "chart_ground_truth", "encounter_review", "chart_review",
                "chart_review_entry")
BACKUP_PREFIX = "gt_backup_"
USER_FIELDS = ("username", "name", "email", "role", "password_hash")


def _connect(url: str):
    import psycopg
    from psycopg.rows import dict_row

    # autocommit: each ``with conn.transaction()`` below is a real transaction.
    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://"), row_factory=dict_row,
                           autocommit=True)


def _mask(url: str) -> str:
    return re.sub(r"//([^:/@]+)(:[^@]*)?@", r"//\1:***@", url)


def _tables(conn) -> set[str]:
    rows = conn.execute("SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema()").fetchall()
    return {r["table_name"] for r in rows}


def _columns(conn, table: str, schema: Optional[str] = None) -> list[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = COALESCE(%s, current_schema()) "
        "AND table_name = %s AND is_generated = 'NEVER' ORDER BY ordinal_position", (schema, table),
    ).fetchall()
    return [r["column_name"] for r in rows]


def read_chart_list(path: str) -> list[str]:
    names: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        names += [n for n in re.split(r"[,\s]+", line.split("#", 1)[0]) if n]
    return list(dict.fromkeys(names))


def map_user(user_id: Any, client_names: dict[int, str], our_ids: dict[str, int]) -> Optional[int]:
    """The client's user id as ours with the same username (None when we have no such user)."""
    return our_ids.get(client_names.get(user_id)) if user_id is not None else None


def client_charts(client, only: Optional[list[str]]) -> list[str]:
    """The charts the client has any ground truth for (limited to ``only`` when given)."""
    have = _tables(client)
    names: set[str] = set()
    for table in CHART_TABLES:
        if table in have:
            names |= {r["chart_name"] for r in client.execute(f"SELECT DISTINCT chart_name FROM {table}").fetchall()}
    if only is not None:
        names &= set(only)
    return sorted(names)


def _count(conn, table: str, charts: list[str]) -> int:
    if table == "page_ground_truth_entry":
        return conn.execute("SELECT count(*) AS n FROM page_ground_truth_entry e JOIN page_ground_truth g "
                            "ON g.id = e.page_ground_truth_id WHERE g.chart_name = ANY(%s)", (charts,)).fetchone()["n"]
    return conn.execute(f"SELECT count(*) AS n FROM {table} WHERE chart_name = ANY(%s)", (charts,)).fetchone()["n"]


def summary(client, ours, charts: list[str]) -> list[tuple[str, int, int]]:
    """(table, our rows, the client's rows) on these charts."""
    present_client, present_ours = _tables(client), _tables(ours)
    return [(t, _count(ours, t, charts) if t in present_ours else 0,
             _count(client, t, charts) if t in present_client else 0) for t in TABLES]


# ---------------------------------------------------------------------------
# backup / restore
# ---------------------------------------------------------------------------

def backup(ours, stamp: str, out_dir: Path) -> str:
    """Every row of the six tables into schema gt_backup_<stamp> and CSV files. Returns the schema."""
    taken = set(list_backups(ours))
    schema = f"{BACKUP_PREFIX}{stamp}"
    n = 1
    while schema in taken:            # two runs in the same second
        n += 1
        schema = f"{BACKUP_PREFIX}{stamp}_{n}"
    present = _tables(ours)
    out_dir.mkdir(parents=True, exist_ok=True)
    with ours.transaction():
        ours.execute(f'CREATE SCHEMA "{schema}"')
        for table in TABLES:
            if table not in present:
                continue
            ours.execute(f'CREATE TABLE "{schema}".{table} AS TABLE {table}')
            with (out_dir / f"{table}.csv").open("wb") as fh, \
                    ours.cursor().copy(f"COPY {table} TO STDOUT WITH (FORMAT csv, HEADER true)") as copy:
                for block in copy:
                    fh.write(bytes(block))
    return schema


def list_backups(ours) -> list[str]:
    rows = ours.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE %s "
                        "ORDER BY schema_name", (BACKUP_PREFIX + "%",)).fetchall()
    return [r["schema_name"] for r in rows]


def restore(ours, schema: str) -> dict[str, int]:
    """Empty the six tables and refill them from a backup schema (ids kept)."""
    if schema not in list_backups(ours):
        raise SystemExit(f"no backup schema {schema!r} (see --list-backups)")
    counts: dict[str, int] = {}
    with ours.transaction():
        present = _tables(ours)
        for table in reversed(TABLES):
            if table in present:
                ours.execute(f"DELETE FROM {table}")
        for table in TABLES:
            if table not in present or not _columns(ours, table, schema):
                continue
            cols = [c for c in _columns(ours, table, schema) if c in set(_columns(ours, table))]
            names = ", ".join(cols)
            counts[table] = ours.execute(
                f'INSERT INTO {table} ({names}) SELECT {names} FROM "{schema}".{table}').rowcount
            _reset_sequence(ours, table)
    return counts


def _reset_sequence(conn, table: str) -> None:
    conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                 f"GREATEST((SELECT COALESCE(max(id), 0) FROM {table}), 1))")


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------

def _users(client, ours, add_users: bool) -> tuple[dict[int, str], dict[str, int], list[str]]:
    client_rows = client.execute(f"SELECT id, {', '.join(USER_FIELDS)} FROM users").fetchall()
    client_names = {r["id"]: r["username"] for r in client_rows}
    our_ids = {r["username"]: r["id"] for r in ours.execute("SELECT id, username FROM users").fetchall()}
    missing = [r for r in client_rows if r["username"] not in our_ids]
    if add_users:
        for r in missing:
            made = ours.execute(
                f"INSERT INTO users ({', '.join(USER_FIELDS)}, is_active) VALUES "
                f"({', '.join(['%s'] * len(USER_FIELDS))}, false) RETURNING id",
                # email is unique: an added account gets none (ours may already use it).
                tuple(None if f == "email" else r[f] for f in USER_FIELDS)).fetchone()
            our_ids[r["username"]] = made["id"]
    return client_names, our_ids, [r["username"] for r in missing]


def _rows(conn, table: str, charts: list[str]) -> list[dict[str, Any]]:
    return conn.execute(f"SELECT * FROM {table} WHERE chart_name = ANY(%s) ORDER BY id", (charts,)).fetchall()


def _insert(ours, table: str, columns: list[str], rows: Iterable[tuple]) -> list[int]:
    sql = (f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) RETURNING id")
    ids: list[int] = []
    with ours.cursor() as cur:
        for row in rows:
            ids.append(cur.execute(sql, row).fetchone()["id"])
    return ids


def copy_ground_truth(client, ours, charts: list[str], add_users: bool = False) -> dict[str, Any]:
    """Replace our ground truth of ``charts`` with the client's, in one transaction."""
    from psycopg.types.json import Jsonb

    present_client, present_ours = _tables(client), _tables(ours)
    counts: dict[str, Any] = {}
    with ours.transaction():
        client_names, our_ids, missing = _users(client, ours, add_users)
        counts["client_users_not_here"] = len(missing)

        def value(column: str, raw: Any) -> Any:
            if column == "user_id":
                return map_user(raw, client_names, our_ids)
            return Jsonb(raw) if isinstance(raw, (dict, list)) else raw

        # Ours out (page_ground_truth_entry goes with its page row: ON DELETE CASCADE).
        for table in reversed(CHART_TABLES):
            if table in present_ours:
                counts[f"deleted.{table}"] = ours.execute(
                    f"DELETE FROM {table} WHERE chart_name = ANY(%s)", (charts,)).rowcount

        # Theirs in. Ids are ours; the page rows' new ids carry their history across.
        new_page_ids: dict[int, int] = {}
        for table in CHART_TABLES:
            if table not in present_client or table not in present_ours:
                continue
            target = set(_columns(ours, table))
            columns = [c for c in _columns(client, table) if c in target and c != "id"]
            rows = _rows(client, table, charts)
            ids = _insert(ours, table, columns, (tuple(value(c, r[c]) for c in columns) for r in rows))
            counts[f"copied.{table}"] = len(ids)
            if table == "page_ground_truth":
                new_page_ids = {r["id"]: i for r, i in zip(rows, ids)}
        if new_page_ids and "page_ground_truth_entry" in present_client:
            target = set(_columns(ours, "page_ground_truth_entry"))
            columns = [c for c in _columns(client, "page_ground_truth_entry")
                       if c in target and c not in ("id", "page_ground_truth_id")]
            history = client.execute("SELECT * FROM page_ground_truth_entry WHERE page_ground_truth_id = ANY(%s) "
                                     "ORDER BY id", (list(new_page_ids),)).fetchall()
            ids = _insert(ours, "page_ground_truth_entry", ["page_ground_truth_id", *columns],
                          ((new_page_ids[h["page_ground_truth_id"]], *(value(c, h[c]) for c in columns))
                           for h in history))
            counts["copied.page_ground_truth_entry"] = len(ids)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="back up ours, then copy (default: a dry run)")
    parser.add_argument("--charts-file", help="only these charts (one chart name per line)")
    parser.add_argument("--add-users", action="store_true",
                        help="create the client's accounts we lack, inactive, so their saves keep a name")
    parser.add_argument("--backup-dir", help="where the CSV backup goes (default utilities/output/gt_backup_<stamp>)")
    parser.add_argument("--list-backups", action="store_true", help="the backup schemas in our database")
    parser.add_argument("--restore", metavar="SCHEMA", help="put a backup back (replaces all six tables)")
    args = parser.parse_args()

    our_url = os.environ.get("DATABASE_URL", "").strip()
    if not our_url:
        print("set DATABASE_URL (ours; core-pipeline/.env is read too)", file=sys.stderr)
        return 2
    ours = _connect(our_url)
    print(f"ours   : {_mask(our_url)}")

    if args.list_backups:
        print("\n".join(list_backups(ours)) or "no backups")
        return 0
    if args.restore:
        counts = restore(ours, args.restore)
        print(f"restored from {args.restore}: " + ", ".join(f"{t} {n}" for t, n in counts.items()))
        return 0

    client_url = os.environ.get("CLIENT_DATABASE_URL", "").strip()
    if not client_url:
        print("set CLIENT_DATABASE_URL (the client's review database)", file=sys.stderr)
        return 2
    if client_url == our_url:
        print("CLIENT_DATABASE_URL and DATABASE_URL are the same database", file=sys.stderr)
        return 2
    client = _connect(client_url)
    print(f"client : {_mask(client_url)}")

    only = read_chart_list(args.charts_file) if args.charts_file else None
    charts = client_charts(client, only)
    if only is not None and len(charts) < len(only):
        print(f"{len(only) - len(charts)} listed charts have no ground truth in the client database")
    if not charts:
        print("nothing to copy")
        return 0
    print(f"charts : {len(charts)} with client ground truth\n")
    print(f"{'table':<26}{'ours now':>10}{'client':>10}")
    for table, n_ours, n_client in summary(client, ours, charts):
        print(f"{table:<26}{n_ours:>10}{n_client:>10}")

    if not args.apply:
        print("\ndry run: nothing changed. --apply backs ours up and replaces these charts' rows with the client's")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.backup_dir) if args.backup_dir else REPO / "utilities" / "output" / f"gt_backup_{stamp}"
    schema = backup(ours, stamp, out_dir)
    print(f"\nbackup : schema {schema} + CSVs in {out_dir}")
    counts = copy_ground_truth(client, ours, charts, add_users=args.add_users)
    for key, n in counts.items():
        print(f"  {key:<40}{n}")
    if counts.get("client_users_not_here") and not args.add_users:
        print("  (their saves have no user here; --add-users would create those accounts, inactive)")
    print(f"done. Undo: python utilities/pull_client_ground_truth.py --restore {schema}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
