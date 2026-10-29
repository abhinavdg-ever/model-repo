#!/usr/bin/env python3
"""Put back the client sheet's Page Type (Accept / Unaccept) that review-ui saves overwrote.

The client sheet's Page Type column holds Accept / Unaccept; its page type is in Encounter
Type. Until 2026-10-29 a Page Details save wrote the page type into both page_type and
encounter_type, so a page saved by hand lost the sheet's value. Each page's latest
bulk_upload entry in page_ground_truth_entry still has it.

A page is restored when its latest load set page_type, a manual save came after that load,
and page_type now differs from the load. Only page_type is written; encounter_type (the
page type Page Details shows and accuracy scores), the history and the Review Log are left
as they are. Safe to run again: a restored page no longer differs.

Examples::

    cd core-pipeline && source .venv/bin/activate
    python ../utilities/restore_sheet_page_type.py                     # preview only
    python ../utilities/restore_sheet_page_type.py --apply             # write
    python ../utilities/restore_sheet_page_type.py --database-url "$CLIENT_DATABASE_URL"

    # No Python on the database host: the same as SQL for psql
    python ../utilities/restore_sheet_page_type.py --sql > restore.sql
    psql "$DATABASE_URL" -f restore.sql

Needs DATABASE_URL (env or core-pipeline/.env) unless --database-url is given.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"

try:
    from dotenv import load_dotenv

    load_dotenv(CORE / ".env")
except ImportError:
    pass

# Pages whose page_type a manual save overwrote: (id, chart, page, now, sheet value).
CANDIDATES = """
WITH bulk AS (
    SELECT DISTINCT ON (e.page_ground_truth_id)
           e.page_ground_truth_id, e.id AS bulk_id, e.labels ->> 'page_type' AS sheet_page_type
      FROM page_ground_truth_entry e
     WHERE e.entry_mode = 'bulk_upload' AND e.labels ? 'page_type'
     ORDER BY e.page_ground_truth_id, e.id DESC
)
SELECT g.id, g.chart_name, g.page_number, g.page_type, b.sheet_page_type
  FROM page_ground_truth g
  JOIN bulk b ON b.page_ground_truth_id = g.id
 WHERE b.sheet_page_type IS NOT NULL
   AND g.page_type IS DISTINCT FROM b.sheet_page_type
   AND EXISTS (SELECT 1 FROM page_ground_truth_entry m
                WHERE m.page_ground_truth_id = g.id AND m.entry_mode = 'manual_entry'
                  AND m.id > b.bulk_id)
 ORDER BY g.chart_name, g.page_number
"""

RESTORE = f"""
UPDATE page_ground_truth g
   SET page_type = c.sheet_page_type
  FROM ({CANDIDATES}) c
 WHERE g.id = c.id
"""


def _url(value: str) -> str:
    # core-pipeline's .env names the SQLAlchemy driver; psycopg takes the plain URL.
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", help="Default: DATABASE_URL")
    parser.add_argument("--schema", default=os.environ.get("DB_SCHEMA", "public"))
    parser.add_argument("--apply", action="store_true", help="Write (default: preview only)")
    parser.add_argument("--sql", action="store_true", help="Print SQL for psql instead")
    args = parser.parse_args(argv)

    if args.sql:
        print("-- Restore the client sheet's Page Type. Preview, then the update, in one transaction.")
        print(f"SET search_path TO {args.schema};")
        print(CANDIDATES.strip() + ";")
        print("BEGIN;")
        print(RESTORE.strip() + ";")
        print("COMMIT;")
        return 0

    url = args.database_url or os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set (env, core-pipeline/.env or --database-url)", file=sys.stderr)
        return 2
    import psycopg
    from psycopg import sql

    with psycopg.connect(_url(url)) as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(args.schema)))
            cur.execute(CANDIDATES)
            rows = cur.fetchall()
            for _id, chart, page, now, sheet in rows:
                print(f"{chart}  page {page}:  {now or '—'}  ->  {sheet}")
            charts = len({row[1] for row in rows})
            print(f"{len(rows)} page(s) in {charts} chart(s) to restore.")
            if not args.apply:
                print("Preview only: nothing written. Re-run with --apply to restore.")
                return 0
            cur.execute(RESTORE)
            print(f"Restored {cur.rowcount} page(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
