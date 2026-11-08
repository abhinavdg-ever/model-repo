"""Admins' Search by value: pages whose Prediction (``imaging_final``, the Final values) or
Ground Truth (``page_ground_truth``) has a column at a value.

Every condition names a column (``COLUMNS``), a side (``prediction`` / ``ground_truth``), an
operator (``is``: equal, case and spaces ignored; ``contains``; ``empty``) and a value. All
conditions must hold (AND). A page is a ``page_list`` row; its ground truth is the
``page_ground_truth`` row of its chart and page number. Results are one row per chart, each
with its matching pages and, per page, both sides of every searched column. Columns are
named from the whitelist only; values are always bound parameters.

On Cosmos (2026-11-08) the same search runs over the documents: every chart doc (run, batch),
every page doc's ``page`` and ``imaging_final`` and every ground-truth page doc, read with
narrow cross-partition queries and matched in Python with the same rules (``_COSMOS``).
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


def _yes_no(value: Any) -> str | None:
    return "Yes" if value is True else "No" if value is False else None


def _flag_is(flag: str):
    return lambda f: None if f.get("blank_junk_flag") is None else ("Yes" if f.get("blank_junk_flag") == flag else "No")


def _field(name: str):
    return lambda f: None if f.get(name) is None else str(f.get(name))


# key -> (Prediction from the page doc's imaging_final, Ground Truth field): as COLUMNS' SQL.
_COSMOS: dict[str, tuple[Any, str]] = {
    "page_type": (_field("page_type"), "page_type"),
    "page_subtype": (_field("page_subtype"), "page_subtype"),
    "codability": (_field("codability"), "codeable"),
    "blank_page": (_flag_is("blank"), "blank_page"),
    "junk_page": (_flag_is("junk"), "junk_page"),
    "is_duplicate": (lambda f: _yes_no(f.get("is_duplicate")), "is_duplicate"),
    "encounter_type": (_field("encounter_type"), "encounter_label"),
    "dos_from": (_field("dos_from"), "dos_from"),
    "dos_to": (_field("dos_to"), "dos_to"),
    "member_name": (_field("member_name"), "member_name"),
    "member_dob": (_field("member_dob"), "member_dob"),
    "member_id": (_field("member_id"), "member_id"),
    "printed_or_handwritten": (_field("printed_or_handwritten"), "printed_or_handwritten"),
    "specialty": (_field("specialty"), "provider_specialty"),
    "provider_name": (_field("provider_name"), "rendering_provider"),
    "provider_credentials": (_field("provider_credentials"), "provider_credentials"),
    "provider_signature": (lambda f: _yes_no(f.get("signature_present")), "provider_signature"),
}


def _cosmos_value(page: dict[str, Any], column: str, side: str) -> str | None:
    _expr(column, side)  # the same column / side checks
    prediction, truth = _COSMOS[column]
    if side == "prediction":
        return prediction(page["final"])
    value = page["truth"].get(truth)
    return None if value is None else str(value)


def _cosmos_norm(value: str | None) -> str:
    return " ".join(str(value or "").split()).lower()


def _cosmos_pages(run_id: str | None, batch_id: str | None) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Every page on Cosmos as {chart, run, batch, page_number, page_name, final, truth}, in the
    Postgres order, and each chart's page count."""
    from app.store.cosmos.review import is_page_doc

    review = backend.cosmos()
    run, batch = (run_id or "").strip(), (batch_id or "").strip()
    charts: dict[str, dict[str, Any]] = {}
    for doc in review.query_all("charts", "SELECT c.chart_name, c.chart FROM c WHERE c.id = 'chart'"):
        chart = doc.get("chart") or {}
        if (run and str(chart.get("run_id") or "") != run) or (batch and str(chart.get("batch_id") or "") != batch):
            continue
        charts[doc["chart_name"]] = chart
    truth: dict[tuple[str, int], dict[str, Any]] = {}
    for doc in review.query_all("ground_truth", "SELECT * FROM c"):
        if is_page_doc(doc) and doc.get("chart_name") in charts:
            truth[(doc["chart_name"], int(doc["page_number"]))] = doc
    pages: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    for doc in review.query_all("pages", "SELECT c.chart_name, c.page, c.imaging_final FROM c WHERE IS_DEFINED(c.page)"):
        name = doc.get("chart_name")
        if name not in charts or not doc.get("page"):
            continue
        number = doc["page"].get("page_number")
        totals[name] = totals.get(name, 0) + 1
        pages.append({
            "chart": name, "run": charts[name].get("run_id"), "batch": charts[name].get("batch_id"),
            "page_number": number, "page_name": doc["page"].get("page_name"),
            "final": doc.get("imaging_final") or {},
            "truth": truth.get((name, int(number))) if number is not None else None,
        })
    for page in pages:
        page["truth"] = page["truth"] or {}
    pages.sort(key=lambda p: (p["run"] is None, str(p["run"] or ""), p["batch"] is None, str(p["batch"] or ""),
                              p["chart"], p["page_number"] if p["page_number"] is not None else 0))
    return pages, totals


def _cosmos_options(column: str, side: str, run_id: str | None, batch_id: str | None) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    pages, _ = _cosmos_pages(run_id, batch_id)
    for page in pages:
        value = (_cosmos_value(page, column, side) or "").strip()
        if value:
            counts[value] = counts.get(value, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_OPTIONS]
    return [{"value": v, "pages": n} for v, n in ranked]


def _cosmos_rows(conditions: list[tuple[str, str, str, str]], shown: list[str], run_id: str | None,
                 batch_id: str | None, limit: int) -> tuple[list[tuple], dict[str, int]]:
    """The matching pages as the Postgres query's rows, and each chart's page count."""
    pages, totals = _cosmos_pages(run_id, batch_id)
    rows: list[tuple] = []
    for page in pages:
        ok = True
        for column, side, op, value in conditions:
            got = _cosmos_norm(_cosmos_value(page, column, side))
            ok = got == "" if op == "empty" else got == value if op == "is" else value in got
            if not ok:
                break
        if not ok:
            continue
        values: list[Any] = []
        for c in shown:
            values += [_cosmos_value(page, c, "prediction"), _cosmos_value(page, c, "ground_truth")]
        rows.append((page["chart"], page["run"], page["batch"], page["page_number"], page["page_name"], *values))
        if len(rows) >= limit:
            break
    return rows, totals


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
    expr = _expr(column, side)
    if backend.reads_cosmos():
        return _cosmos_options(column, side, run_id, batch_id)
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
    if not conditions:
        raise SearchError(400, "Add at least one condition")
    if len(conditions) > MAX_CONDITIONS:
        raise SearchError(400, f"At most {MAX_CONDITIONS} conditions")
    where: list[str] = []
    params: list[Any] = []
    shown: list[str] = []
    checks: list[tuple[str, str, str, str]] = []
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
        checks.append((column, side, op, value))
        if column not in shown:
            shown.append(column)
    if backend.reads_cosmos():
        rows, totals = _cosmos_rows(checks, shown, run_id, batch_id, max_pages + 1)
        truncated = len(rows) > max_pages
        return _assemble(rows[:max_pages], totals, shown, truncated, max_pages)
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
    return _assemble(rows, totals, shown, truncated, max_pages)


def _assemble(rows: list[tuple], totals: dict[str, int], shown: list[str], truncated: bool,
              max_pages: int) -> dict[str, Any]:
    """The response from the matching rows: one entry per chart, its pages, per run / batch."""
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
