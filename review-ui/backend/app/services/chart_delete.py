"""Delete a chart's pipeline results (an admin's Delete in review-ui).

Removes the chart_list row; every result table hangs off it with ON DELETE CASCADE
(pages, OCR, blank/junk, member, DOS, page type, continuity, final values, jobs).
Kept on purpose: page_ground_truth (keyed by chart name), manifest_member_list (the
client roster) and the files on disk (review-ui mounts the workspace read-only), so a
re-run of the chart rebuilds it. The deletion is recorded in chart_deletion.
"""

from __future__ import annotations

from typing import Optional

from app.services.db import connection


class ChartNotFound(LookupError):
    pass


def delete_chart(
    database_url: str,
    chart_name: str,
    *,
    db_schema: str = "public",
    user_id: Optional[int] = None,
) -> dict:
    """Delete one chart; returns what was removed. Raises ChartNotFound."""
    with connection(database_url, db_schema) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM chart_list WHERE chart_name = %s
                RETURNING id, chart_name, page_count, run_id, batch_id
                """,
                (chart_name,),
            )
            row = cur.fetchone()
            if row is None:
                raise ChartNotFound(chart_name)
            chart_id, name, page_count, run_id, batch_id = row
            cur.execute(
                """
                INSERT INTO chart_deletion (chart_name, page_count, run_id, batch_id, user_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (name, page_count, run_id, batch_id, user_id),
            )
    return {
        "chart_id": int(chart_id),
        "chart_name": str(name),
        "page_count": page_count,
        "run_id": run_id,
        "batch_id": batch_id,
        "kept": ["ground truth", "manifest", "files on disk"],
    }
