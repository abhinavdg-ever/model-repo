"""Page annotations, one CSV for every chart.

``review-ui/data/annotation/annotations.csv``. A later save of the same
folder, page file and field replaces that row.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

COLUMNS = [
    "folder_id",
    "page_number",
    "page_file",
    "field_id",
    "verdict",
    "value",
    "saved_at",
]
_VERDICTS = {"correct", "wrong"}


def annotation_file(review_root: Path) -> Path:
    return review_root / "data" / "annotation" / "annotations.csv"


def _clean_id(value: str, label: str) -> str:
    text = (value or "").strip()
    if not text or "/" in text or "\\" in text or text in {".", ".."}:
        raise HTTPException(status_code=400, detail=f"Invalid {label}")
    return text


def load_annotations(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows: list[dict[str, str]] = []
        for raw in reader:
            row = {column: (raw.get(column) or "").strip() for column in COLUMNS}
            if row["folder_id"] and row["page_file"] and row["field_id"]:
                rows.append(row)
    return rows


def _write(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def save_annotation(
    path: Path,
    *,
    folder_id: str,
    page_number: int,
    page_file: str,
    field_id: str,
    verdict: str,
    value: str,
) -> dict[str, str]:
    folder_id = _clean_id(folder_id, "folder id")
    page_file = _clean_id(page_file, "page file")
    field_id = _clean_id(field_id, "field")
    verdict = (verdict or "").strip().lower()
    if verdict not in _VERDICTS:
        raise HTTPException(status_code=400, detail="verdict must be correct or wrong")
    if page_number < 1:
        raise HTTPException(status_code=400, detail="Invalid page number")
    stored_value = "" if verdict == "correct" else (value or "").strip()
    if verdict == "wrong" and not stored_value:
        raise HTTPException(status_code=400, detail="A wrong annotation needs the correct value")
    row = {
        "folder_id": folder_id,
        "page_number": str(page_number),
        "page_file": page_file,
        "field_id": field_id,
        "verdict": verdict,
        "value": stored_value,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    rows = [
        existing
        for existing in load_annotations(path)
        if not (
            existing["folder_id"] == folder_id
            and existing["page_file"] == page_file
            and existing["field_id"] == field_id
        )
    ]
    rows.append(row)
    _write(path, rows)
    return row
