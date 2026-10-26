"""Manual Review: a manual QA ground-truth sheet against the system output
(an uploaded sheet or the pipeline's own results), matched on
(PDF ID, Page Number).

Ported from the standalone Streamlit ExcelCompare tool. The comparison and
report modules are its code with package-relative imports, plus review-ui's
own reading rules (ground truth as the base, presence labels, credentials,
dates). A matched row also carries ``Mismatched`` and ``Evidence`` (see
compare.py), and ``compare.summarize`` totals a run for both the one-pass
and the streaming compare. The API layer is ``run``, ``stream``, ``report``
and ``summary_pdf`` below.

A finished comparison is kept under a ``reportId`` (``run``'s result, the
stream's ``done`` event): the downloads build from it, so they match the
screen and skip reading and comparing everything again.
"""

from __future__ import annotations

import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from io import BytesIO
from typing import Any

from . import database_algo, database_ground, database_system  # noqa: F401  (no pandas at import)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024

# Finished comparisons kept for their downloads, newest last. review-ui runs
# one process; a download whose result is gone compares again.
KEEP_RESULTS = 4
_results: OrderedDict[str, dict] = OrderedDict()
_results_lock = threading.Lock()


def _remember(result: dict) -> str:
    report_id = secrets.token_urlsafe(16)
    with _results_lock:
        _results[report_id] = result
        while len(_results) > KEEP_RESULTS:
            _results.popitem(last=False)
    return report_id


def remembered(report_id: str | None) -> dict | None:
    """The comparison a ``reportId`` names, or None once it has been dropped."""
    if not report_id:
        return None
    with _results_lock:
        return _results.get(report_id)


def _upload(data: bytes, filename: str) -> BytesIO:
    """In-memory file the ported reader accepts: it picks CSV or Excel from ``.name``."""
    buffer = BytesIO(data)
    buffer.name = filename
    return buffer


def _text(value: Any) -> str:
    """PDF ID / Page Number come back as a mix of numbers and strings."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _row(row: dict, fields: list[str]) -> dict[str, Any]:
    return {
        "pdfId": _text(row.get("PDF ID")),
        "pageNumber": _text(row.get("Page Number")),
        "values": {field: _text(row.get(field, "")) for field in fields},
        "mismatched": list(row.get("Mismatched", [])),
        "status": row["Status"],
        "reason": row["Reason"],
    }


def _percentage(value: Any) -> float | None:
    """The summary's "N/A" (nothing compared) and "-" (chart counts) read as None."""
    return float(value) if isinstance(value, (int, float)) else None


def _summary(summary_df) -> list[dict[str, Any]]:
    return [
        {
            "metric": str(item["Metric"]),
            "count": int(item["Count"]),
            "percentage": _percentage(item["Percentage"]),
        }
        for item in summary_df.to_dict("records")
    ]


def _totals(result: dict) -> dict[str, Any]:
    """compare.summarize's totals the screen shows beyond the status counts."""
    return {
        "matchedCount": int(result["matched_count"]),
        "unvalidatedCount": int(result["unvalidated_count"]),
        "excludedCount": int(result["excluded_count"]),
        "evaluatedCount": int(result["evaluated_count"]),
        "evaluatedAccuracyRate": (
            None if result["evaluated_accuracy_rate"] is None else float(result["evaluated_accuracy_rate"])
        ),
        "groundCharts": int(result["total_unique_ground_charts"]),
        "systemCharts": int(result["total_unique_system_charts"]),
        "chartsCompared": int(result["total_unique_charts_compared"]),
        "fieldErrors": [
            {"field": e["field"], "count": int(e["error_count"]), "percentage": float(e["error_percentage"])}
            for e in result["field_error_summary"]
        ],
    }


def run(ground: tuple[bytes, str], system: tuple[bytes, str]) -> dict[str, Any]:
    """The comparison as JSON for the review-ui screen."""
    # pandas is imported here, not at app start, so the rest of review-ui runs without it.
    from .compare import compare_files

    result = compare_files(_upload(*ground), _upload(*system))
    fields = list(result["active_comparison_fields"])
    rows = [_row(row, fields) for row in result["result_rows"]]
    summary = _summary(result["summary_df"])
    return {
        "groundCount": int(result["ground_count"]),
        "systemCount": int(result["system_count"]),
        "correctCount": int(result["correct_count"]),
        "errorCount": int(result["error_count"]),
        "missingCount": int(result["missing_count"]),
        "extraCount": int(result["extra_count"]),
        **_totals(result),
        "fields": fields,
        "exemptedColumns": list(result["exempted_columns"]),
        "summary": summary,
        "rows": rows,
        "reportId": _remember(result),
    }


