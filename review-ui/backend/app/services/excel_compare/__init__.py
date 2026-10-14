"""Excel Compare: a manual QA ground-truth sheet against a system-generated
sheet, matched on (PDF ID, Page Number).

Ported from the standalone Streamlit ExcelCompare tool. The comparison and
report modules are its code with package-relative imports; the only change is
the ``Mismatched`` list compare_files adds to a matched row (see compare.py).
The API layer is ``run`` and ``report`` below.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from . import database_ground  # noqa: F401  (no pandas at import)

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


def run(ground: tuple[bytes, str], system: tuple[bytes, str]) -> dict[str, Any]:
    """The comparison as JSON for the review-ui screen."""
    # pandas is imported here, not at app start, so the rest of review-ui runs without it.
    from .compare import compare_files

    result = compare_files(_upload(*ground), _upload(*system))
    fields = list(result["active_comparison_fields"])
    rows = [
        {
            "pdfId": _text(row.get("PDF ID")),
            "pageNumber": _text(row.get("Page Number")),
            "values": {field: _text(row.get(field, "")) for field in fields},
            "mismatched": list(row.get("Mismatched", [])),
            "status": row["Status"],
            "reason": row["Reason"],
        }
        for row in result["result_rows"]
    ]
    summary = [
        {
            "metric": str(item["Metric"]),
            "count": int(item["Count"]),
            "percentage": float(item["Percentage"]),
        }
        for item in result["summary_df"].to_dict("records")
    ]
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


def columns() -> dict[str, Any]:
    """What a System Generated file needs, from the tool's own column lists."""
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
