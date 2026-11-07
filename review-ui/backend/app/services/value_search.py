"""Admins' Search by value: pages whose Prediction (``imaging_final``, the Final values) or
Ground Truth (``page_ground_truth``) has a column at a value.

Every condition names a column (``COLUMNS``), a side (``prediction`` / ``ground_truth``), an
operator (``is``: equal, case and spaces ignored; ``contains``; ``empty``) and a value. All
conditions must hold (AND). A page is a ``page_list`` row; its ground truth is the
``page_ground_truth`` row of its chart and page number. Results are one row per chart, each
with its matching pages and, per page, both sides of every searched column. Columns are
named from the whitelist only; values are always bound parameters.
"""

from __future__ import annotations

from typing import Any

from app.services import backend
from app.services.chart_run_batch import with_run_batch_default
from app.services.db import connection
from app.services.ocr_search import SearchError

MAX_PAGES = 2000
MAX_CONDITIONS = 6
MAX_OPTIONS = 200

SIDES = ("prediction", "ground_truth")
OPERATORS = ("is", "contains", "empty")

_FLAG = "f.blank_junk_flag"
_YES_NO = "CASE WHEN {x} IS TRUE THEN 'Yes' WHEN {x} IS FALSE THEN 'No' END"

# key -> (label, Prediction SQL, Ground Truth SQL). Both are text expressions.
COLUMNS: dict[str, tuple[str, str, str]] = {
    "page_type": ("Page Type", "f.page_type", "g.page_type"),
    "page_subtype": ("Page Subtype", "f.page_subtype", "g.page_subtype"),
    "codability": ("Codability", "f.codability", "g.codeable"),
    "blank_page": ("Blank Page", f"CASE WHEN {_FLAG} = 'blank' THEN 'Yes' WHEN {_FLAG} IS NOT NULL THEN 'No' END",
                   "g.blank_page"),
    "junk_page": ("Junk Page", f"CASE WHEN {_FLAG} = 'junk' THEN 'Yes' WHEN {_FLAG} IS NOT NULL THEN 'No' END",
                  "g.junk_page"),
    "is_duplicate": ("Duplicate", _YES_NO.format(x="f.is_duplicate"), "g.is_duplicate"),
    "encounter_type": ("Encounter Type", "f.encounter_type", "g.encounter_label"),
    "dos_from": ("DOS From", "f.dos_from::text", "g.dos_from"),
    "dos_to": ("DOS To", "f.dos_to::text", "g.dos_to"),
    "member_name": ("Member Name", "f.member_name", "g.member_name"),
    "member_dob": ("Member DOB", "f.member_dob::text", "g.member_dob"),
    "member_id": ("Member ID", "f.member_id", "g.member_id"),
    "printed_or_handwritten": ("Printed / Handwritten", "f.printed_or_handwritten", "g.printed_or_handwritten"),
    "specialty": ("Specialty", "f.specialty", "g.provider_specialty"),
    "provider_name": ("Provider Name", "f.provider_name", "g.rendering_provider"),
    "provider_credentials": ("Provider Credentials", "f.provider_credentials", "g.provider_credentials"),
    "provider_signature": ("Provider Signature", _YES_NO.format(x="f.signature_present"), "g.provider_signature"),
}

_FROM = """
  FROM page_list p
  JOIN chart_list c ON c.id = p.chart_id
  LEFT JOIN imaging_final f ON f.page_id = p.id
  LEFT JOIN page_ground_truth g ON g.chart_name = c.chart_name AND g.page_number = p.page_number
"""


def _not_on_cosmos() -> None:
    """Search by value joins every chart's Final values to page ground truth: on Cosmos that
    is two containers across every partition, so it waits for a search index (501)."""
    if backend.reads_cosmos():
        raise backend.NotOnCosmos("Search by value")


def columns() -> list[dict[str, str]]:
    return [{"key": key, "label": label} for key, (label, _, _) in COLUMNS.items()]


def _expr(column: str, side: str) -> str:
    if column not in COLUMNS:
        raise SearchError(400, f"Unknown column {column!r}")
    if side not in SIDES:
        raise SearchError(400, f"Side must be one of {', '.join(SIDES)}")
    _, prediction, truth = COLUMNS[column]
    return prediction if side == "prediction" else truth


def _norm(expr: str) -> str:
    return f"lower(btrim(regexp_replace(coalesce(({expr})::text, ''), '\\s+', ' ', 'g')))"


def _run_batch(run_id: str | None, batch_id: str | None) -> tuple[str, list[Any]]:
    sql, params = "", []
    if (run_id or "").strip():
        sql += " AND c.run_id = %s"
        params.append(run_id.strip())
    if (batch_id or "").strip():
        sql += " AND c.batch_id = %s"
        params.append(batch_id.strip())
    return sql, params


