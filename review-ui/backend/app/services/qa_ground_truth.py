"""QA ground truth (2026-11-25, user): every QA Ground Truth file loaded in Manual Review is
upserted into ``qa_ground_truth``, one row per (chart_name, page_number), the latest load
winning. History shows which charts have it (to the testing user).

The rows are read with Manual Review's own header rules (a page_ground_truth-format sheet is
converted first, as the comparison reads it), so the columns hold what the comparison compared.
``labels`` keeps the whole row as read. DB_BACKEND=cosmos (2026-11-25): the same rows as "qa-<n>"
documents in the ground_truth container (store/cosmos/review.py); under both, Postgres, then a
copy in Cosmos.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger("review_ui.api")

# The account that sees and edits QA ground truth in review-ui (as the frontend's qaColumn.ts).
QA_USER = "testing-user"

# Manual Review column -> qa_ground_truth column.
COLUMNS = {
    "Extracted Name": "member_name",
    "Extracted DOB": "member_dob",
    "Member ID": "member_id",
    "Type (Printed/HW)": "printed_or_handwritten",
    "DOS From": "dos_from",
    "DOS To": "dos_to",
    "Is Blank or Junk?": "is_blank_or_junk",
    "Duplicate": "is_duplicate",
    "Page Type": "page_type",
    "Page Subtype": "page_subtype",
    "Codable / Non-Codable": "codeable",
    "Provider Credentials": "provider_credentials",
    "Current Sequence": "current_sequence",
    "Actual Sequence": "actual_sequence",
}


def _cell(value: Any) -> Optional[str]:
    """A sheet cell as text; empty / NaN -> None, a whole float without its ".0"."""
    if value is None:
        return None
    if isinstance(value, float):
        if value != value:  # NaN
            return None
        if value.is_integer():
            return str(int(value))
    text = str(value).strip()
    return text or None


def rows_from_sheet(data: bytes, filename: str) -> list[dict[str, Any]]:
    """The sheet's pages as qa_ground_truth rows. A row with no PDF ID or no whole page number is
    skipped; a page given twice keeps its last row."""
    from app.services.excel_compare import _upload
    from app.services.excel_compare.compare import _read_file
    from app.services.excel_compare.record_matcher import normalize_key

    frame = _read_file(_upload(data, filename))
    if "PDF ID" not in frame.columns or "Page Number" not in frame.columns:
        return []
    found: dict[tuple[str, int], dict[str, Any]] = {}
    for record in frame.to_dict("records"):
        chart, page = normalize_key(record.get("PDF ID"), record.get("Page Number"))
        if not chart or not page.isdigit():
            continue
        labels = {str(key): text for key, value in record.items() if (text := _cell(value)) is not None}
        row: dict[str, Any] = {"chart_name": chart, "page_number": int(page), "labels": labels}
        for column, target in COLUMNS.items():
            row[target] = _cell(record.get(column))
        found[(chart, int(page))] = row
    return list(found.values())


_UPSERT = """
INSERT INTO qa_ground_truth (chart_name, page_number, {columns}, labels, source_file, user_id)
VALUES (%(chart_name)s, %(page_number)s, {values}, %(labels)s::jsonb, %(source_file)s, %(user_id)s)
ON CONFLICT (chart_name, page_number) DO UPDATE SET
    {updates}, labels = EXCLUDED.labels, source_file = EXCLUDED.source_file, user_id = EXCLUDED.user_id
