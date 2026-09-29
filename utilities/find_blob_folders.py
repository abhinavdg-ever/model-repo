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
import re
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
# Chart folders are the leaves (67909080_56076434). Run / Batch / DEID are not.
_CHART_FOLDER = re.compile(r"^\d+_\d+$")

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


def find_folders(
    container: str,
    prefix: str,
    wanted: set[str],
    on_found=None,
) -> dict[str, list[str]]:
    """Map each wanted folder name (casefold) to the blob paths where it sits.

    Stops at a matching folder: its page files are not listed. Keeps walking
    sibling Run/Batch/DEID folders so a name stored twice is reported twice.
    ``on_found(name, paths)`` runs each time a wanted folder is located.
    """
    _silence_sdk()
    client = get_container_client(container)
    found: dict[str, list[str]] = {name: [] for name in wanted}
    pending = [prefix.strip("/")]
    seen: set[str] = set()

    while pending:
        current = pending.pop(0)
        if current in seen:
            continue
        seen.add(current)
        for child in _child_prefixes(client, current):
            leaf = child.rsplit("/", 1)[-1]
            key = leaf.casefold()
            if key in found:
                path = f"{container}/{child}"
                if path not in found[key]:
                    found[key].append(path)
                    if on_found is not None:
                        on_found(leaf, list(found[key]))
                continue
            # A chart folder only holds page files. Listing inside one that is
            # not on the Excel list is a request per chart and never a hit.
            if _CHART_FOLDER.match(leaf):
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
        "folder name",
        "folder_name",
        "folder",
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


def _silence_sdk() -> None:
    """Hide Azure request/response dumps. Those lines are HTTP 200 traces."""
    for name in (
        "azure",
        "azure.core",
        "azure.core.pipeline",
        "azure.core.pipeline.policies",
        "azure.core.pipeline.policies.http_logging_policy",
        "azure.identity",
        "urllib3",
        "urllib3.connectionpool",
    ):
        logging.getLogger(name).setLevel(logging.ERROR)


def locate_workbook(path: Path, blob: str, column: str | None, out: Path | None) -> Path:
    from openpyxl import load_workbook

    container, prefix = split_blob(blob)
    wb = load_workbook(path)
    ws = wb.active
    col, start = _folder_column(ws, column or "Folder Name")
    loc_col = _location_column(ws)

    rows: list[tuple[int, str]] = []
    for row in range(start, ws.max_row + 1):
        value = ws.cell(row, col).value
        if value is None or not str(value).strip():
            continue
        rows.append((row, str(value).strip()))
    if not rows:
        raise SystemExit(f"No folder names under the Folder Name column in {path}")

    by_key: dict[str, list[int]] = {}
    for row, name in rows:
        by_key.setdefault(name.casefold(), []).append(row)

    dest = out or path
    found_count = 0

    def on_found(name: str, paths: list[str]) -> None:
        nonlocal found_count
        print(f"processing {name}", flush=True)
        text = " | ".join(paths)
        for row in by_key.get(name.casefold(), []):
            ws.cell(row, loc_col).value = text
        found_count += 1
        if found_count % 10 == 0:
            wb.save(dest)

    print(f"processing {len(rows)} folder(s) under {container}/{prefix}", flush=True)
    found = find_folders(container, prefix, set(by_key), on_found)

    missing = 0
    for row, name in rows:
        if found.get(name.casefold()):
            continue
        ws.cell(row, loc_col).value = "NOT FOUND"
        missing += 1
    wb.save(dest)
    print(
        f"saved {dest} — {found_count} found, {missing} not found",
        flush=True,
    )
    return dest


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    _silence_sdk()
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
        help="Where to write (default: the same xlsx, updated every 10 finds)",
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
