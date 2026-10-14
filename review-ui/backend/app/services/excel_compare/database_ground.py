"""Ground truth for Manual Review read from page_ground_truth instead of an
uploaded sheet.

PDF ID is the chart name and Page Number the page number. Only the labels
whose meaning matches an Excel Compare column are carried over; member name
and DOB are Yes / No presence labels in the database, not the extracted
values the tool compares, so they are left out. A column no page has a value
for is dropped, so it reads as "not present" rather than as a blank on every
row.
"""

from __future__ import annotations

import csv
import io

# Manual Review column -> page_ground_truth column.
DIRECT_COLUMNS = {
    "DOS From": "dos_from",
    "DOS To": "dos_to",
    "Page Type": "page_type",
    "Page Subtype": "page_subtype",
    "Codable / Non-Codable": "codeable",
    "Duplicate": "is_duplicate",
}

BLANK_OR_JUNK = "Is Blank or Junk?"


def _yes(value: object) -> bool:
    return str(value or "").strip().lower().startswith("y")


def _blank_or_junk(labels: dict) -> str:
    """Yes when either label says Yes; No when one is labelled; else blank."""
    blank, junk = labels.get("blank_page"), labels.get("junk_page")
    if _yes(blank) or _yes(junk):
        return "Yes"
    if str(blank or "").strip() or str(junk or "").strip():
        return "No"
    return ""


def ground_csv(rows: list[tuple[str, int, dict]]) -> bytes:
    """The database labels as the CSV an uploaded Ground Truth file would be."""
    records = []
    for chart, page_number, labels in rows:
        record = {"PDF ID": chart, "Page Number": page_number}
        for column, source in DIRECT_COLUMNS.items():
            value = labels.get(source)
            record[column] = "" if value is None else str(value).strip()
        record[BLANK_OR_JUNK] = _blank_or_junk(labels)
        records.append(record)
    columns = ["PDF ID", "Page Number"] + [
        column
        for column in [*DIRECT_COLUMNS, BLANK_OR_JUNK]
        if any(record[column] for record in records)
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(records)
    return buffer.getvalue().encode("utf-8")

