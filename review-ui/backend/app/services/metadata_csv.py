"""Load stacked metadata_R{n}_B{n}.csv files (DATA_MODE=local manifest source)."""

from __future__ import annotations

import csv
import re
from functools import lru_cache
from pathlib import Path

from app.core.schemas import ImagingManifestDetails

METADATA_FILE_RE = re.compile(r"^metadata_R(\d+)_B(\d+)\.csv$", re.IGNORECASE)


def discover_metadata_csvs(metadata_dir: Path) -> list[Path]:
    if not metadata_dir.is_dir():
        return []
    found: list[tuple[int, int, Path]] = []
    for path in metadata_dir.iterdir():
        if not path.is_file() or path.name.startswith("._"):
            continue
        m = METADATA_FILE_RE.match(path.name)
        if not m:
            continue
        found.append((int(m.group(1)), int(m.group(2)), path))
    found.sort(key=lambda t: (t[0], t[1], t[2].name))
    return [p for _, _, p in found]


def run_batch_from_metadata_name(name: str) -> tuple[str | None, str | None]:
    """metadata_R1_B1.csv → ('R1', 'B1')."""
    m = METADATA_FILE_RE.match(Path(name).name)
    if not m:
        return None, None
    return f"R{m.group(1)}", f"B{m.group(2)}"


def read_metadata_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows: list[dict[str, str]] = []
        for row in reader:
            norm = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
            if not norm.get("recordId"):
                continue
            rows.append(norm)
        return rows


def row_to_manifest(row: dict[str, str]) -> ImagingManifestDetails:
    first = row.get("DummyFirstName", "")
    last = row.get("DummyLastName", "")
    member = f"{first} {last}".strip() or None
    return ImagingManifestDetails(
        member=member,
        dob=row.get("DummyDOB") or None,
        memberId=row.get("MemberID") or None,
    )


@lru_cache(maxsize=4)
def _index_metadata(metadata_dir: str) -> dict[str, list[dict[str, str]]]:
    """recordId → stacked entries from all metadata_Rn_Bn CSVs.

    Each entry is ``{"run_id", "batch_id", **csv_row}``.
    """
    by_record: dict[str, list[dict[str, str]]] = {}
    for path in discover_metadata_csvs(Path(metadata_dir)):
        run_id, batch_id = run_batch_from_metadata_name(path.name)
        for row in read_metadata_rows(path):
            entry = dict(row)
            if run_id:
                entry["run_id"] = run_id
            if batch_id:
                entry["batch_id"] = batch_id
            by_record.setdefault(row["recordId"], []).append(entry)
    return by_record


def clear_metadata_cache() -> None:
    _index_metadata.cache_clear()


def manifest_for_record(
    metadata_dir: Path,
    record_id: str,
) -> ImagingManifestDetails | None:
    """First matching row for recordId (folder / chart name), or None."""
    if not metadata_dir.is_dir():
        return None
    index = _index_metadata(str(metadata_dir.resolve()))
    rows = index.get(record_id) or []
    if not rows:
        return None
    return row_to_manifest(rows[0])


def run_batch_for_record(
    metadata_dir: Path | None,
    record_id: str,
) -> tuple[str | None, str | None]:
    """Run/batch from the metadata_Rn_Bn.csv that lists this chart, or (None, None)."""
    if metadata_dir is None or not metadata_dir.is_dir():
        return None, None
    index = _index_metadata(str(metadata_dir.resolve()))
    rows = index.get(record_id) or []
    if not rows:
        return None, None
    first = rows[0]
    run_id = (first.get("run_id") or "").strip() or None
    batch_id = (first.get("batch_id") or "").strip() or None
    return run_id, batch_id
