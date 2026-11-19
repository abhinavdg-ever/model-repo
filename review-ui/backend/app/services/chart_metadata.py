"""Each chart's Scope (Project Scope), Source, Measure and Retrieval (RR Method), for the
list filters.

From ``manifest_member_list`` (project_scope, source_name, measure, rr_method) when a
database is configured, with the metadata CSVs (Project Scope, SourceName, Measure,
RR Method) filling what it lacks.
A chart with several manifest rows takes its first.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from app.services import backend
from app.services.chart_run_batch import db_lookup_enabled
from app.services.db import connection
from app.services.metadata_csv import _index_metadata

logger = logging.getLogger("review_ui.chart_metadata")

# Field -> the CSV headers it is read from (matched without case, spaces or underscores).
FIELDS: dict[str, tuple[str, ...]] = {
    "project_scope": ("ProjectScope", "project_scope", "Scope"),
    "source_name": ("SourceName", "source_name"),
    "measure": ("Measure", "MeasureName"),
    "rr_method": ("RRMethod", "rr_method", "RetrievalMethod", "RetrievalName"),
}

ChartMetadata = dict[str, Optional[str]]


def _key(header: str) -> str:
    return re.sub(r"[\s_\-]", "", header or "").casefold()


def fetch_db_chart_metadata(database_url: str, *, db_schema: str = "public") -> dict[str, ChartMetadata]:
    """record_id -> fields from manifest_member_list; {} when the columns are not there yet."""
    if backend.reads_cosmos():
        return cosmos_chart_metadata()
    if not db_lookup_enabled(database_url, db_schema):
        return {}
    try:
        with connection(database_url, db_schema) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT DISTINCT ON (record_id) record_id, j ->> 'source_name',
                           j ->> 'measure', j ->> 'rr_method', j ->> 'project_scope'
                      FROM (SELECT id, record_id, to_jsonb(m) AS j FROM manifest_member_list m) rows
                     -- Read through to_jsonb so a database without a column (before
                     -- schema/migrations/2026-10-19.sql, or project_scope before 2026-10-21)
                     -- answers NULL for it instead of failing the whole lookup.
                     WHERE COALESCE(j ->> 'source_name', j ->> 'measure', j ->> 'rr_method',
                                    j ->> 'project_scope') IS NOT NULL
                     ORDER BY record_id, id
                    """
                )
                rows = cur.fetchall()
    except Exception as exc:
        # No manifest_member_list at all, or the database is unreachable.
        logger.warning("manifest metadata lookup failed: %s", exc)
        return {}
    return {
        str(rid): {
            "source_name": _text(source),
            "measure": _text(measure),
            "rr_method": _text(method),
            "project_scope": _text(scope),
        }
        for rid, source, measure, method, scope in rows
        if rid
    }


def cosmos_chart_metadata() -> dict[str, ChartMetadata]:
    """record_id -> fields from the manifest documents: the record's lowest-id member with
    any of them, as the Postgres DISTINCT ON (record_id) ... ORDER BY record_id, id. One
    cross-partition query (four fields per manifest doc)."""
    try:
        rows = backend.cosmos().manifest_metadata()
    except Exception as exc:
        logger.warning("manifest metadata lookup failed: %s", exc)
        return {}
    out: dict[str, ChartMetadata] = {}
    for rid, _id, source, measure, method, scope in sorted(rows, key=lambda r: (str(r[0]), r[1])):
        if not rid or str(rid) in out or all(v is None for v in (source, measure, method, scope)):
            continue
        out[str(rid)] = {
            "source_name": _text(source),
            "measure": _text(measure),
            "rr_method": _text(method),
            "project_scope": _text(scope),
        }
    return out


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
    if database_url or backend.reads_cosmos():
        for rid, values in fetch_db_chart_metadata(database_url, db_schema=db_schema).items():
            row = merged.setdefault(rid, {name: None for name in FIELDS})
            for name, value in values.items():
                if value:
                    row[name] = value
    return merged


def _text(value: object) -> Optional[str]:
    text = "" if value is None else str(value).strip()
    return text or None


_CHART_FACTS_SQL = """
SELECT to_jsonb(c) ->> 'chart_cluster',
       (SELECT to_jsonb(m) FROM manifest_member_list m
         WHERE m.record_id = c.chart_name
           AND COALESCE(to_jsonb(m) ->> 'source_name', to_jsonb(m) ->> 'measure',
                        to_jsonb(m) ->> 'rr_method', to_jsonb(m) ->> 'project_scope') IS NOT NULL
         ORDER BY m.id LIMIT 1)
  FROM chart_list c
 WHERE c.chart_name = %s
"""


def chart_facts(database_url: str | None, chart: str, *, db_schema: str = "public",
                metadata_dir: Path | None = None) -> dict[str, Optional[str]]:
    """One chart's cluster (chart_list.chart_cluster) and Scope / Source / Measure / Retrieval
    (its first manifest_member_list row with any, the metadata CSVs filling gaps), for the
    chart's Analysis card (2026-11-19, user). Missing values are left out."""
    out: dict[str, Optional[str]] = {}
    if backend.reads_cosmos():
        # The cluster comes with the chart's own document (CosmosFolderRepository.get_folder).
        out.update(cosmos_chart_metadata().get(chart) or {})
    elif database_url and db_lookup_enabled(database_url, db_schema):
        try:
            with connection(database_url, db_schema) as conn, conn.cursor() as cur:
                cur.execute(_CHART_FACTS_SQL, (chart,))
                found = cur.fetchone()
        except Exception as exc:
            logger.warning("chart facts lookup failed for a chart: %s", type(exc).__name__)
            found = None
        if found:
            cluster, manifest = found
            out["chart_cluster"] = _text(cluster)
            manifest = manifest or {}
            for name in FIELDS:
                out[name] = _text(manifest.get(name))
    for name, value in (csv_chart_metadata(metadata_dir).get(chart) or {}).items():
        if value and not out.get(name):
            out[name] = value
    return {k: v for k, v in out.items() if v}
