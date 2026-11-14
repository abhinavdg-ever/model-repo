#!/usr/bin/env python3
"""Ground truth: set one column's value X to value Y in page_ground_truth, on one database.

For example, every blank / NULL / "NA" in page_ground_truth.is_invoice -> "No".
Only page_ground_truth and page_ground_truth_entry are touched. Every page changed also
gets a page_ground_truth_entry row the way a bulk upload writes one (db-utilities
ground_truth.py): entry_mode 'bulk_upload', labels = the page's newest bulk_upload labels
with the new value on top (a page with no entry yet starts from its current labels),
source_path SOURCE_PATH, no user. So review-ui's "bulk upload" view shows the change too.
One transaction: both tables or neither.

DRY_RUN = True (the default) changes nothing: it prints how many rows each value would
move. Never prints page text or patient values: only counts, chart names and page numbers.

    python3 utilities/gt_replace_value.py                 # VM / Mac
    python utilities\\gt_replace_value.py                  # Windows (Command Prompt)

    pip install "psycopg[binary]"    (if neither psycopg nor psycopg2 is installed)
"""
# ============================ PARAMETERS ============================
DATABASE_URL = ""            # "" = DATABASE_URL from the environment, else core-pipeline/.env
COLUMN = "is_invoice"        # the page_ground_truth column to change

# Values to replace. BLANK = NULL, "" and whitespace-only. Text values match after
# trimming, ignoring case ("NA", "na", " N/A " ...) when IGNORE_CASE is True.
FROM_VALUES = ["BLANK", "NA", "N/A"]
TO_VALUE = "No"              # the new value; None = set NULL
IGNORE_CASE = True

CHARTS = []                  # [] = every chart; or ["67993625_56070967", ...]
DRY_RUN = True               # True = only count; False = change the rows (one transaction)
SHOW_PAGES = 20              # how many chart / page numbers to list (0 = none)
SOURCE_PATH = "utilities/gt_replace_value.py"   # the entry's source_path (where the change came from)
# ====================================================================

import os
import re
import sys
from pathlib import Path

BLANK = "BLANK"
TABLE = "page_ground_truth"
# The new entry: the page's newest bulk_upload labels (else, with no bulk entry yet, all its
# current labels, as review-ui seeds one), with the new value on top, so the entry is complete.
_ADD_ENTRIES = """
INSERT INTO page_ground_truth_entry (page_ground_truth_id, entry_mode, labels, source_path)
SELECT g.id, 'bulk_upload',
       COALESCE(
           (SELECT e.labels FROM page_ground_truth_entry e
             WHERE e.page_ground_truth_id = g.id AND e.entry_mode = 'bulk_upload'
             ORDER BY e.created_at DESC, e.id DESC LIMIT 1),
           to_jsonb(g) - 'id' - 'chart_name' - 'page_number' - 'source_page_id'
                       - 'source_path' - 'created_at' - 'updated_at' - 'user_id'
       ) || jsonb_build_object(%s::text, %s::text),
       %s
  FROM page_ground_truth g
 WHERE g.id = ANY(%s)
"""
_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def database_url() -> str:
    if DATABASE_URL:
        return DATABASE_URL
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env = Path(__file__).resolve().parents[1] / "core-pipeline" / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit("Set DATABASE_URL at the top of this script (or in the environment).")


def connect(url: str):
    try:
        import psycopg

        return psycopg.connect(url, connect_timeout=15)
    except ImportError:
        import psycopg2

        return psycopg2.connect(url, connect_timeout=15)


