"""Ground truth for Manual Review in the page_ground_truth format: rows read
from the table, or an uploaded sheet that mirrors it (``from_upload``). Both
go through ``ground_csv``, so the two read the same way.

PDF ID is the chart name and Page Number the page number. Only the labels
whose meaning matches a Manual Review column are carried over. Member name,
DOB and ID are Yes / No presence labels; the comparison reads them as "was a
value found" (field_comparator.PRESENCE_FIELDS). Page Type is encounter_type;
page_type is ignored. printed_or_handwritten is not carried over: Type
(Printed/HW) is compared against an uploaded sheet's column only. A column
no page has a value for is dropped, so it
reads as "not present" rather than as a blank on every row.
"""

from __future__ import annotations

import csv
import io

# Manual Review column -> page_ground_truth column.
DIRECT_COLUMNS = {
    "Extracted Name": "member_name",
    "Extracted DOB": "member_dob",
    "Member ID": "member_id",
    "DOS From": "dos_from",
    "DOS To": "dos_to",
    "Page Subtype": "page_subtype",
    "Codable / Non-Codable": "codeable",
    "Duplicate": "is_duplicate",
}

BLANK_OR_JUNK = "Is Blank or Junk?"
PAGE_TYPE = "Page Type"
CREDENTIALS = "Provider Credentials"


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


def _page_type(labels: dict) -> str:
    """encounter_type; page_type is ignored."""
    return str(labels.get("encounter_type") or "").strip()


def _credentials(labels: dict) -> str:
    """provider_credentials; older rows kept credentials in provider_signature,
    so that is read when it holds more than Yes / No / NA (as the Accuracy tab does)."""
    own = str(labels.get("provider_credentials") or "").strip()
    if own:
        return own
    legacy = str(labels.get("provider_signature") or "").strip()
    if legacy.lower() in {"", "yes", "y", "no", "n", "true", "false", "na", "n/a", "none", "not found"}:
        return ""
    return legacy


def ground_csv(rows: list[tuple[str, int, dict]]) -> bytes:
    """The database labels as the CSV an uploaded Ground Truth file would be."""
    records = []
    for chart, page_number, labels in rows:
        record = {"PDF ID": chart, "Page Number": page_number}
        for column, source in DIRECT_COLUMNS.items():
            value = labels.get(source)
            record[column] = "" if value is None else str(value).strip()
        record[PAGE_TYPE] = _page_type(labels)
        record[CREDENTIALS] = _credentials(labels)
        record[BLANK_OR_JUNK] = _blank_or_junk(labels)
        records.append(record)
    columns = ["PDF ID", "Page Number"] + [
        column
        for column in [*DIRECT_COLUMNS, PAGE_TYPE, BLANK_OR_JUNK, CREDENTIALS]
        if any(record[column] for record in records)
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(records)
    return buffer.getvalue().encode("utf-8")


# The two columns that make a sheet a page_ground_truth mirror.
KEY_COLUMNS = ("chart_name", "page_number")


def is_table_format(data: bytes, filename: str) -> bool:
    """True when the sheet's headers include chart_name and page_number."""
    return all(column in _read(data, filename).columns for column in KEY_COLUMNS)


def from_upload(data: bytes, filename: str) -> bytes:
    """An uploaded page_ground_truth mirror as the same Ground Truth CSV the
    database rows become (``ground_csv``)."""
    frame = _read(data, filename)
    rows = []
    for record in frame.to_dict("records"):
        chart = str(record.get("chart_name") or "").strip()
        page = str(record.get("page_number") or "").strip()
        if not chart or not page:
            continue
        try:
            page_number = int(float(page))
        except ValueError:
            continue
        labels = {key: (None if value == "" else value) for key, value in record.items()}
        rows.append((chart, page_number, labels))
    if not rows:
        raise ValueError("Ground Truth file has no rows with a chart_name and page_number.")
    return ground_csv(rows)


def _read(data: bytes, filename: str):
    """Every cell as text, blanks as "" (no NaN), headers as written."""
    import pandas as pd

    from . import _upload
    from .file_reader import decode_csv

    if filename.lower().endswith(".csv"):
        frame = pd.read_csv(io.StringIO(decode_csv(data)), dtype=str, keep_default_na=False)
    else:
        frame = pd.read_excel(_upload(data, filename), dtype=str, keep_default_na=False)
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    return frame
