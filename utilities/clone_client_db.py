#!/usr/bin/env python3
"""Fill the client's review database from ours: the charts last run on or after a cutoff,
or the charts named in a list, our users (so the same sign-ins work), and no ground truth
(docs/CLIENT_INSTANCE.md).

    python utilities/clone_client_db.py --dry-run            # what would be copied
    python utilities/clone_client_db.py                      # copy into an empty target
    python utilities/clone_client_db.py --replace            # refresh: swap the chart data
    python utilities/clone_client_db.py --charts-file charts.txt          # only these charts
    python utilities/clone_client_db.py --charts-file charts.txt --add    # add them to the target
    python utilities/clone_client_db.py --charts-file charts.txt --update # re-copy them (re-runs)
    python utilities/clone_client_db.py --charts-file ids.txt --by-id --update  # the list holds chart_ids
    python utilities/clone_client_db.py --since "2026-11-01 00:00" --add  # add newer runs

Which charts: ``--since`` (last run at or after it; default 2026-10-26 00:00 when no list
is given) and / or ``--charts-file`` (a text file, one chart name per line; commas and
spaces also separate, a ``#`` starts a comment; ``--by-id``: chart_list ids instead of
names). Both given: a chart must be in the list AND run since the date. Entries not in our
database are printed.

How they land: an empty target is filled; ``--replace`` deletes all the target's chart data
first; ``--add`` keeps what the target has and copies only the chosen charts it does not
have yet (one already there is skipped); ``--update`` deletes just the chosen charts from
the target (matched by chart name) and copies them again, so a batch of re-run charts is
brought up to date and every other chart is left alone. Ground truth is never touched.

Needs SOURCE_DATABASE_URL (ours) and TARGET_DATABASE_URL (the client's, created with
schema/v1.sql). "Last run" is chart_list.updated_at, which a run writes last.

Copied, for the chosen charts only: chart_list, page_list, every pipeline result table,
pipeline_jobs and the charts' manifest_member_list rows; ids are kept, so a page is the
same page_list.id in both databases. Users are merged by username (password, name,
role, active), so our sign-ins work and accounts made on the client side stay.

Ground truth: not copied unless ``--with-ground-truth`` (2026-10-27, user). With it, our
page_ground_truth rows of the chosen charts and their page_ground_truth_entry history are
copied for every page the target has no ground truth on yet; a page the client already
labelled keeps theirs. Who saved each one (user_id) is matched by username. The chart's
member decision (chart_ground_truth) comes too, for a chart the target has none for.

``--replace-ground-truth`` (with it): the target's ground truth of the chosen charts is deleted
first and ours copied in its place -- the client's own labels on those charts are lost.

Never copied: encounter_review, user_login, chart_deletion; pipeline_stage is seeded by
v1.sql. The client's own ground truth is keyed by chart name and page number, so
--replace leaves it in place.
"""
from __future__ import annotations

import argparse
import os
import time
import re
import sys
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

# Parents first: chart_list and manifest_member_list, then page_list, then the rest.
CHART_TABLES = (
    "page_list",
    "page_stage_status",
    "ocr_results",
    "ocr_quality_results",
    "blank_junk_classification",
    "dos_extraction_results",
    "page_classification",
    "encounter_type_results",
    "page_sequencing_results",
    "provider_signature_results",
    "additional_page_details",
    "page_continuity_results",
    "imaging_final",
    "member_extraction_results",
    "member_verification_summary",
    "pipeline_jobs",
)
USER_FIELDS = ("name", "email", "role", "password_hash", "is_active")
DEFAULT_SINCE = "2026-10-26 00:00"


def _connect(url: str):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://"), row_factory=dict_row)


def _columns(conn, table: str) -> list[str]:
    """The table's ordinary columns (a generated column is computed, not copied)."""
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s AND is_generated = 'NEVER' "
        "ORDER BY ordinal_position",
        (table,),
    ).fetchall()
    return [row["column_name"] for row in rows]


