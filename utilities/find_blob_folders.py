#!/usr/bin/env python3
"""Find chart folders under a blob prefix and write the path back to Excel.

You pass an .xlsx whose rows name the folders to find (one folder per row)
and a blob location such as ``imaging-pipeline/Raw_Input``. The script walks
virtual directories under that prefix (Run1/Batch1, Run2/Batch3/DEID_PNGs,
and so on) without listing page files, and appends a ``found_location``
column: ``imaging-pipeline/Raw_Input/Run1/Batch1/DEID_PNGs/<folder>``.

A name that appears in more than one place is written as several paths
separated by `` | ``. A name that is not under the prefix is ``NOT FOUND``.

Uses the core-pipeline venv and Azure settings in ``core-pipeline/.env``.

Two inputs: the blob location (``container/prefix``) and the local Excel file.

Examples::

    cd core-pipeline && source .venv/bin/activate

    python ../utilities/find_blob_folders.py imaging-pipeline/Raw_Input folders.xlsx
    python ../utilities/find_blob_folders.py imaging-pipeline/Raw_Input C:/lists/folders.xlsx --column "Folder Name"
"""
from __future__ import annotations

import argparse
import logging
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

from db.blob_store import get_container_client, normalize_prefix  # noqa: E402

logger = logging.getLogger("find_blob_folders")

DEFAULT_BLOB = "imaging-pipeline/Raw_Input"
LOCATION_HEADER = "found_location"
_HEADER_NAMES = {
    "folder",
    "folders",
    "folder_name",
    "folder name",
    "chart",
    "chart_name",
    "chart name",
    "name",
    "record_id",
    "record id",
}


def split_blob(blob: str) -> tuple[str, str]:
    """``imaging-pipeline/Raw_Input`` → container ``imaging-pipeline``, prefix ``Raw_Input``."""
    text = (blob or "").strip().replace("\\", "/").strip("/")
    if not text or "/" not in text:
        raise SystemExit(
            f"Expected container/prefix, for example {DEFAULT_BLOB!r}, got {blob!r}"
        )
    container, _, prefix = text.partition("/")
    if not container or not prefix:
        raise SystemExit(f"Expected container/prefix, got {blob!r}")
    return container, prefix.strip("/")


def _child_prefixes(client, prefix: str) -> list[str]:
    """Virtual subfolders of ``prefix`` (delimiter listing — no page blobs)."""
    pref = normalize_prefix(prefix)
    children: list[str] = []
    for item in client.walk_blobs(name_starts_with=pref, delimiter="/"):
        name = getattr(item, "name", None)
        if not isinstance(name, str):
            name = getattr(item, "prefix", None)
        if not isinstance(name, str) or not name.endswith("/"):
            continue
        if name.rstrip("/") == pref.rstrip("/"):
            continue
        children.append(name.strip("/"))
    return children


def find_folders(container: str, prefix: str, wanted: set[str]) -> dict[str, list[str]]:
    """Map each wanted folder name (casefold) to the blob paths where it sits.

    Stops at a matching folder: its page files are not listed. Keeps walking
    sibling Run/Batch/DEID folders so a name stored twice is reported twice.
    """
    client = get_container_client(container)
    found: dict[str, list[str]] = {name: [] for name in wanted}
    pending = [prefix.strip("/")]
    seen: set[str] = set()

    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        logger.info("listing %s/%s", container, current)
        for child in _child_prefixes(client, current):
            leaf = child.rsplit("/", 1)[-1]
            key = leaf.casefold()
            if key in found:
                path = f"{container}/{child}"
                if path not in found[key]:
                    found[key].append(path)
                    logger.info("found %s at %s", leaf, path)
                continue
            pending.append(child)
    return found


def _header_map(ws) -> dict[str, int]:
    headers: dict[str, int] = {}
    for col in range(1, ws.max_column + 1):
        value = ws.cell(1, col).value
        if value is None:
            continue
        headers[str(value).strip().casefold()] = col
    return headers


def _folder_column(ws, column: str | None) -> tuple[int, int]:
    """Return ``(column_index, first_data_row)``. Row 1 is a header when it has no digits."""
    headers = _header_map(ws)
    if column and column.strip().isdigit():
        col = int(column)
        if col < 1:
            raise SystemExit("--column must be 1 or greater")
        return col, 1
    if column:
        key = column.strip().casefold()
        if key not in headers:
            known = ", ".join(sorted(headers)) or "(none)"
            raise SystemExit(f"No column {column!r}. Headers: {known}")
        return headers[key], 2
    for name in (
        "folder",
        "folder_name",
        "folder name",
        "chart_name",
        "chart name",
        "chart",
        "name",
    ):
        if name in headers:
            return headers[name], 2
    first = ws.cell(1, 1).value
    text = "" if first is None else str(first).strip()
    if text.casefold() in _HEADER_NAMES or not any(ch.isdigit() for ch in text):
        return 1, 2
    return 1, 1


def _location_column(ws) -> int:
    headers = _header_map(ws)
    if LOCATION_HEADER in headers:
        return headers[LOCATION_HEADER]
    col = ws.max_column + 1
    ws.cell(1, col).value = LOCATION_HEADER
    return col


def locate_workbook(path: Path, blob: str, column: str | None, out: Path | None) -> Path:
    from openpyxl import load_workbook

    container, prefix = split_blob(blob)
    wb = load_workbook(path)
    ws = wb.active
    col, start = _folder_column(ws, column)
    loc_col = _location_column(ws)

    rows: list[tuple[int, str]] = []
    for row in range(start, ws.max_row + 1):
        value = ws.cell(row, col).value
        if value is None or not str(value).strip():
            continue
        rows.append((row, str(value).strip()))
    if not rows:
        raise SystemExit(f"No folder names in {path} column {col}")

    wanted = {name.casefold() for _row, name in rows}
    logger.info(
        "looking for %d folder(s) under %s/%s", len(wanted), container, prefix
    )
    found = find_folders(container, prefix, wanted)

    hit = 0
    for row, name in rows:
        paths = found.get(name.casefold()) or []
        ws.cell(row, loc_col).value = " | ".join(paths) if paths else "NOT FOUND"
        if paths:
            hit += 1

    dest = out or path.with_name(f"{path.stem}_located{path.suffix}")
    wb.save(dest)
    logger.info(
        "wrote %s — %d found, %d not found", dest, hit, len(rows) - hit
    )
    return dest


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "blob",
        help=f"Blob location to search, container/prefix (for example {DEFAULT_BLOB})",
    )
    parser.add_argument(
        "excel",
        type=Path,
        help="Local xlsx with one folder name per row",
    )
    parser.add_argument(
        "--column",
        default=None,
        help="Header name or 1-based column number. Default: a folder/chart column, else column A",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Where to write the workbook (default: <name>_located.xlsx beside the input)",
    )
    args = parser.parse_args()
    excel = args.excel.expanduser().resolve()
    if not excel.is_file():
        raise SystemExit(f"Excel file not found: {excel}")
    if excel.suffix.casefold() != ".xlsx":
        raise SystemExit("Need an .xlsx file (openpyxl does not read .xls)")
    locate_workbook(excel, args.blob, args.column, args.out)


if __name__ == "__main__":
    main()