""".format(
    columns=", ".join(COLUMNS.values()),
    values=", ".join(f"%({c})s" for c in COLUMNS.values()),
    updates=", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS.values()),
)


def upsert(database_url: str, db_schema: str, rows: list[dict[str, Any]],
           source_file: Optional[str], user_id: Optional[int]) -> int:
    """Upsert ``rows`` (rows_from_sheet); returns how many were written."""
    if not rows:
        return 0
    from app.services import backend

    def to_cosmos(store: Any) -> None:
        for row in rows:
            values = {column: row.get(column) for column in COLUMNS.values()}
            store.save_qa_page(row["chart_name"], row["page_number"], values, row["labels"],
                               source_file, user_id, replace=True)

    if backend.reads_cosmos():
        to_cosmos(backend.cosmos())
        return len(rows)
    from app.services.db import connection

    params = [
        {**row, "labels": json.dumps(row["labels"]), "source_file": source_file, "user_id": user_id}
        for row in rows
    ]
    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.executemany(_UPSERT, params)
    backend.mirror("QA ground truth", to_cosmos)
    return len(params)


def store_sheet(database_url: str, db_schema: str, data: bytes, filename: str,
                source_file: Optional[str], user_id: Optional[int]) -> int:
    """Read and upsert one QA sheet. Never raises: a failure is logged and nothing is stored, so
    the Manual Review itself goes on."""
    try:
        return upsert(database_url, db_schema, rows_from_sheet(data, filename), source_file, user_id)
    except Exception as exc:  # noqa: BLE001 - storing QA must not stop the comparison
        logger.warning("qa_ground_truth upsert failed: %s", exc)
        return 0


def charts_with_qa(database_url: str, db_schema: str) -> set[str]:
    """Chart names with any qa_ground_truth row; empty when the table is missing or unreadable."""
    from app.services import backend

    if backend.reads_cosmos():
        try:
            return backend.cosmos().charts_with_qa()
        except Exception as exc:  # noqa: BLE001 - History shows No rather than failing
            logger.info("QA ground truth not read from Cosmos: %s", exc)
            return set()
    from app.services.db import connection

    try:
        with connection(database_url, db_schema) as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT chart_name FROM qa_ground_truth")
            return {str(name) for (name,) in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001 - History shows No rather than failing
        logger.info("qa_ground_truth not read: %s", exc)
        return set()


def chart_pages(database_url: str, db_schema: str, chart_name: str) -> dict[str, dict[str, Any]]:
    """One chart's QA ground truth, page number (as text) -> {column: value, source_file,
    updated_at}, for the chart view's QA Ground Truth column (2026-11-25, user). Empty when the
    chart has none or the table is missing."""
    from app.services import backend

    columns = list(COLUMNS.values())
    if backend.reads_cosmos():
        try:
            docs = backend.cosmos().qa_pages(chart_name)
        except Exception as exc:  # noqa: BLE001 - no QA column rather than an error
            logger.info("QA ground truth not read from Cosmos for a chart: %s", exc)
            return {}
        return {str(doc["page_number"]): _entry(doc.get, doc.get("source_file"), doc.get("updated_at"),
                                                doc.get("labels"))
                for doc in docs}
    from app.services.db import connection

    try:
        with connection(database_url, db_schema) as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT page_number, {', '.join(columns)}, source_file, updated_at, labels "
                "FROM qa_ground_truth WHERE chart_name = %s",
                (chart_name,),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - no QA column rather than an error
        logger.info("qa_ground_truth not read for a chart: %s", exc)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for page_number, *values, source_file, updated_at, labels in rows:
        found = dict(zip(columns, values))
        out[str(page_number)] = _entry(found.get, source_file, updated_at, labels)
    return out


def _entry(get: Any, source_file: Any, updated_at: Any, labels: Any = None) -> dict[str, Any]:
    """A page's QA values (non-empty only; the chart-view fields by their id), its source file
    and when it was last written."""
    entry = {**_fields(labels),
             **{column: get(column) for column in COLUMNS.values() if get(column) not in (None, "")}}
    entry["source_file"] = source_file
    entry["updated_at"] = updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at
    return entry


# 2026-11-25 (user: the QA column has every Ground Truth field): a chart-view field with no
# qa_ground_truth column (orientation, quality, encounter, provider_name, ...) is kept in labels
# under "cv:<field id>" (the chart view's row id); "" clears it.
FIELD_PREFIX = "cv:"
_FIELD_ID = re.compile(r"^[a-z][a-z_]{0,39}$")


def _fields(labels: Any) -> dict[str, Any]:
    """The chart-view fields kept in ``labels`` ("cv:<id>"), non-empty only."""
    if isinstance(labels, str):
        try:
            labels = json.loads(labels)
        except ValueError:
            return {}
    if not isinstance(labels, dict):
        return {}
    return {k[len(FIELD_PREFIX):]: v for k, v in labels.items()
            if k.startswith(FIELD_PREFIX) and v not in (None, "")}


# The source_file of a page edited in the chart view rather than loaded from a QA file.
CHART_VIEW_SOURCE = "review-ui (chart view)"


def save_page(database_url: str, db_schema: str, chart_name: str, page_number: int,
              values: dict[str, Any], user_id: Optional[int]) -> dict[str, Any]:
    """The testing user's QA entry for one page (2026-11-25, user): the given qa_ground_truth
    columns set (an empty value clears it), the others kept; labels gain the same values under
    their Manual Review names. Returns the page as chart_pages gives it."""
    from app.services import backend

    by_column = {column: field for field, column in COLUMNS.items()}
    clean = {column: (_cell(value) if value is not None else None)
             for column, value in values.items() if column in by_column}
    # Every other chart-view field, by its row id, in labels ("" clears it).
    extra = {FIELD_PREFIX + key: (_cell(value) or "") for key, value in values.items()
             if key not in by_column and _FIELD_ID.match(str(key))}
    if not clean and not extra:
        raise ValueError("no QA ground truth field to save")
    labels = {**{by_column[column]: value for column, value in clean.items() if value is not None}, **extra}

    def to_cosmos(store: Any) -> None:
        store.save_qa_page(chart_name, page_number, clean, labels, CHART_VIEW_SOURCE, user_id, replace=False)

    if backend.reads_cosmos():
        to_cosmos(backend.cosmos())
        return chart_pages(database_url, db_schema, chart_name).get(str(page_number), {})
    from app.services.db import connection

    names = list(clean)
    sql = (
        f"INSERT INTO qa_ground_truth (chart_name, page_number, {''.join(n + ', ' for n in names)}labels, source_file, user_id) "
        f"VALUES (%s, %s, {''.join('%s, ' for _ in names)}%s::jsonb, %s, %s) "
        "ON CONFLICT (chart_name, page_number) DO UPDATE SET "
        + "".join(f"{n} = EXCLUDED.{n}, " for n in names)
        + "labels = qa_ground_truth.labels || EXCLUDED.labels, source_file = EXCLUDED.source_file, "
        "user_id = EXCLUDED.user_id"
    )
    with connection(database_url, db_schema) as conn, conn.cursor() as cur:
        cur.execute(sql, (chart_name, page_number, *[clean[n] for n in names], json.dumps(labels),
                          CHART_VIEW_SOURCE, user_id))
    backend.mirror("QA ground truth", to_cosmos)
    return chart_pages(database_url, db_schema, chart_name).get(str(page_number), {})


def all_pages(database_url: str, db_schema: str) -> dict[str, dict[str, dict[str, Any]]]:
    """Every chart's QA ground truth, chart -> page number -> entry (as chart_pages), for the
    Accuracy view's QA scoring (2026-11-25, user). Empty when there is none or no store."""
    from app.services import backend

    columns = list(COLUMNS.values())
    out: dict[str, dict[str, dict[str, Any]]] = {}
    try:
        if backend.reads_cosmos():
            for doc in backend.cosmos().all_qa_pages():
                out.setdefault(str(doc["chart_name"]), {})[str(doc["page_number"])] = _entry(
                    doc.get, doc.get("source_file"), doc.get("updated_at"), doc.get("labels"))
            return out
        from app.services.db import connection

        with connection(database_url, db_schema) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT chart_name, page_number, {', '.join(columns)}, source_file, updated_at, labels "
                        "FROM qa_ground_truth")
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 - no QA scoring rather than an error
        logger.info("qa_ground_truth not read: %s", exc)
        return {}
    for chart, page_number, *values, source_file, updated_at, labels in rows:
        found = dict(zip(columns, values))
        out.setdefault(str(chart), {})[str(page_number)] = _entry(found.get, source_file, updated_at, labels)
    return out