def _copy(src, dst, table: str, where: str, params: tuple) -> int:
    """COPY the matching rows across, on the columns both databases have."""
    from psycopg import sql

    target = set(_columns(dst, table))
    columns = [c for c in _columns(src, table) if c in target]
    names = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
    select = sql.SQL("SELECT {} FROM {} WHERE " + where).format(names, sql.Identifier(table))
    count = src.execute(
        sql.SQL("SELECT count(*) AS n FROM {} WHERE " + where).format(sql.Identifier(table)), params
    ).fetchone()["n"]
    if not count:
        return 0
    with src.cursor().copy(sql.SQL("COPY ({}) TO STDOUT").format(select), params) as out, \
            dst.cursor().copy(sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(table), names)) as into:
        for block in out:
            into.write(block)
    return count


def _reset_sequence(dst, table: str) -> None:
    dst.execute(
        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
        f"GREATEST((SELECT COALESCE(max(id), 0) FROM {table}), 1))"
    )


def read_chart_list(path: str) -> list[str]:
    """Chart names from a text file: one per line (commas and spaces also separate), blank
    lines skipped, ``#`` starts a comment; order kept, repeats dropped."""
    names: list[str] = []
    with open(path, encoding="utf-8-sig") as handle:
        for line in handle:
            for name in re.split(r"[,\s]+", line.split("#", 1)[0]):
                if name and name not in names:
                    names.append(name)
    return names


def chart_filter(cutoff: Optional[datetime], names: Optional[list], by_id: bool = False) -> tuple[str, tuple]:
    """The WHERE clause on chart_list choosing the charts, and its parameters. ``names`` are
    chart names, or chart_list ids with ``by_id``."""
    clauses, params = [], []
    if cutoff is not None:
        clauses.append("updated_at >= %s")
        params.append(cutoff)
    if names is not None:
        clauses.append(f"{'id' if by_id else 'chart_name'} = ANY(%s)")
        params.append(names)
    return (" AND ".join(clauses) or "TRUE"), tuple(params)


def read_chart_ids(path: str) -> list[int]:
    """chart_list ids from a list file (``read_chart_list``); a non-number is an error."""
    entries = read_chart_list(path)
    bad = [entry for entry in entries if not entry.isdigit()]
    if bad:
        raise SystemExit(f"--by-id: not chart ids: {', '.join(bad[:10])}")
    return [int(entry) for entry in entries]


def _describe(cutoff: Optional[datetime], names: Optional[list[str]]) -> str:
    parts = [f"run since {cutoff.isoformat()}"] if cutoff is not None else []
    if names is not None:
        parts.append(f"in the list of {len(names)}")
    return " and ".join(parts)


def _report_missing(src, names: Optional[list], by_id: bool = False) -> None:
    if not names:
        return
    column = "id" if by_id else "chart_name"
    found = {row[column] for row in src.execute(
        f"SELECT {column} FROM chart_list WHERE {column} = ANY(%s)", (names,)).fetchall()}
    missing = [str(name) for name in names if name not in found]
    if missing:
        print(f"not in our database ({len(missing)}): {', '.join(missing)}")


