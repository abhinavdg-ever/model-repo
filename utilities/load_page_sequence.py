#!/usr/bin/env python3
"""Load only the Sequence column of a ground-truth sheet into page_ground_truth.page_sequence.

The full loader (``python cli.py ground-truth --local FILE``) rewrites every ground-truth
column of each page in the sheet. This one writes the sequence alone, so labels edited in
review-ui stay as they are. The sheet is the same as the full load's: Chart_Name, Id and a
sequence column headed ``Sequence``, ``Page Sequence`` or ``Actual Sequence``. A page not
yet in page_ground_truth is added with just its sequence. An empty cell clears it.

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

from jobs.ground_truth_load import (  # noqa: E402
    _HEADER,
    _read_csv,
    _read_xlsx,
    iter_files,
    norm_header,
    read_table,
)


def has_sequence_column(path: Path) -> bool:
    headers, _ = _read_csv(path) if path.suffix.casefold() == ".csv" else _read_xlsx(path)
    return any(_HEADER.get(norm_header(h)) == "page_sequence" for h in headers)

_UPSERT = """
WITH g AS (
INSERT INTO page_ground_truth (chart_name, page_number, source_page_id, page_sequence, source_path)
VALUES (%(chart_name)s, %(page_number)s, %(source_page_id)s, %(page_sequence)s, %(source_path)s)
ON CONFLICT (chart_name, page_number) DO UPDATE SET
    source_page_id = COALESCE(EXCLUDED.source_page_id, page_ground_truth.source_page_id),
    page_sequence = EXCLUDED.page_sequence,
    updated_at = now()
RETURNING id
)
INSERT INTO page_ground_truth_entry (page_ground_truth_id, entry_mode, labels, source_path)
SELECT g.id, 'bulk_upload',
       COALESCE((SELECT e.labels FROM page_ground_truth_entry e
                  WHERE e.page_ground_truth_id = g.id AND e.entry_mode = 'bulk_upload'
                  ORDER BY e.id DESC LIMIT 1), '{}'::jsonb)
       || jsonb_build_object('page_sequence', %(page_sequence)s::text),
       %(source_path)s
  FROM g
"""


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
        if not has_sequence_column(path):
            print(f"{path.name}: no Sequence column, skipped", file=sys.stderr)
            continue
        rows.extend(row for row in read_table(path) if not row.get("_skip"))
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
        with conn.cursor() as cur:
            cur.executemany(
                _UPSERT,
                [
                    {
                        "chart_name": row["chart_name"],
                        "page_number": row["page_number"],
                        "source_page_id": row.get("source_page_id"),
                        "page_sequence": row.get("page_sequence"),
                        "source_path": row.get("source_path"),
                    }
                    for row in rows
                ],
            )
    print(json.dumps({"pages_written": len(rows), "charts": charts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
