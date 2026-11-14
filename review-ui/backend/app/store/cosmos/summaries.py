"""Precomputed per-chart summaries on Cosmos (2026-11-09, user): what the Accuracy View and
Search by value need from a chart, kept as one compressed document per chart so neither has to
read every page document on every request.

Container ``metrics`` (partition key ``/period``), all summaries in the partition ``summary``::

    {id: "summary-<chart>", period: "summary", chart_name, version, fingerprint, built_at,
     run_id, batch_id, page_count, last_verified_at,
     document_gz: the chart's imaging document with its latest ground truth (gzip + base64),
     bulk_gz:     page number -> its latest spreadsheet-load ground truth,
     values_gz:   the Search by value rows: page number, page name, imaging_final, ground truth}

A summary is current while its fingerprint matches: the chart document's _etag, and the ids and
_etags of the chart's ground-truth and manifest documents (one narrow cross-partition read of
each container). Any writer -- core-pipeline, review-ui, db-utilities, the migration -- changes
those, so nobody has to remember to invalidate. A stale or missing summary is rebuilt from the
chart's documents (the same imaging document the chart view shows) and saved. ``VERSION`` goes
up when the summary's shape changes, so old ones are rebuilt.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger("review_ui.cosmos.summaries")

VERSION = 1
CONTAINER = "metrics"
PERIOD = "summary"


def _pack(value: Any) -> str:
    return base64.b64encode(gzip.compress(json.dumps(value, default=str).encode("utf-8"))).decode("ascii")


def _unpack(text: Optional[str]) -> Any:
    if not text:
        return None
    return json.loads(gzip.decompress(base64.b64decode(text)).decode("utf-8"))


def doc_id(chart_name: str) -> str:
    """A Cosmos id may not hold / \\ ? #."""
    return "summary-" + re.sub(r"[/\\?#]", "_", chart_name)


def fingerprints(review: Any, names: Optional[Iterable[str]] = None) -> dict[str, str]:
    """Chart name -> fingerprint of its chart, ground-truth and manifest documents."""
    wanted = set(names) if names is not None else None
    parts: dict[str, list[str]] = {}

    def add(chart: Any, tag: str) -> None:
        if not chart or (wanted is not None and chart not in wanted):
            return
        parts.setdefault(str(chart), []).append(tag)

    for d in review.query_all("charts", "SELECT c.chart_name, c._etag FROM c WHERE c.id = 'chart'"):
        if d.get("id", "chart") == "chart":
            add(d.get("chart_name"), f"chart:{d.get('_etag')}")
    for container in ("ground_truth", "manifest"):
        for d in review.query_all(container, "SELECT c.chart_name, c.id, c._etag FROM c"):
            add(d.get("chart_name"), f"{container}:{d.get('id')}:{d.get('_etag')}")
    return {chart: hashlib.sha1(f"v{VERSION}|".encode() + "|".join(sorted(tags)).encode()).hexdigest()
            for chart, tags in parts.items() if any(t.startswith("chart:") for t in tags)}


def _stored(review: Any) -> dict[str, dict[str, Any]]:
    docs = review.query("metrics", "SELECT * FROM c", PERIOD)
    return {d["chart_name"]: d for d in docs if d.get("period") == PERIOD and d.get("version") == VERSION}