def stream(
    ground: tuple[bytes, str],
    system: tuple[bytes, str] | None = None,
    load_system: Callable[[str], bytes | None] | None = None,
    last_run: Callable[[str], str | None] | None = None,
) -> Iterator[dict[str, Any]]:
    """The comparison one PDF ID at a time, for the screen to fill in as it goes.
    Ground truth is the base: only its charts and pages are compared.

    The Algo side is an uploaded sheet (``system``) or, chart by chart,
    ``load_system(pdf_id)``: the pipeline output as CSV bytes, None when the
    chart has none. ``last_run(pdf_id)`` is the chart's last pipeline run. Events: ``start`` (charts, fields, exemptions), one
    ``chart`` per PDF ID with its rows, then ``done`` with the summary. A
    failure part-way is an ``error`` event; what was sent stays valid.
    """
    import pandas as pd

    from .compare import (
        _read_file,
        _resolve_comparison_fields,
        _validate_columns,
        compare_frames,
        restrict_to_ground,
        summarize,
    )
    from .database_system import COLUMNS
    from .header_normalizer import unrecognized_columns
    from .record_matcher import normalize_key, unique_pdf_ids

    ground_df = _read_file(_upload(*ground))
    _validate_columns(ground_df, "Ground Truth")
    if system is not None:
        system_df = _read_file(_upload(*system))
        _validate_columns(system_df, "Algo Generated")
    else:
        system_df = pd.DataFrame(columns=COLUMNS)
    fields, exempted = _resolve_comparison_fields(ground_df, system_df)

    def key(frame) -> "pd.Series":
        return frame["PDF ID"].map(lambda value: normalize_key(value, 0)[0])

    # Ground truth is the base: its charts, in its order (see restrict_to_ground).
    ground_keys = key(ground_df)
    system_keys = key(system_df) if system is not None else None
    pdf_ids = [pdf_id for pdf_id in dict.fromkeys(ground_keys.tolist()) if pdf_id]

    yield {
        "type": "start",
        "charts": len(pdf_ids),
        "fields": fields,
        "exemptedColumns": exempted,
    }

    all_rows: list[dict] = []
    system_charts: set[str] = set()
    counts = {
        "groundCount": 0, "systemCount": 0, "correctCount": 0, "errorCount": 0,
        "missingCount": 0, "extraCount": 0, "unvalidatedCount": 0, "excludedCount": 0,
    }
    for pdf_id in pdf_ids:
        try:
            ground_part = ground_df[ground_keys == pdf_id]
            if system_keys is not None:
                system_part = system_df[system_keys == pdf_id]
            else:
                data = load_system(pdf_id) if load_system else None
                system_part = _read_file(_upload(data, "algo.csv")) if data else pd.DataFrame(columns=COLUMNS)
            system_part = restrict_to_ground(ground_part, system_part)
            rows, missing, extra = compare_frames(ground_part, system_part, fields)
        except Exception as exc:  # noqa: BLE001 - reported to the screen, not raised mid-stream
            yield {"type": "error", "pdfId": pdf_id, "detail": str(exc)}
            return
        all_rows.extend(rows)
        system_charts |= unique_pdf_ids(system_part)
        counts["groundCount"] += len(ground_part)
        counts["systemCount"] += len(system_part)
        counts["correctCount"] += sum(1 for row in rows if row["Status"] == "CORRECT")
        counts["errorCount"] += sum(1 for row in rows if row["Status"] == "ERROR")
        counts["unvalidatedCount"] += sum(1 for row in rows if row["Status"] == "UNVALIDATED")
        counts["excludedCount"] += sum(1 for row in rows if row["Status"] == "EXCLUDED")
        counts["missingCount"] += missing
        counts["extraCount"] += extra
        try:
            ran = last_run(pdf_id) if last_run else None
        except Exception:  # noqa: BLE001 - a missing timestamp must not stop the comparison
            ran = None
        yield {
            "type": "chart",
            "pdfId": pdf_id,
            "lastRunAt": ran,
            "rows": [_row(row, fields) for row in rows],
            **counts,
        }

    result = summarize(
        all_rows, counts["groundCount"], counts["systemCount"], fields,
        set(pdf_ids), system_charts,
    )
    report_id = _remember({
        **result,
        "result_rows": all_rows,
        "active_comparison_fields": fields,
        "exempted_columns": exempted,
        "unrecognized_columns": {
            "ground": unrecognized_columns(ground_df),
            # The pipeline output's own columns are all read.
            "system": unrecognized_columns(system_df) if system is not None else [],
        },
    })
    yield {
        "type": "done", **counts, **_totals(result),
        "summary": _summary(result["summary_df"]),
        "reportId": report_id,
    }


def columns() -> dict[str, Any]:
    """What a Algo Generated file needs, from the tool's own column lists."""
    from .field_comparator import ACTIVE_COMPARISON_FIELDS, ALWAYS_EXEMPT_FIELDS
    from .header_normalizer import COLUMN_ALIASES
    from .record_matcher import KEY_COLUMNS

    def aliases(canonical: str) -> list[str]:
        return [alias for alias, target in COLUMN_ALIASES.items() if target == canonical]

    return {
        "required": [{"column": c, "aliases": aliases(c)} for c in KEY_COLUMNS],
        "compared": [{"column": c, "aliases": aliases(c)} for c in ACTIVE_COMPARISON_FIELDS],
        "exempt": list(ALWAYS_EXEMPT_FIELDS),
    }


def comparison(ground: tuple[bytes, str], system: tuple[bytes, str]) -> dict:
    """compare_files over two sheets: the result the downloads are built from.
    (Not ``compare``: importing the ``.compare`` module rebinds that name.)"""
    from .compare import compare_files

    return compare_files(_upload(*ground), _upload(*system))


def report(result: dict) -> bytes:
    """A comparison (``comparison`` or ``remembered``) as the tool's Excel report (three sheets)."""
    from .result_builder import write_report

    buffer = BytesIO()
    write_report(
        buffer,
        result["result_rows"],
        result["summary_df"],
        result["active_comparison_fields"],
        result["exempted_columns"],
    )
    return buffer.getvalue()


def summary_pdf(
    result: dict,
    ground_label: str = "Ground Truth",
    system_label: str = "Algo Generated",
) -> bytes:
    """A comparison (``comparison`` or ``remembered``) as the printable Master Summary (PDF)."""
    from .master_summary import build_master_summary_pdf

    return build_master_summary_pdf(result, ground_label=ground_label, system_label=system_label)
