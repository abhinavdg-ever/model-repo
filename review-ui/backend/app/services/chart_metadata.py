"""Each chart's Source, Measure and Retrieval (RR Method), for the list filters.

From ``manifest_member_list`` (source_name, measure, rr_method) when a database is
configured, with the metadata CSVs (SourceName, Measure, RR Method) filling what it lacks.
A chart with several manifest rows takes its first.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from app.services.chart_run_batch import db_lookup_enabled
from app.services.db import connection
from app.services.metadata_csv import _index_metadata

logger = logging.getLogger("review_ui.chart_metadata")

# Field -> the CSV headers it is read from (matched without case, spaces or underscores).
FIELDS: dict[str, tuple[str, ...]] = {
    "source_name": ("SourceName", "source_name"),
    "measure": ("Measure", "MeasureName"),
    "rr_method": ("RRMethod", "rr_method", "RetrievalMethod", "RetrievalName"),
}

ChartMetadata = dict[str, Optional[str]]


def _key(header: str) -> str:
    return re.sub(r"[\s_\-]", "", header or "").casefold()


def fetch_db_chart_metadata(database_url: str, *, db_schema: str = "public") -> dict[str, ChartMetadata]:
    """record_id -> fields from manifest_member_list; {} when the columns are not there yet."""
    if not db_lookup_enabled(database_url, db_schema):
        return {}
    try:
        with connection(database_url, db_schema) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (record_id) record_id, source_name, measure, rr_method
                      FROM manifest_member_list
                     WHERE source_name IS NOT NULL OR measure IS NOT NULL OR rr_method IS NOT NULL
                     ORDER BY record_id, id
                    """
                )
                rows = cur.fetchall()
    except Exception as exc:
        # A database without schema/changes_2026-10-19.sql has no such columns.
        logger.warning("manifest metadata lookup failed: %s", exc)
        return {}
    return {
        str(rid): {
            "source_name": _text(source),
            "measure": _text(measure),
            "rr_method": _text(method),
        }
        for rid, source, measure, method in rows
        if rid
    }


def csv_chart_metadata(metadata_dir: Path | None) -> dict[str, ChartMetadata]:
    """record_id -> fields from the stacked metadata CSVs (first row per record)."""
    if metadata_dir is None or not metadata_dir.is_dir():
        return {}
    out: dict[str, ChartMetadata] = {}
    for rid, rows in _index_metadata(str(metadata_dir.resolve())).items():
        by_key = {_key(k): v for k, v in rows[0].items()}
        values = {
            name: next((_text(by_key.get(_key(h))) for h in headers if _text(by_key.get(_key(h)))), None)
            for name, headers in FIELDS.items()
        }
        if any(values.values()):
            out[rid] = values
    return out


def chart_metadata_map(
    database_url: str | None,
    *,
    db_schema: str = "public",
    metadata_dir: Path | None = None,
) -> dict[str, ChartMetadata]:
    """The database wins per field; the CSVs fill gaps."""
    merged = csv_chart_metadata(metadata_dir)
    if database_url:
        for rid, values in fetch_db_chart_metadata(database_url, db_schema=db_schema).items():
            row = merged.setdefault(rid, {name: None for name in FIELDS})
            for name, value in values.items():
                if value:
                    row[name] = value
    return merged


def _text(value: object) -> Optional[str]:
    text = "" if value is None else str(value).strip()
    return text or None
