"""Fill the batch metadata CSVs from the client's extra-columns dump, then load them.

The client sends one dump across all charts, ``metadata_extra.csv`` (one row per recordId),
placed beside the batch files in review-ui/data/metadata. It carries columns the batch
files do not: MemberID, ClientID, ProjectID, SourceName, RR Method, Measure, ServiceNote. This utility:

  1. reads the dump (``--source``, default ``<metadata dir>/metadata_extra.csv``; .csv / .xlsx),
  2. for every batch file in the metadata folder (metadata_R<n>_B<n>.csv, metadata_demotest.csv;
     never the dump itself), adds those
     columns when the file lacks them and fills each row from the master by recordId (a
     value in the master replaces the file's; a blank in the master keeps it). Names,
     DOB and every other column are left alone,
  3. optionally writes the master records no batch file has into ``--new-file``,
  4. with ``--load``, loads the batch files into manifest_member_list (jobs.manifest_sweeper),
     which writes the new columns to the database.

The same is ``POST /api/manifest/metadata-extra`` and ``utilities/fill_metadata_extra.py``.

Usage (from core-pipeline/):
  python -m jobs.metadata_expand --dry-run                 # report only
  python -m jobs.metadata_expand --load                    # fill the CSVs, then the DB
  python -m jobs.metadata_expand --metadata-dir /path/to/metadata --source dump.xlsx --load
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import METADATA_ROOT
from jobs.manifest_sweeper import (
    EXTRA_FILE_NAME,
    EXTRA_HEADERS,
    _record_id,
    header_key,
    parse_manifest_bytes,
    parse_run_batch_from_name,
)

logger = logging.getLogger(__name__)

# The columns this utility owns, in the order they are appended, with the header written
# when a file has none of that column's spellings.
TARGET_COLUMNS: list[tuple[str, tuple[str, ...]]] = [
    ("MemberID", ("MemberID", "MemberId", "member_id", "external_member_id")),
    ("ClientID", ("client_id", *EXTRA_HEADERS["client_id"])),
    ("ProjectID", ("project_id", *EXTRA_HEADERS["project_id"])),
    ("SourceName", ("source_name", *EXTRA_HEADERS["source_name"])),
    ("RR Method", ("rr_method", *EXTRA_HEADERS["rr_method"])),
    ("Measure", ("measure", *EXTRA_HEADERS["measure"])),
    ("ServiceNote", ("service_note", *EXTRA_HEADERS["service_note"])),
]


def _clean(value: object) -> str:
    text = "" if value is None else str(value).strip()
    # Excel hands whole numbers back as "993544760.0".
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def _find_header(headers: list[str], spellings: tuple[str, ...]) -> Optional[str]:
    wanted = {header_key(s) for s in spellings}
    return next((h for h in headers if header_key(h) in wanted), None)


def read_master(path: Path) -> dict[str, dict[str, str]]:
    """recordId -> {target header: value}, the first row of each recordId."""
    rows = parse_manifest_bytes(path.name, path.read_bytes())
    if not rows:
        raise RuntimeError(f"No rows in {path}")
    headers = list(rows[0].keys())
    columns = {target: _find_header(headers, spellings) for target, spellings in TARGET_COLUMNS}
    missing = [target for target, header in columns.items() if header is None]
    if missing:
        logger.warning("Master has no column for %s; those stay as they are", ", ".join(missing))
    master: dict[str, dict[str, str]] = {}
    for row in rows:
        rid = _clean(_record_id({k: _clean(v) for k, v in row.items() if k}))
        if not rid:
            continue
        values = {target: _clean(row.get(header)) for target, header in columns.items() if header}
        if rid in master:
            if master[rid] != values:
                logger.warning("recordId %s appears more than once in the master; the first row is used", rid)
            continue
        master[rid] = values
    return master


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    # Write beside the file, then replace it, so a failure never leaves half a CSV.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def expand_file(path: Path, master: dict[str, dict[str, str]], *, dry_run: bool) -> dict[str, object]:
    """Add the target columns to one batch CSV and fill them from the master."""
    headers, rows = _read_csv(path)
    record_header = _find_header(headers, ("recordId", "RecordId", "record_id", "chart_name", "ChartName"))
    if record_header is None:
        return {"file": path.name, "skipped": "no recordId column"}
    added: list[str] = []
    column_of: dict[str, str] = {}
    for target, spellings in TARGET_COLUMNS:
        existing = _find_header(headers, (target, *spellings))
        if existing is None:
            headers.append(target)
            added.append(target)
            existing = target
        column_of[target] = existing
    matched = filled = 0
    seen: set[str] = set()
    for row in rows:
        rid = _clean(row.get(record_header))
        values = master.get(rid)
        for column in column_of.values():
            row.setdefault(column, "")
        if values is None:
            continue
        matched += 1
        seen.add(rid)
        for target, value in values.items():
            column = column_of[target]
            if value and row.get(column) != value:
                row[column] = value
                filled += 1
    if not dry_run and (added or filled):
        _write_csv(path, headers, rows)
    return {
        "file": path.name,
        "rows": len(rows),
        "matched": matched,
        "cells_filled": filled,
        "columns_added": added,
        "record_ids": seen,
    }


def batch_files(metadata_dir: Path) -> list[Path]:
    """The batch CSVs: a run and batch in the name (metadata_R1_B1.csv) or metadata_demotest."""
    return sorted(
        p
        for p in metadata_dir.glob("*.csv")
        if not p.name.startswith("._") and parse_run_batch_from_name(p.name)[0] is not None
    )


def run(
    source: Optional[Path] = None,
    *,
    metadata_dir: Path = METADATA_ROOT,
    new_file: Optional[str] = None,
    load: bool = False,
    dry_run: bool = False,
) -> dict[str, object]:
    source = source or metadata_dir / EXTRA_FILE_NAME
    if not source.is_file():
        raise FileNotFoundError(f"No extra-columns dump at {source}")
    master = read_master(source)
    files = [p for p in batch_files(metadata_dir) if p.name != new_file]
    results = [expand_file(path, master, dry_run=dry_run) for path in files]
    covered: set[str] = set()
    for result in results:
        covered |= result.pop("record_ids", set())  # type: ignore[arg-type]
    unmatched = sorted(rid for rid in master if rid not in covered)

    if new_file and unmatched and not dry_run:
        headers = ["recordId", *(target for target, _ in TARGET_COLUMNS)]
        rows = [{"recordId": rid, **master[rid]} for rid in unmatched]
        target = metadata_dir / new_file
        if target.exists():
            raise RuntimeError(f"{target} exists; pick a new --new-file name")
        _write_csv(target, headers, rows)
        logger.info("Wrote %d records no batch file had to %s", len(rows), target)
    elif unmatched:
        logger.warning(
            "%d master records are in no batch file (pass --new-file to write them): %s",
            len(unmatched), ", ".join(unmatched[:10]) + (" …" if len(unmatched) > 10 else ""),
        )

    loaded = None
    if load and not dry_run:
        from jobs.manifest_sweeper import run_load

        loaded = {}
        for path in files + ([metadata_dir / new_file] if new_file and (metadata_dir / new_file).is_file() else []):
            result = run_load(local_path=path)
            result.pop("files_detail", None)
            loaded[path.name] = result

    return {
        "source": str(source),
        "master_records": len(master),
        "files": results,
        "unmatched": len(unmatched),
        "dry_run": dry_run,
        "loaded": loaded,
    }


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--source", type=Path, default=None,
        help=f"The extra-columns dump (.csv / .xlsx); default <metadata dir>/{EXTRA_FILE_NAME}",
    )
    parser.add_argument(
        "--metadata-dir", type=Path, default=METADATA_ROOT,
        help=f"Folder of batch metadata CSVs (default {METADATA_ROOT})",
    )
    parser.add_argument("--new-file", help="Write master records no batch file has to this CSV in the folder")
    parser.add_argument("--load", action="store_true", help="Then load the folder into manifest_member_list")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change; write nothing")
    args = parser.parse_args(argv)
    result = run(
        args.source.expanduser() if args.source else None,
        metadata_dir=args.metadata_dir.expanduser(),
        new_file=args.new_file,
        load=args.load,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