def _dry_run(src, cutoff: Optional[datetime], names: Optional[list], by_id: bool = False) -> int:
    """What a copy would take from the source; reads only."""
    where, params = chart_filter(cutoff, names, by_id)
    row = src.execute(
        "SELECT count(*) AS charts, min(updated_at) AS first_run, max(updated_at) AS last_run, "
        f"(SELECT count(*) FROM page_list p WHERE p.chart_id IN (SELECT id FROM chart_list WHERE {where})) AS pages, "
        "(SELECT count(*) FROM users) AS users "
        f"FROM chart_list WHERE {where}",
        params + params,
    ).fetchone()
    statuses = src.execute(
        f"SELECT status, count(*) AS n FROM chart_list WHERE {where} GROUP BY status ORDER BY status", params
    ).fetchall()
    total = src.execute("SELECT count(*) AS n FROM chart_list").fetchone()["n"]
    print(f"charts {_describe(cutoff, names)}: {row['charts']} of {total} charts, {row['pages']} pages, "
          f"{row['users']} users; by status {{{', '.join(f'{s['status']}: {s['n']}' for s in statuses)}}}")
    _report_missing(src, names, by_id)
    print(f"their last runs: {row['first_run']} .. {row['last_run']}; dry run, nothing written")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", default=None,
                        help="charts last run at or after this local time "
                             f"(default {DEFAULT_SINCE} when no --charts-file is given)")
    parser.add_argument("--tz", default="Asia/Kolkata", help="time zone of --since (default Asia/Kolkata)")
    parser.add_argument("--charts-file", default=None,
                        help="a text file of chart names, one per line: only these charts")
    parser.add_argument("--by-id", action="store_true",
                        help="--charts-file holds chart_list ids (chart_id) instead of chart names")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--replace", action="store_true",
                      help="refresh: delete the target's chart data first (ground truth and users stay)")
    mode.add_argument("--add", action="store_true",
                      help="keep the target's charts; copy only the chosen charts it does not have yet")
    mode.add_argument("--update", action="store_true",
                      help="delete just the chosen charts from the target and copy them again (re-runs)")
    parser.add_argument("--with-ground-truth", action="store_true",
                        help="also copy our ground truth of the chosen charts, on pages the target has none yet")
    parser.add_argument("--replace-ground-truth", action="store_true",
                        help="with --with-ground-truth: first DELETE the target's ground truth of the chosen "
                             "charts (page labels, their history, the member decision), then copy ours")
    parser.add_argument("--dry-run", action="store_true", help="count what would be copied; change nothing")
    args = parser.parse_args()

    source_url, target_url = os.environ.get("SOURCE_DATABASE_URL"), os.environ.get("TARGET_DATABASE_URL")
    if not source_url or not (target_url or args.dry_run):
        print("set SOURCE_DATABASE_URL and TARGET_DATABASE_URL (a dry run needs only the source)", file=sys.stderr)
        return 2
    if source_url == target_url:
        print("the source and the target are the same database", file=sys.stderr)
        return 2
    if args.replace_ground_truth and not args.with_ground_truth:
        print("--replace-ground-truth needs --with-ground-truth", file=sys.stderr)
        return 2
    if args.by_id and not args.charts_file:
        print("--by-id needs --charts-file", file=sys.stderr)
        return 2
    names_wanted = (
        (read_chart_ids(args.charts_file) if args.by_id else read_chart_list(args.charts_file))
        if args.charts_file else None
    )
    if names_wanted is not None and not names_wanted:
        print(f"{args.charts_file} names no charts", file=sys.stderr)
        return 2
    since = args.since or (None if names_wanted is not None else DEFAULT_SINCE)
    cutoff = datetime.fromisoformat(since).replace(tzinfo=ZoneInfo(args.tz)) if since else None
    where, params = chart_filter(cutoff, names_wanted, args.by_id)

    if args.dry_run:
        with _connect(source_url) as src:
            code = _dry_run(src, cutoff, names_wanted, args.by_id)
            if args.with_ground_truth:
                row = src.execute(
                    "SELECT count(*) AS n FROM page_ground_truth WHERE chart_name IN "
                    f"(SELECT chart_name FROM chart_list WHERE {where})", params,
                ).fetchone()
                print(f"ground truth: {row['n']} of our pages on these charts (copied where the target has none)")
            return code

    with _connect(source_url) as src, _connect(target_url) as dst:
        charts = src.execute(
            f"SELECT id, chart_name, status FROM chart_list WHERE {where} ORDER BY id", params
        ).fetchall()
        _report_missing(src, names_wanted, args.by_id)
        existing = dst.execute("SELECT count(*) AS n FROM chart_list").fetchone()["n"]
        if existing and not (args.replace or args.add or args.update):
            print(f"the target already has {existing} charts: use --add, --update or --replace",
                  file=sys.stderr)
            return 1
        # The target's charts by name (a chart re-created in ours may have a new id).
        target_ids = {row["chart_name"]: row["id"] for row in dst.execute(
            "SELECT id, chart_name FROM chart_list WHERE chart_name = ANY(%s)",
            ([row["chart_name"] for row in charts],)).fetchall()}
        chosen = [row["chart_name"] for row in charts]
        if args.add:
            skipped = [row["chart_name"] for row in charts if row["chart_name"] in target_ids]
            charts = [row for row in charts if row["chart_name"] not in target_ids]
            if skipped:
                print(f"already in the target, skipped ({len(skipped)}): {', '.join(skipped)}")
        ids = [row["id"] for row in charts]
        names = [row["chart_name"] for row in charts]
        statuses: dict[str, int] = {}
        for row in charts:
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        print(f"charts {_describe(cutoff, names_wanted)}: copying {len(ids)} {statuses}")
        if not dst.execute("SELECT count(*) AS n FROM pipeline_stage").fetchone()["n"]:
            print("the target has no schema / pipeline_stage: run schema/v1.sql on it first", file=sys.stderr)
            return 1
        if args.replace:
            for table in reversed(CHART_TABLES):
                dst.execute(f"DELETE FROM {table}")
            dst.execute("DELETE FROM chart_list")
            dst.execute("DELETE FROM manifest_member_list")
        if args.update and target_ids:
            # Just these charts: their old rows go, the latest are copied below.
            stale = list(target_ids.values())
            for table in reversed(CHART_TABLES):
                dst.execute(f"DELETE FROM {table} WHERE chart_id = ANY(%s)", (stale,))
            dst.execute("DELETE FROM chart_list WHERE id = ANY(%s)", (stale,))
            print(f"updating {len(stale)} charts already in the target")

        # Manifest rows the target already has (--add) stay as they are.
        kept = [row["id"] for row in dst.execute("SELECT id FROM manifest_member_list").fetchall()]
        manifest = _copy(
            src, dst, "manifest_member_list",
            "(record_id = ANY(%s) OR id IN (SELECT matched_member_list_id FROM member_verification_summary "
            "WHERE chart_id = ANY(%s) UNION SELECT matched_member_list_id FROM member_extraction_results "
            "WHERE chart_id = ANY(%s))) AND NOT id = ANY(%s)",
            (names, ids, ids, kept),
        )
        counts = {"chart_list": _copy(src, dst, "chart_list", "id = ANY(%s)", (ids,)),
                  "manifest_member_list": manifest}
        for table in CHART_TABLES:
            started = time.monotonic()
            counts[table] = _copy(src, dst, table, "chart_id = ANY(%s)", (ids,))
            # Where the time goes: OCR text is most of it.
            print(f"  copied {table:30} {counts[table]:>8} rows in {time.monotonic() - started:6.1f} s", flush=True)

        # Users: the same sign-ins, merged by username; accounts made on the client side stay.
        users = src.execute(f"SELECT username, {', '.join(USER_FIELDS)} FROM users").fetchall()
        for user in users:
            dst.execute(
                f"INSERT INTO users (username, {', '.join(USER_FIELDS)}) "
                f"VALUES (%s, {', '.join(['%s'] * len(USER_FIELDS))}) "
                f"ON CONFLICT (username) DO UPDATE SET "
                + ", ".join(f"{field} = EXCLUDED.{field}" for field in USER_FIELDS),
                (user["username"], *(user[field] for field in USER_FIELDS)),
            )
        counts["users"] = len(users)

        for table in ("chart_list", "manifest_member_list", "users", *CHART_TABLES):
            _reset_sequence(dst, table)
        if args.with_ground_truth:
            if args.replace_ground_truth:
                counts["ground_truth_deleted"] = _delete_ground_truth(dst, chosen)
            counts["page_ground_truth"], counts["page_ground_truth_entry"] = _copy_ground_truth(src, dst, chosen)
        ground_truth = dst.execute("SELECT count(*) AS n FROM page_ground_truth").fetchone()["n"]

    for table, count in counts.items():
        print(f"  {table:30} {count}")
    if args.with_ground_truth:
        print(f"target ground truth rows: {ground_truth} (ours copied where the page had none)")
    else:
        print(f"target ground truth rows: {ground_truth} (the client's own; none copied)")
    return 0