def options(database_url: str, db_schema: str, column: str, side: str, *,
            run_id: str | None = None, batch_id: str | None = None) -> list[dict[str, Any]]:
    """The column's values on that side, most pages first (for the value picker)."""
    _not_on_cosmos()
    expr = _expr(column, side)
    filters, params = _run_batch(run_id, batch_id)
    sql = (f"SELECT btrim(({expr})::text) AS v, count(*) {_FROM}"
           f" WHERE nullif(btrim(({expr})::text), '') IS NOT NULL{filters}"
           " GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT %s")
    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.execute(sql, [*params, MAX_OPTIONS])
        return [{"value": v, "pages": int(n)} for v, n in cur.fetchall()]


def search_values(database_url: str, db_schema: str, conditions: list[dict[str, Any]], *,
                  run_id: str | None = None, batch_id: str | None = None,
                  max_pages: int = MAX_PAGES) -> dict[str, Any]:
    _not_on_cosmos()
    if not conditions:
        raise SearchError(400, "Add at least one condition")
    if len(conditions) > MAX_CONDITIONS:
        raise SearchError(400, f"At most {MAX_CONDITIONS} conditions")
    where: list[str] = []
    params: list[Any] = []
    shown: list[str] = []
    for cond in conditions:
        column, side = str(cond.get("column") or ""), str(cond.get("side") or "")
        op, value = str(cond.get("op") or "is"), " ".join(str(cond.get("value") or "").split()).lower()
        expr = _expr(column, side)
        if op not in OPERATORS:
            raise SearchError(400, f"Operator must be one of {', '.join(OPERATORS)}")
        if op == "empty":
            where.append(f"{_norm(expr)} = ''")
        elif not value:
            raise SearchError(400, f"Give a value for {COLUMNS[column][0]}")
        elif op == "is":
            where.append(f"{_norm(expr)} = %s")
            params.append(value)
        else:
            where.append(f"strpos({_norm(expr)}, %s) > 0")
            params.append(value)
        if column not in shown:
            shown.append(column)
    filters, filter_params = _run_batch(run_id, batch_id)
    picked = ", ".join(f"({COLUMNS[c][1]})::text, ({COLUMNS[c][2]})::text" for c in shown)
    sql = (f"SELECT c.chart_name, c.run_id, c.batch_id, p.page_number, p.page_name, {picked} {_FROM}"
           f" WHERE {' AND '.join(where)}{filters}"
           " ORDER BY c.run_id NULLS LAST, c.batch_id NULLS LAST, c.chart_name, p.page_number LIMIT %s")
    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.execute(sql, [*params, *filter_params, max_pages + 1])
        rows = cur.fetchall()
        truncated = len(rows) > max_pages
        rows = rows[:max_pages]
        names = sorted({r[0] for r in rows})
        totals: dict[str, int] = {}
        if names:
            cur.execute("SELECT c.chart_name, count(p.id) FROM chart_list c LEFT JOIN page_list p ON p.chart_id = c.id"
                        " WHERE c.chart_name = ANY(%s) GROUP BY c.chart_name", [names])
            totals = {name: int(n) for name, n in cur.fetchall()}

    charts: dict[str, dict[str, Any]] = {}
    for row in rows:
        chart_name, run, batch, page_number, page_name = row[:5]
        values = row[5:]
        chart = charts.get(chart_name)
        if chart is None:
            shown_run, shown_batch = with_run_batch_default(run, batch)
            chart = charts[chart_name] = {"chart": chart_name, "run": shown_run, "batch": shown_batch,
                                          "total_pages": totals.get(chart_name), "pages": []}
        chart["pages"].append({
            "page_number": page_number,
            "page_name": page_name,
            "values": {c: {"prediction": values[2 * i], "ground_truth": values[2 * i + 1]}
                       for i, c in enumerate(shown)},
        })
    batches: dict[tuple[str, str], dict[str, Any]] = {}
    for chart in charts.values():
        b = batches.setdefault((chart["run"], chart["batch"]),
                               {"run": chart["run"], "batch": chart["batch"], "charts": 0, "pages": 0})
        b["charts"] += 1
        b["pages"] += len(chart["pages"])
    return {
        "columns": [{"key": c, "label": COLUMNS[c][0]} for c in shown],
        "charts": list(charts.values()),
        "batches": list(batches.values()),
        "total_charts": len(charts),
        "total_pages": sum(len(c["pages"]) for c in charts.values()),
        "truncated": truncated,
        "max_pages": max_pages,
    }