def where_clause() -> tuple[str, list]:
    """The rows to change: the column holds one of FROM_VALUES (and not already TO_VALUE)."""
    parts, args = [], []
    texts = [v for v in FROM_VALUES if v is not None and str(v).upper() != BLANK]
    if any(v is None or str(v).upper() == BLANK for v in FROM_VALUES):
        parts.append(f"({COLUMN} IS NULL OR btrim({COLUMN}::text) = '')")
    if texts:
        if IGNORE_CASE:
            parts.append(f"lower(btrim({COLUMN}::text)) = ANY(%s)")
            args.append([str(v).strip().lower() for v in texts])
        else:
            parts.append(f"btrim({COLUMN}::text) = ANY(%s)")
            args.append([str(v).strip() for v in texts])
    if not parts:
        sys.exit("FROM_VALUES is empty.")
    sql = "(" + " OR ".join(parts) + ")"
    if TO_VALUE is None:
        sql += f" AND {COLUMN} IS NOT NULL"
    else:
        sql += f" AND {COLUMN} IS DISTINCT FROM %s"
        args.append(TO_VALUE)
    if CHARTS:
        sql += " AND chart_name = ANY(%s)"
        args.append(list(CHARTS))
    return sql, args


def main() -> int:
    if not _NAME.match(COLUMN or ""):
        sys.exit(f"COLUMN must be a plain lower-case name, got {COLUMN!r}")
    if COLUMN in {"id", "chart_name", "page_number", "source_page_id", "source_path",
                  "created_at", "updated_at", "user_id"}:
        sys.exit(f"Refusing to change the key / bookkeeping column {COLUMN!r}.")
    where, args = where_clause()
    with connect(database_url()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (TABLE,),
        )
        columns = {r[0] for r in cur.fetchall()}
        if not columns:
            sys.exit(f"No table {TABLE!r} in this database.")
        cur.execute("SELECT 1 FROM information_schema.tables WHERE table_name = 'page_ground_truth_entry'")
        if not cur.fetchone():
            sys.exit("No page_ground_truth_entry table: open review-ui once (it creates it), then re-run.")
        if COLUMN not in columns:
            sys.exit(f"No column {COLUMN!r} in {TABLE}. Columns: {', '.join(sorted(columns))}")
        has_page = {"chart_name", "page_number"} <= columns
        if CHARTS and "chart_name" not in columns:
            sys.exit(f"CHARTS needs a chart_name column; {TABLE} has none.")

        # What would change, by current value (values are labels like "NA" / blank, not page text).
        cur.execute(
            f"SELECT COALESCE(NULLIF(btrim({COLUMN}::text), ''), '<blank>') AS v, count(*) "
            f"FROM {TABLE} WHERE {where} GROUP BY 1 ORDER BY 2 DESC",
            args,
        )
        by_value = cur.fetchall()
        total = sum(n for _, n in by_value)
        target = "NULL" if TO_VALUE is None else repr(TO_VALUE)
        scope = f"{len(CHARTS)} chart(s)" if CHARTS else "every chart"
        print(f"{TABLE}.{COLUMN} -> {target}  ({scope}){'  [DRY RUN]' if DRY_RUN else ''}")
        for value, n in by_value:
            print(f"  {value!r:>14}: {n:,} row(s)")
        print(f"  total: {total:,} row(s)")
        if has_page and SHOW_PAGES and total:
            cur.execute(
                f"SELECT chart_name, page_number FROM {TABLE} WHERE {where} "
                f"ORDER BY chart_name, page_number LIMIT %s",
                [*args, SHOW_PAGES],
            )
            print("  e.g. " + ", ".join(f"{c} p{p}" for c, p in cur.fetchall())
                  + (" ..." if total > SHOW_PAGES else ""))
        if DRY_RUN or not total:
            if DRY_RUN:
                print("Nothing changed. Set DRY_RUN = False to apply.")
            return 0
        cur.execute(f"UPDATE {TABLE} SET {COLUMN} = %s WHERE {where} RETURNING id", [TO_VALUE, *args])
        ids = [r[0] for r in cur.fetchall()]
        cur.execute(_ADD_ENTRIES, [COLUMN, TO_VALUE, SOURCE_PATH, ids])
        entries = cur.rowcount
        conn.commit()
        print(f"Changed {len(ids):,} page(s) in {TABLE}; added {entries:,} bulk_upload entr"
              f"{'y' if entries == 1 else 'ies'} in page_ground_truth_entry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