def _delete_ground_truth(dst, chart_names: list[str]) -> int:
    """The target's ground truth of these charts, gone (2026-10-28, user): page labels (their
    history goes with them, ON DELETE CASCADE) and the member decision. Returns pages deleted."""
    if not chart_names:
        return 0
    deleted = dst.execute(
        "DELETE FROM page_ground_truth WHERE chart_name = ANY(%s)", (chart_names,)
    ).rowcount
    if "chart_ground_truth" in _tables(dst):
        dst.execute("DELETE FROM chart_ground_truth WHERE chart_name = ANY(%s)", (chart_names,))
    print(f"ground truth: deleted the target's labels on {deleted} pages of {len(chart_names)} charts")
    return deleted


def map_user_id(user_id, source_names: dict, target_ids: dict):
    """Our user id as the target's id of the same username (None when it has none)."""
    return target_ids.get(source_names.get(user_id)) if user_id is not None else None


def _copy_ground_truth(src, dst, chart_names: list[str]) -> tuple[int, int]:
    """Our ground truth of these charts into the target, page by page: a page the target
    already has a row for is left as it is. Ids are the target's own; each copied row's
    history (page_ground_truth_entry) follows it. Returns (rows copied, entries copied)."""
    from psycopg.types.json import Jsonb

    if not chart_names:
        return 0, 0
    source_names = {r["id"]: r["username"] for r in src.execute("SELECT id, username FROM users").fetchall()}
    target_ids = {r["username"]: r["id"] for r in dst.execute("SELECT id, username FROM users").fetchall()}
    target_columns = set(_columns(dst, "page_ground_truth"))
    columns = [c for c in _columns(src, "page_ground_truth") if c in target_columns and c != "id"]
    entry_target = set(_columns(dst, "page_ground_truth_entry"))
    entry_columns = [c for c in _columns(src, "page_ground_truth_entry")
                     if c in entry_target and c not in ("id", "page_ground_truth_id")]

    def value(column, raw):
        if column == "user_id":
            return map_user_id(raw, source_names, target_ids)
        return Jsonb(raw) if isinstance(raw, (dict, list)) else raw

    rows = src.execute(
        "SELECT * FROM page_ground_truth WHERE chart_name = ANY(%s) ORDER BY chart_name, page_number",
        (chart_names,),
    ).fetchall()
    insert = (f"INSERT INTO page_ground_truth ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) "
              "ON CONFLICT (chart_name, page_number) DO NOTHING RETURNING id")
    insert_entry = (f"INSERT INTO page_ground_truth_entry (page_ground_truth_id, {', '.join(entry_columns)}) "
                    f"VALUES (%s, {', '.join(['%s'] * len(entry_columns))})")
    # In batches, not a round trip per page (2026-10-28): the pages the target already has
    # are left out up front, the rest go in one executemany, their history in another.
    have = {
        (r["chart_name"], r["page_number"]) for r in dst.execute(
            "SELECT chart_name, page_number FROM page_ground_truth WHERE chart_name = ANY(%s)", (chart_names,)
        ).fetchall()
    }
    fresh = [row for row in rows if (row["chart_name"], row["page_number"]) not in have]
    new_ids: dict[int, int] = {}
    if fresh:
        with dst.cursor() as cur:
            cur.executemany(insert, [tuple(value(c, row[c]) for c in columns) for row in fresh], returning=True)
            for row in fresh:
                made = cur.fetchone()
                if made is not None:
                    new_ids[row["id"]] = made["id"]
                if not cur.nextset():
                    break
    copied = len(new_ids)
    entries = 0
    if new_ids:
        history = src.execute(
            "SELECT * FROM page_ground_truth_entry WHERE page_ground_truth_id = ANY(%s) ORDER BY id",
            (list(new_ids),),
        ).fetchall()
        if history:
            with dst.cursor() as cur:
                cur.executemany(insert_entry, [
                    (new_ids[entry["page_ground_truth_id"]], *(value(c, entry[c]) for c in entry_columns))
                    for entry in history
                ])
            entries = len(history)
    skipped = len(rows) - copied
    if skipped:
        print(f"ground truth: {skipped} pages kept the target's own (not overwritten)")
    # The reviewer's member decision per chart (2026-10-28), when both databases have it.
    if "chart_ground_truth" in _tables(src) and "chart_ground_truth" in _tables(dst):
        for row in src.execute(
            "SELECT chart_name, member_decision, member_decision_reason, user_id "
            "FROM chart_ground_truth WHERE chart_name = ANY(%s)", (chart_names,),
        ).fetchall():
            dst.execute(
                "INSERT INTO chart_ground_truth (chart_name, member_decision, member_decision_reason, user_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (chart_name) DO NOTHING",
                (row["chart_name"], row["member_decision"], row["member_decision_reason"],
                 map_user_id(row["user_id"], source_names, target_ids)),
            )
    return copied, entries


def _tables(conn) -> set[str]:
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    ).fetchall()
    return {row["table_name"] for row in rows}


if __name__ == "__main__":
    sys.exit(main())
