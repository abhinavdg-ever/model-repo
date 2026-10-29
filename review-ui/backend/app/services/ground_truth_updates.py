"""Who last updated each ground-truth tab of a chart, and when (2026-10-28, user).

Page Details and Sequencing read page_ground_truth_entry (a sequence-only save counts for
Sequencing alone); Encounter reads encounter_review; Rejection Rules reads
chart_ground_truth. A tab no one has saved by hand shows the chart's latest spreadsheet
load: "Last Updated By Bulk Upload on X".

``missing`` lists what the review still lacks (2026-10-29, user), warned about before the
chart is marked Reviewed: pages with no Page Details labels, pages with no sequence, no
encounters saved, no member decision.
"""

from __future__ import annotations

from app.core.schemas import ChartGroundTruthUpdates, GroundTruthUpdate
from app.services.chart_review import _has_users, _iso, _who
from app.services.db import connection
from app.services.ground_truth import _chart_name, ensure_entries

# The chart's newest entry of the kind {where} picks: (mode, who, when).
_LATEST_ENTRY = """
SELECT e.entry_mode, {who}, e.created_at
  FROM page_ground_truth_entry e
  JOIN page_ground_truth g ON g.id = e.page_ground_truth_id
 WHERE g.chart_name = %s AND {where}
 ORDER BY e.created_at DESC, e.id DESC
 LIMIT 1
"""

# A save that set more than the page sequence: Page Details.
_PAGE = "e.labels - 'page_sequence' <> '{}'::jsonb"
_SEQUENCE = "e.labels ? 'page_sequence'"
_BULK = "e.entry_mode = 'bulk_upload'"


def _entry(cur, chart: str, who: str, where: str) -> GroundTruthUpdate | None:
    cur.execute(_LATEST_ENTRY.format(who=who, where=where), (chart,))
    row = cur.fetchone()
    if row is None:
        return None
    mode, by, at = row
    if mode == "bulk_upload":
        return GroundTruthUpdate(bulk=True, at=_iso(at))
    return GroundTruthUpdate(by=by, at=_iso(at))


def _table_exists(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
    row = cur.fetchone()
    return bool(row and row[0])


def _chart_row(cur, chart: str, who: str, table: str) -> GroundTruthUpdate | None:
    """The newest save in a chart-keyed table (encounter_review, chart_ground_truth)."""
    if not _table_exists(cur, table):
        return None
    cur.execute(
        f"SELECT {who}, r.updated_at FROM {table} r WHERE r.chart_name = %s "
        "ORDER BY r.updated_at DESC LIMIT 1",
        (chart,),
    )
    row = cur.fetchone()
    return GroundTruthUpdate(by=row[0], at=_iso(row[1])) if row else None


# The chart's pages, and of them those with Page Details labels and with a sequence.
_PAGE_COUNTS = """
SELECT (SELECT count(*) FROM page_list p JOIN chart_list c ON c.id = p.chart_id
         WHERE c.chart_name = %(chart)s),
       count(*) FILTER (WHERE COALESCE(NULLIF(btrim(member_name), ''), NULLIF(btrim(member_dob), ''),
                                       NULLIF(btrim(dos_from), ''), NULLIF(btrim(blank_page), ''),
                                       NULLIF(btrim(junk_page), ''), NULLIF(btrim(encounter_type), ''),
                                       NULLIF(btrim(codeable), '')) IS NOT NULL),
       count(*) FILTER (WHERE NULLIF(btrim(page_sequence), '') IS NOT NULL)
  FROM (SELECT DISTINCT ON (page_number) *
          FROM page_ground_truth WHERE chart_name = %(chart)s
         ORDER BY page_number, updated_at DESC, id DESC) g
"""


def _has_rows(cur, table: str, chart: str, where: str = "TRUE") -> bool:
    if not _table_exists(cur, table):
        return False
    cur.execute(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE chart_name = %s AND {where})", (chart,))
    return bool(cur.fetchone()[0])


def _missing(cur, chart: str) -> list[str]:
    """One line per part of the review not yet filled in."""
    cur.execute(_PAGE_COUNTS, {"chart": chart})
    pages, labelled, sequenced = cur.fetchone()
    out: list[str] = []
    if pages and labelled < pages:
        out.append(f"Page Details: {pages - labelled} of {pages} pages have no ground truth")
    if pages and sequenced < pages:
        out.append(
            "Page Sequencing: not saved" if sequenced == 0
            else f"Page Sequencing: {pages - sequenced} of {pages} pages have no sequence"
        )
    if not _has_rows(cur, "encounter_review", chart):
        out.append("Encounter: no encounters saved")
    if not _has_rows(cur, "chart_ground_truth", chart, "member_decision IS NOT NULL"):
        out.append("Rejection Rules: no Accept / Reject decision")
    return out


def ground_truth_updates(
    database_url: str, folder_id: str, db_schema: str = "public"
) -> ChartGroundTruthUpdates:
    chart = _chart_name(folder_id)
    with connection(database_url, db_schema) as conn:
        ensure_entries(conn, database_url, db_schema)
        with conn.cursor() as cur:
            who_e = _who(_has_users(cur), "e")
            who_r = _who(_has_users(cur), "r")
            bulk = _entry(cur, chart, who_e, _BULK) or GroundTruthUpdate()
            return ChartGroundTruthUpdates(
                page=_entry(cur, chart, who_e, _PAGE) or bulk,
                sequencing=_entry(cur, chart, who_e, _SEQUENCE) or bulk,
                encounter=_chart_row(cur, chart, who_r, "encounter_review") or bulk,
                member=_chart_row(cur, chart, who_r, "chart_ground_truth") or bulk,
                missing=_missing(cur, chart),
            )
