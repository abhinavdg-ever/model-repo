"""Manual Review: a manual QA ground-truth sheet against the system output
(an uploaded sheet or the pipeline's own results), matched on
(PDF ID, Page Number).

Ported from the standalone Streamlit ExcelCompare tool. The comparison and
report modules are its code with package-relative imports; the only change is
the ``Mismatched`` list compare_files adds to a matched row (see compare.py).
The API layer is ``run`` and ``report`` below.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from io import BytesIO
from typing import Any

from . import database_ground, database_system  # noqa: F401  (no pandas at import)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


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


def _summary(summary_df) -> list[dict[str, Any]]:
    return [
        {
            "metric": str(item["Metric"]),
            "count": int(item["Count"]),
            "percentage": float(item["Percentage"]),
        }
        for item in summary_df.to_dict("records")
    ]


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
        "fields": fields,
        "exemptedColumns": list(result["exempted_columns"]),
        "summary": summary,
        "rows": rows,
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
    )
    from .database_system import COLUMNS
    from .record_matcher import normalize_key
    from .result_builder import build_summary

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
    counts = {"groundCount": 0, "systemCount": 0, "correctCount": 0, "errorCount": 0, "missingCount": 0, "extraCount": 0}
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
        counts["groundCount"] += len(ground_part)
        counts["systemCount"] += len(system_part)
        counts["correctCount"] += sum(1 for row in rows if row["Status"] == "CORRECT")
        counts["errorCount"] += sum(1 for row in rows if row["Status"] == "ERROR")
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

    summary = build_summary(counts["groundCount"], counts["systemCount"], all_rows)
    yield {"type": "done", **counts, "summary": _summary(summary)}


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


def report(ground: tuple[bytes, str], system: tuple[bytes, str]) -> bytes:
    """The same comparison as the tool's Excel report (three sheets)."""
    from .compare import compare_files
    from .result_builder import write_report

    result = compare_files(_upload(*ground), _upload(*system))
    buffer = BytesIO()
    write_report(
        buffer,
        result["result_rows"],
        result["summary_df"],
        result["active_comparison_fields"],
        result["exempted_columns"],
    )
    return buffer.getvalue()
