#!/usr/bin/env python3
"""Recompute chart_list.status from the stage rows, for charts a bulk run left at 'received'.

Before 2026-10-20 a batch run re-registered every chart as 'received' when it was queued,
so a finished chart dropped off History until its turn came, and a run that stopped early
left it there. Its stage rows (page_stage_status) still say what is done, so the real
status can be worked out again: the same rule the pipeline uses (db.chart_status).
A chart with no pages stays 'received'.

Examples::

    cd core-pipeline && source .venv/bin/activate
    python ../utilities/refresh_chart_status.py --run Test --batch Test --dry-run
    python ../utilities/refresh_chart_status.py --run Test --batch Test
    python ../utilities/refresh_chart_status.py --chart 57163714_50758967
    python ../utilities/refresh_chart_status.py --all-received

Needs DATABASE_URL (env or core-pipeline/.env). Do not run it while those charts are
being processed: their stage rows are being rewritten.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORE = REPO / "core-pipeline"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

try:
    from dotenv import load_dotenv

    load_dotenv(CORE / ".env")
except ImportError:
    pass

from db import connect  # noqa: E402
from db.chart_status import _stage_rows, compute_progress, refresh_chart_status  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", help="run_id, e.g. Test or R2")
    parser.add_argument("--batch", help="batch_id, e.g. Test or B4")
    parser.add_argument("--chart", action="append", default=[], help="a chart name (repeatable)")
    parser.add_argument("--all-received", action="store_true", help="every chart at 'received'")
    parser.add_argument("--status", default="received", help="only charts at this status (default received)")
    parser.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    args = parser.parse_args(argv)
    if not (args.run or args.batch or args.chart or args.all_received):
        parser.error("give --run / --batch, --chart, or --all-received")

    where, params = ["status = %s"], [args.status]
    if args.run:
        where.append("run_id = %s")
        params.append(args.run)
    if args.batch:
        where.append("batch_id = %s")
        params.append(args.batch)
    if args.chart:
        where.append("chart_name = ANY(%s)")
        params.append(args.chart)

    changed = 0
    with connect() as conn:
        charts = conn.execute(
            f"SELECT id, chart_name, status FROM chart_list WHERE {' AND '.join(where)} ORDER BY chart_name",
            params,
        ).fetchall()
        print(f"{len(charts)} chart(s) at '{args.status}'")
        for chart in charts:
            if args.dry_run:
                pages = conn.execute(
                    "SELECT COUNT(*) AS n FROM page_list WHERE chart_id = %s", (chart["id"],)
                ).fetchone()["n"]
                progress = compute_progress(_stage_rows(conn, chart["id"]), pages_total=int(pages or 0))
            else:
                progress = refresh_chart_status(conn, chart["id"])
            new = progress["status"]
            note = f" (stage {progress['current_stage']})" if progress.get("current_stage") else ""
            print(f"  {chart['chart_name']}: {chart['status']} -> {new}{note}")
            changed += new != chart["status"]
    print(f"{changed} would change (dry run)" if args.dry_run else f"{changed} changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
