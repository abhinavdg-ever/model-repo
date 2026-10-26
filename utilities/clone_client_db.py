#!/usr/bin/env python3
"""Fill the client's review database from ours: the charts last run on or after a cutoff,
our users (so the same sign-ins work), and no ground truth (docs/CLIENT_INSTANCE.md).

    python utilities/clone_client_db.py --dry-run            # what would be copied
    python utilities/clone_client_db.py                      # copy into an empty target
    python utilities/clone_client_db.py --replace            # refresh: swap the chart data

Needs SOURCE_DATABASE_URL (ours) and TARGET_DATABASE_URL (the client's, created with
schema/v1.sql). "Last run" is chart_list.updated_at, which a run writes last.

Copied, for the chosen charts only: chart_list, page_list, every pipeline result table,
pipeline_jobs and the charts' manifest_member_list rows; ids are kept, so a page is the
same page_list.id in both databases. Users are merged by username (password, name,
role, active), so our sign-ins work and accounts made on the client side stay.

Never copied: page_ground_truth and page_ground_truth_entry (manual and CSV-uploaded
ground truth), encounter_review, user_login, chart_deletion; pipeline_stage is seeded by
v1.sql. The client's own ground truth is keyed by chart name and page number, so
--replace leaves it in place.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
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


def _dry_run(src, cutoff: datetime) -> int:
    """What a copy would take from the source; reads only."""
    row = src.execute(
        "SELECT count(*) AS charts, min(updated_at) AS first_run, max(updated_at) AS last_run, "
        "(SELECT count(*) FROM page_list p JOIN chart_list c ON c.id = p.chart_id WHERE c.updated_at >= %s) AS pages, "
        "(SELECT count(*) FROM users) AS users "
        "FROM chart_list WHERE updated_at >= %s",
        (cutoff, cutoff),
    ).fetchone()
    statuses = src.execute(
        "SELECT status, count(*) AS n FROM chart_list WHERE updated_at >= %s GROUP BY status ORDER BY status", (cutoff,)
    ).fetchall()
    total = src.execute("SELECT count(*) AS n FROM chart_list").fetchone()["n"]
    print(f"cutoff {cutoff.isoformat()}: {row['charts']} of {total} charts, {row['pages']} pages, "
          f"{row['users']} users; by status {{{', '.join(f'{s['status']}: {s['n']}' for s in statuses)}}}")
    print(f"their last runs: {row['first_run']} .. {row['last_run']}; dry run, nothing written")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", default="2026-10-26 00:00",
                        help="charts last run at or after this local time (default 2026-10-26 00:00)")
    parser.add_argument("--tz", default="Asia/Kolkata", help="time zone of --since (default Asia/Kolkata)")
    parser.add_argument("--replace", action="store_true",
                        help="refresh: delete the target's chart data first (ground truth and users stay)")
    parser.add_argument("--dry-run", action="store_true", help="count what would be copied; change nothing")
    args = parser.parse_args()

    source_url, target_url = os.environ.get("SOURCE_DATABASE_URL"), os.environ.get("TARGET_DATABASE_URL")
    if not source_url or not (target_url or args.dry_run):
        print("set SOURCE_DATABASE_URL and TARGET_DATABASE_URL (a dry run needs only the source)", file=sys.stderr)
        return 2
    if source_url == target_url:
        print("the source and the target are the same database", file=sys.stderr)
        return 2
    cutoff = datetime.fromisoformat(args.since).replace(tzinfo=ZoneInfo(args.tz))

    if args.dry_run:
        with _connect(source_url) as src:
            return _dry_run(src, cutoff)

    with _connect(source_url) as src, _connect(target_url) as dst:
        charts = src.execute(
            "SELECT id, chart_name, status FROM chart_list WHERE updated_at >= %s ORDER BY id", (cutoff,)
        ).fetchall()
        ids = [row["id"] for row in charts]
        names = [row["chart_name"] for row in charts]
        statuses: dict[str, int] = {}
        for row in charts:
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        print(f"cutoff {cutoff.isoformat()}: {len(ids)} charts {statuses}")

        existing = dst.execute("SELECT count(*) AS n FROM chart_list").fetchone()["n"]
        if existing and not args.replace:
            print(f"the target already has {existing} charts: use --replace to refresh", file=sys.stderr)
            return 1
        if not dst.execute("SELECT count(*) AS n FROM pipeline_stage").fetchone()["n"]:
            print("the target has no schema / pipeline_stage: run schema/v1.sql on it first", file=sys.stderr)
            return 1
        if args.replace:
            for table in reversed(CHART_TABLES):
                dst.execute(f"DELETE FROM {table}")
            dst.execute("DELETE FROM chart_list")
            dst.execute("DELETE FROM manifest_member_list")

        manifest = _copy(
            src, dst, "manifest_member_list",
            "record_id = ANY(%s) OR id IN (SELECT matched_member_list_id FROM member_verification_summary "
            "WHERE chart_id = ANY(%s) UNION SELECT matched_member_list_id FROM member_extraction_results "
            "WHERE chart_id = ANY(%s))",
            (names, ids, ids),
        )
        counts = {"chart_list": _copy(src, dst, "chart_list", "id = ANY(%s)", (ids,)),
                  "manifest_member_list": manifest}
        for table in CHART_TABLES:
            counts[table] = _copy(src, dst, table, "chart_id = ANY(%s)", (ids,))

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
        ground_truth = dst.execute("SELECT count(*) AS n FROM page_ground_truth").fetchone()["n"]

    for table, count in counts.items():
        print(f"  {table:30} {count}")
    print(f"target ground truth rows: {ground_truth} (the client's own; none copied)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