def _bulk_truth(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Page number -> its latest spreadsheet-load ground truth (PageGroundTruth as JSON)."""
    from app.services.ground_truth import _ground_truth_from_row
    from app.store.cosmos.review import entries, truth_row

    out: dict[str, Any] = {}
    for doc in docs:
        loads = [e for e in entries(doc) if e.get("entry_mode") == "bulk_upload"]
        if not loads:
            continue
        last = loads[-1]
        from app.store.cosmos.gt_lists import from_doc

        labelled = {**from_doc(doc), **from_doc(last.get("labels") or {}), "updated_at": last.get("created_at")}
        out[str(int(doc["page_number"]))] = _ground_truth_from_row(truth_row(labelled)).model_dump(mode="json")
    return out


def _value_rows(review: Any, chart_name: str, truth_docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Search by value's rows: every page's imaging_final and ground truth."""
    from app.store.cosmos.gt_lists import from_doc

    truth = {int(d["page_number"]): from_doc({k: v for k, v in d.items() if not k.startswith("_") and k != "entries"})
             for d in truth_docs}
    rows = []
    for doc in review.query("pages", "SELECT c.page, c.imaging_final FROM c", chart_name):
        page = doc.get("page")
        if not page:
            continue
        number = page.get("page_number")
        rows.append({"page_number": number, "page_name": page.get("page_name"),
                     "final": doc.get("imaging_final") or {},
                     "truth": truth.get(int(number)) if number is not None else None})
    rows.sort(key=lambda r: r["page_number"] if r["page_number"] is not None else 0)
    return rows


def build(review: Any, chart_name: str, fingerprint: str) -> dict[str, Any]:
    """Build and save one chart's summary."""
    from app.store.cosmos.review import is_page_doc

    chart_doc = review.read("charts", "chart", chart_name) or {}
    chart = chart_doc.get("chart") or {}
    document = review.repo.get_imaging(chart_name)
    truth_docs = [d for d in review.query("ground_truth", "SELECT * FROM c", chart_name) if is_page_doc(d)]
    moments = [str(d.get("updated_at")) for d in truth_docs if d.get("updated_at")]
    rows = _value_rows(review, chart_name, truth_docs)
    summary = {
        "id": doc_id(chart_name), "period": PERIOD, "chart_name": chart_name, "version": VERSION,
        "fingerprint": fingerprint, "built_at": datetime.now(timezone.utc).isoformat(),
        "run_id": chart.get("run_id"), "batch_id": chart.get("batch_id"), "page_count": len(rows),
        "last_verified_at": max(moments) if moments else None,
        "document_gz": _pack(document.model_dump(mode="json")),
        "bulk_gz": _pack(_bulk_truth(truth_docs)),
        "values_gz": _pack(rows),
    }
    try:
        review.c(CONTAINER).upsert_item(summary)
    except Exception as exc:  # noqa: BLE001 -- still usable for this request
        logger.warning("summary for %s not saved: %s", chart_name, exc)
    return summary


def current(review: Any, names: Optional[Iterable[str]] = None) -> dict[str, dict[str, Any]]:
    """Chart name -> its current summary (stale or missing ones rebuilt), for ``names`` or every
    chart. A chart that fails to build is left out (logged)."""
    prints = fingerprints(review, names)
    try:
        stored = _stored(review)
    except Exception as exc:  # noqa: BLE001 -- no metrics container: build each time
        logger.warning("summaries not readable (%s): building them for this request", exc)
        stored = {}
    out: dict[str, dict[str, Any]] = {}
    rebuilt = 0
    for chart, fingerprint in prints.items():
        summary = stored.get(chart)
        if summary is None or summary.get("fingerprint") != fingerprint:
            try:
                summary = build(review, chart, fingerprint)
                rebuilt += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("summary for %s not built: %s", chart, exc)
                continue
        out[chart] = summary
    if rebuilt:
        logger.info("Cosmos summaries: %d rebuilt, %d current", rebuilt, len(out) - rebuilt)
    return out


def document(summary: dict[str, Any]) -> Any:
    from app.core.schemas import ImagingDocumentResponse

    return ImagingDocumentResponse.model_validate(_unpack(summary.get("document_gz")))


def bulk_truth(summary: dict[str, Any]) -> dict[int, Any]:
    from app.core.schemas import PageGroundTruth

    return {int(k): PageGroundTruth.model_validate(v) for k, v in (_unpack(summary.get("bulk_gz")) or {}).items()}


def value_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    return _unpack(summary.get("values_gz")) or []
