#!/usr/bin/env python3
"""Load chart clusters into chart_list.chart_cluster (2026-11-15, user). review-ui's History and
Accuracy View offer them as the Cluster filter.

The file is chart-clustering's ``chart_clusters.csv`` (columns chart_name, cluster) or any CSV
with a chart column (chart_name / chart / chart_id) and a cluster column (cluster /
chart_cluster). A blank cluster clears the chart's value. Loading does not change the chart's
Last Run At (chart_list.updated_at): the updated_at trigger is paused inside one transaction,
which needs the table owner's rights. Nothing is written when the role may not.

Examples::

    cd core-pipeline && source .venv/bin/activate          (Windows: .venv\\Scripts\\activate.bat)
    python ../utilities/load_chart_clusters.py ../chart-clustering/out/run1/chart_clusters.csv --dry-run
    python ../utilities/load_chart_clusters.py ../chart-clustering/out/run1/chart_clusters.csv
    python ../utilities/load_chart_clusters.py clusters.csv --clear-others   # also clear every chart not in the file

Needs DATABASE_URL (env or core-pipeline/.env). The column is added when missing
(schema/migrations/2026-11-15.sql).
"""
from __future__ import annotations

import argparse
import csv
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

ADD_COLUMN = "ALTER TABLE chart_list ADD COLUMN IF NOT EXISTS chart_cluster VARCHAR(100)"
TRIGGER = "trg_chart_list_updated_at"
CHART_COLUMNS = ("chart_name", "chart", "chart_id", "chartname")
CLUSTER_COLUMNS = ("cluster", "chart_cluster")
MAX_CHARS = 100


def read_file(path: Path) -> dict[str, str]:
    """chart name -> cluster ('' clears). Later rows win."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        heads = {h.strip().lower(): h for h in reader.fieldnames or []}
        chart_col = next((heads[c] for c in CHART_COLUMNS if c in heads), None)
        cluster_col = next((heads[c] for c in CLUSTER_COLUMNS if c in heads), None)
        if not chart_col or not cluster_col:
            sys.exit(f"{path.name}: needs a chart column ({' / '.join(CHART_COLUMNS)}) and a cluster column "
                     f"({' / '.join(CLUSTER_COLUMNS)}); found {', '.join(reader.fieldnames or [])}")
        out: dict[str, str] = {}
        for row in reader:
            name = str(row.get(chart_col) or "").strip()
            if name:
                out[name] = " ".join(str(row.get(cluster_col) or "").split())[:MAX_CHARS]
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="CSV: chart_name, cluster")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    ap.add_argument("--clear-others", action="store_true", help="clear chart_cluster on charts not in the file")
    args = ap.parse_args(argv)

    wanted = read_file(Path(args.file))
    if not wanted:
        sys.exit("no rows in the file")
    url = os.environ.get("DATABASE_URL") or ""
    if not url:
        sys.exit("DATABASE_URL is not set (core-pipeline/.env)")
    import psycopg

    with psycopg.connect(url) as conn:
        if not args.dry_run:
            conn.execute(ADD_COLUMN)
            conn.commit()
        has_col = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_name = 'chart_list' AND column_name = 'chart_cluster'"
        ).fetchone()
        current = {
            str(name): (cluster or "")
            for name, cluster in conn.execute(
                "SELECT chart_name, " + ("chart_cluster" if has_col else "NULL") + " FROM chart_list").fetchall()
        }
        missing = sorted(set(wanted) - set(current))
        updates = [(name, current[name], value) for name, value in sorted(wanted.items())
                   if name in current and current[name] != value]
        if args.clear_others:
            updates += [(name, value, "") for name, value in sorted(current.items()) if name not in wanted and value]

        if updates and not args.dry_run:
            try:
                with conn.transaction():
                    conn.execute(f"ALTER TABLE chart_list DISABLE TRIGGER {TRIGGER}")
                    for name, _, value in updates:
                        conn.execute("UPDATE chart_list SET chart_cluster = %s WHERE chart_name = %s",
                                     (value or None, name))
                    conn.execute(f"ALTER TABLE chart_list ENABLE TRIGGER {TRIGGER}")
            except psycopg.errors.InsufficientPrivilege:
                sys.exit("not allowed to pause chart_list's updated_at trigger: run as the table owner "
                         "(nothing was changed)")
    counts: dict[str, int] = {}
    for _, _, value in updates:
        counts[value or "(cleared)"] = counts.get(value or "(cleared)", 0) + 1
    for value, n in sorted(counts.items()):
        print(f"{value}\t{n} chart(s)")
    print(f"{len(updates)} chart(s) {'would change' if args.dry_run else 'changed'}, "
          f"{len(wanted) - len(missing) - sum(1 for u in updates if u[0] in wanted)} already as in the file, "
          f"{len(missing)} not in chart_list{' (dry run: nothing written)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
