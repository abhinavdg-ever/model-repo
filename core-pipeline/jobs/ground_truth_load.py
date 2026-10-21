"""Load client page-level ground truth into page_ground_truth.

The spreadsheet is one row per page. Chart_Name is the chart folder name.
Id is the page file stem: 1, 1.jpg, 1.png, and 1.tif are the same page.

A load can run before the chart exists. Re-runs upsert on
(chart_name, page_number). Only the label columns the file has are written:
a file with just Chart_Name, Id and Sequence sets page_sequence and leaves every
other label as it is. Within a column the file has, an empty cell clears the
label. Pages absent from a later file are left in place.
Each row written also appends a ``bulk_upload`` entry to
page_ground_truth_entry (the page's previous bulk labels with this file's on
top), so review-ui can show the latest load with manual edits hidden.

Usage:
  python -m jobs.ground_truth_load --local /path/to/ground_truth.xlsx
  python cli.py ground-truth --local /path/to/ground_truth.csv
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_HEADER = {
    "chartname": "chart_name",
    "id": "source_page_id",
    "membername": "member_name",
    "memberdob": "member_dob",
    "dosfrom": "dos_from",
    "dosto": "dos_to",
    "encountertype": "encounter_type",
    "pagetype": "page_type",
    "codableornoncodable": "codeable",
    "codeableornoncodeable": "codeable",
    "blankpage": "blank_page",
    # The numeric Sequence column. "Page Sequence" in the client sheet is something
    # else and is not loaded.
    "sequence": "page_sequence",
    "junkpage": "junk_page",
    "isinvoicepage": "is_invoice",
    "rotation": "rotation",
    "isvisible": "is_visible",
    "renderingprovider": "rendering_provider",
    "providerspecialty": "provider_specialty",
    "providersignature": "provider_signature",
    "deletedlevel": "deleted_level",
}

_TEXT_FIELDS = (
    "member_name",
    "member_dob",
    "dos_from",
    "dos_to",
    "encounter_type",
    "page_type",
    "codeable",
    "blank_page",
    "junk_page",
    "is_invoice",
    "page_sequence",
    "rotation",
    "is_visible",
    "rendering_provider",
    "provider_specialty",
    "provider_signature",
    "deleted_level",
)

# Databases created before is_visible existed gain the column on first load.
_ADD_IS_VISIBLE = "ALTER TABLE page_ground_truth ADD COLUMN IF NOT EXISTS is_visible TEXT"

# Databases created before page_ground_truth_entry gain it on first load.
# Kept in step with schema/v1.sql and review-ui's services/ground_truth.py.
_ADD_ENTRY_TABLE = """
CREATE TABLE IF NOT EXISTS page_ground_truth_entry (
    id                      BIGSERIAL PRIMARY KEY,
    page_ground_truth_id    BIGINT NOT NULL REFERENCES page_ground_truth(id) ON DELETE CASCADE,
    entry_mode              VARCHAR(20) NOT NULL
                            CHECK (entry_mode IN ('bulk_upload', 'manual_entry')),
    labels                  JSONB NOT NULL DEFAULT '{}'::jsonb,
    source_path             TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_page_ground_truth_entry_page_ground_truth_id_entry_mode
    ON page_ground_truth_entry(page_ground_truth_id, entry_mode);
"""

def upsert_sql(columns: Sequence[str]) -> str:
    """The upsert for a file whose label columns are ``columns`` (names from
    _TEXT_FIELDS only): those labels, the page id and source path are written;
    every other label keeps its stored value."""
    unknown = set(columns) - set(_TEXT_FIELDS)
    if unknown:
        raise ValueError(f"not ground-truth columns: {sorted(unknown)}")
    insert = ["chart_name", "page_number", "source_page_id", *columns, "source_path"]
    updates = ",\n    ".join(f"{column} = EXCLUDED.{column}" for column in (*columns, "source_path"))
    return f"""
WITH g AS (
INSERT INTO page_ground_truth ({", ".join(insert)})
VALUES ({", ".join(f"%({column})s" for column in insert)})
ON CONFLICT (chart_name, page_number) DO UPDATE SET
    source_page_id = COALESCE(EXCLUDED.source_page_id, page_ground_truth.source_page_id),
    {updates},
    updated_at = now()
RETURNING id
)
INSERT INTO page_ground_truth_entry (page_ground_truth_id, entry_mode, labels, source_path)
SELECT g.id, 'bulk_upload',
       COALESCE((SELECT e.labels FROM page_ground_truth_entry e
                  WHERE e.page_ground_truth_id = g.id AND e.entry_mode = 'bulk_upload'
                  ORDER BY e.id DESC LIMIT 1), '{{}}'::jsonb)
       || %(labels)s::jsonb,
       %(source_path)s
  FROM g
"""


# Every label column: a sheet with the full set.
_UPSERT = upsert_sql(_TEXT_FIELDS)


def norm_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def page_number_from_id(value: Any) -> Optional[int]:
    """Id 1, 1.jpg, 1.png, and 1.tif are page 1. Anything else is not a page id."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float) and value == int(value):
        value = int(value)
    elif isinstance(value, int):
        return value if value > 0 else None
    text = str(value).strip()
    if not text:
        return None
    stem = Path(text).stem if "." in text else text
    if not stem.isdigit():
        return None
    number = int(stem)
    return number if number > 0 else None


def _cell(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and value == int(value):
        value = int(value)
    text = str(value).strip()
    return text or None


def whole_number(value: Optional[str]) -> Optional[str]:
    """A sequence as a whole number: "3", "3.0" and "03" are "3". Anything else is kept
    as written, so a bad cell shows up rather than vanishing."""
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return value
    return str(int(number)) if number == int(number) else value


# Codeable, Non Codeable, Non-Codeable, NonCodeable, non_codeable, Codable …
_CODEABLE = re.compile(r"\b(?:(non)[\s_-]*)?code?able\b", re.IGNORECASE)


def spell_codeable(value: Optional[str]) -> Optional[str]:
    """Codable / Non-Codable, however a sheet or an older run spelled it."""
    if not value:
        return value
    return _CODEABLE.sub(lambda m: "Non-Codable" if m.group(1) else "Codable", value)


def parse_rows(headers: list[Any], data_rows: list[list[Any]]) -> list[dict[str, Any]]:
    """Map a header row and data rows onto page_ground_truth columns. Each row holds
    only the label columns the headers name, listed in ``_columns``."""
    columns = [norm_header(h) for h in headers]
    named = {_HEADER.get(key) for key in columns}
    present = tuple(field for field in _TEXT_FIELDS if field in named)
    out: list[dict[str, Any]] = []
    for raw in data_rows:
        if raw is None or not any(c is not None and str(c).strip() for c in raw):
            continue
        mapped: dict[str, Any] = {}
        for idx, key in enumerate(columns):
            field = _HEADER.get(key)
            if not field or idx >= len(raw):
                continue
            mapped[field] = raw[idx]
        chart = _cell(mapped.get("chart_name"))
        page_number = page_number_from_id(mapped.get("source_page_id"))
        if not chart or page_number is None:
            out.append({"_skip": True})
            continue
        row: dict[str, Any] = {
            "chart_name": chart,
            "page_number": page_number,
            "source_page_id": _cell(mapped.get("source_page_id")),
            "_columns": present,
        }
        for field in present:
            cell = _cell(mapped.get(field))
            if field == "codeable":
                cell = spell_codeable(cell)
            elif field == "page_sequence":
                cell = whole_number(cell)
            row[field] = cell
        out.append(row)
    return out


# A CSV saved from Excel on Windows is often cp1252, not UTF-8 ("é", "’", "–" break
# UTF-8 decoding). latin-1 decodes any byte, so it is the last resort.
CSV_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")


def decode_csv(data: bytes) -> str:
    """The file's text: UTF-8 (with or without BOM), else cp1252, else latin-1."""
    for encoding in CSV_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def _read_csv(path: Path) -> tuple[list[Any], list[list[Any]]]:
    rows = list(csv.reader(io.StringIO(decode_csv(path.read_bytes()), newline="")))
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _read_xlsx(path: Path) -> tuple[list[Any], list[list[Any]]]:
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = book.active
        rows = [list(row) for row in sheet.iter_rows(values_only=True)]
    finally:
        book.close()
    if not rows:
        return [], []
    return list(rows[0]), [list(r) for r in rows[1:]]


def read_table(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.casefold()
    if suffix == ".csv":
        headers, data = _read_csv(path)
    elif suffix in {".xlsx", ".xlsm"}:
        headers, data = _read_xlsx(path)
    else:
        raise ValueError(f"unsupported ground-truth file: {path.name}")
    rows = parse_rows(headers, data)
    for row in rows:
        if not row.get("_skip"):
            row["source_path"] = str(path)
    return rows


def entry_labels(row: dict[str, Any], columns: Sequence[str] = _TEXT_FIELDS) -> str:
    """The labels a load writes, as the page_ground_truth_entry JSON. An empty
    cell is kept as null: the load cleared that column."""
    return json.dumps({field: row.get(field) for field in columns})


def upsert_rows(conn: Any, rows: list[dict[str, Any]], columns: Optional[Sequence[str]] = None) -> int:
    """Upsert parsed rows, each with the label columns its file had (or ``columns``
    for every row). Returns the number of rows written."""
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("_skip"):
            continue
        groups.setdefault(tuple(columns if columns is not None else row["_columns"]), []).append(row)
    with conn.cursor() as cur:
        cur.execute(_ADD_IS_VISIBLE)
        cur.execute(_ADD_ENTRY_TABLE)
        for group_columns, group in groups.items():
            cur.executemany(
                upsert_sql(group_columns),
                [
                    {
                        **{column: row.get(column) for column in group_columns},
                        "chart_name": row["chart_name"],
                        "page_number": row["page_number"],
                        "source_page_id": row.get("source_page_id"),
                        "source_path": row.get("source_path"),
                        "labels": entry_labels(row, group_columns),
                    }
                    for row in group
                ],
            )
    return sum(len(group) for group in groups.values())


def iter_files(path: Path) -> Iterator[Path]:
    if path.is_file():
        yield path
        return
    if not path.is_dir():
        return
    for child in sorted(path.iterdir()):
        if child.suffix.casefold() in {".csv", ".xlsx", ".xlsm"} and not child.name.startswith("."):
            yield child


def run_load(local_path: str) -> dict[str, Any]:
    """Read local CSV/XLSX files and upsert page_ground_truth. Requires Postgres."""
    from db import connect, is_skip_db_write

    if is_skip_db_write():
        raise RuntimeError("ground-truth load writes Postgres; skip_db_write is on")

    root = Path(local_path)
    if not root.exists():
        raise FileNotFoundError(local_path)

    parsed: list[dict[str, Any]] = []
    files = 0
    for path in iter_files(root):
        files += 1
        parsed.extend(read_table(path))

    writable = [row for row in parsed if not row.get("_skip")]
    skipped = len(parsed) - len(writable)
    charts = sorted({row["chart_name"] for row in writable})

    if writable:
        with connect() as conn:
            upsert_rows(conn, writable)

    return {
        "files": files,
        "rows_read": len(parsed),
        "rows_written": len(writable),
        "rows_skipped": skipped,
        "charts": charts,
        "columns": sorted({column for row in writable for column in row["_columns"]}),
    }
