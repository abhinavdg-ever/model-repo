#!/usr/bin/env python3
"""Load only the Sequence column of a ground-truth sheet into page_ground_truth.page_sequence.

The full loader (``python cli.py ground-truth --local FILE``) writes every ground-truth
column the sheet has. This one writes the sequence alone, even from a full sheet, so the
other labels (and review-ui edits to them) stay as they are. The sheet is the same as
the full load's: Chart_Name, Id and the numeric ``Sequence`` column (a ``Page Sequence``
column is not read). A page not yet in page_ground_truth is added with just its
sequence. An empty cell clears it.

Each page written also gets a ``bulk_upload`` entry: the page's previous bulk labels with
the new sequence, so review-ui's "bulk upload" view keeps the other columns.

Examples::

    cd core-pipeline && source .venv/bin/activate

    # What would be written, nothing written
    python ../utilities/load_page_sequence.py /path/to/ground_truth.csv --dry-run

    # Write it (a file, or a folder of .csv / .xlsx)
    python ../utilities/load_page_sequence.py /path/to/ground_truth.csv

Requires the core-pipeline virtualenv and ``DATABASE_URL`` (env or ``core-pipeline/.env``).
"""
from __future__ import annotations

import argparse
import json
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

from jobs.ground_truth_load import iter_files, read_table, upsert_rows  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("path", help="CSV/XLSX file, or a folder of them")
    parser.add_argument("--dry-run", action="store_true", help="print what would be written")
    args = parser.parse_args(argv)

    root = Path(args.path)
    if not root.exists():
        print(f"not found: {root}", file=sys.stderr)
        return 1

    rows = []
    for path in iter_files(root):
        parsed = [row for row in read_table(path) if not row.get("_skip")]
        if parsed and "page_sequence" not in parsed[0]["_columns"]:
            print(f"{path.name}: no Sequence column, skipped", file=sys.stderr)
            continue
        rows.extend(parsed)
    if not rows:
        print("no rows with Chart_Name, Id and Sequence")
        return 1

    charts = sorted({row["chart_name"] for row in rows})
    filled = sum(1 for row in rows if row.get("page_sequence"))
    print(f"{len(rows)} pages across {len(charts)} charts, {filled} with a sequence")
    if args.dry_run:
        for row in rows[:20]:
            print(f"  {row['chart_name']}  page {row['page_number']}: {row.get('page_sequence') or '(clear)'}")
        return 0

    from db import connect

    with connect() as conn:
        upsert_rows(conn, rows, columns=("page_sequence",))
    print(json.dumps({"pages_written": len(rows), "charts": charts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
