"""Derive a chart's overall status from per-page stage rows.

v6 packed "which stage am I in" into ``chart_list.status``, so every new stage
needed a CHECK-constraint migration and a stage that runs twice could not be
represented at all. v7 splits it:

  * ``chart_list.status``        — lifecycle: received / downloading / processing /
                                   completed / failed / needs_review
                                   (``rejected`` is legacy; no longer written —
                                   accept/reject lives on member_verification_summary)
  * ``chart_list.current_stage`` — which stage is the bottleneck, plus
    ``current_pass`` so "blank/junk pass 2" is distinct from pass 1

The rule is unchanged in spirit: **the current stage is the earliest stage in
``pipeline_stage.seq`` order where not every page is completed|skipped.**

Stage order lives in the ``pipeline_stage`` table, not in this module, so
registering page-subtype / encounter / sequencing later needs no code change
here — flip ``is_phase1`` and they join the rollup.
"""
from __future__ import annotations

from typing import Any, Optional

DONE = frozenset({"completed", "skipped"})

CHART_STATUS_VALUES = frozenset(
    {
        "received",
        "downloading",
        "processing",
        "completed",
        "failed",
        "needs_review",
        "rejected",
    }
)


def _stage_rows(conn: Any, chart_id: int) -> list[dict[str, Any]]:
    """Per-stage page rollup for one chart, in execution order."""
    return list(
        conn.execute(
            """
            SELECT stage_name, pass_no, seq, label, pages_total,
                   pending, processing, completed, failed, skipped
              FROM v_chart_stage_progress
             WHERE chart_id = %s AND is_phase1
             ORDER BY seq
            """,
            (chart_id,),
        ).fetchall()
    )


def compute_progress(
    stage_rows: list[dict[str, Any]],
    *,
    pages_total: int,
    previous_status: Optional[str] = None,
    member_final_status: Optional[str] = None,
    member_document_decision: Optional[str] = None,
) -> dict[str, Any]:
    """Overall status + per-stage progress.

    Rules, in order:
      1. No pages  → keep received/downloading, else received.
      2. A page failed in a stage that is not otherwise complete → failed.
      3. Otherwise the earliest incomplete stage → processing, current_stage.
      4. Every stage done → needs_review / completed, from the member
         verification outcome (never ``rejected`` on chart_list).
    """
    prev = (previous_status or "").lower() or None
    stages: list[dict[str, Any]] = []

    if pages_total <= 0:
        status = prev if prev in {"received", "downloading"} else "received"
        return {
            "status": status,
            "current_stage": None,
            "current_pass": None,
            "pages_total": 0,
            "stages": stages,
        }

    earliest: Optional[dict[str, Any]] = None
    any_failed = False

    for row in stage_rows:
        done = int(row["completed"] or 0) + int(row["skipped"] or 0)
        failed = int(row["failed"] or 0)
        complete = done >= pages_total
        stages.append(
            {
                "stage": row["stage_name"],
                "pass_no": int(row["pass_no"]),
                "label": row["label"],
                "pending": int(row["pending"] or 0),
                "processing": int(row["processing"] or 0),
                "completed": int(row["completed"] or 0),
                "failed": failed,
                "skipped": int(row["skipped"] or 0),
                "done": done,
                "total": pages_total,
                "complete": complete,
            }
        )
        if failed and not complete:
            any_failed = True
        if earliest is None and not complete:
            earliest = stages[-1]

    if any_failed:
        status = "failed"
    elif earliest is None:
        # Accept/reject lives on member_verification_summary only — chart
        # lifecycle stays completed (or needs_review). Rejected/Accepted as a
        # chart_list.status comes later; never stamp ``rejected`` here.
        if member_final_status == "needs_review":
            status = "needs_review"
        else:
            status = "completed"
    else:
        status = "processing"

    return {
        "status": status,
        "current_stage": earliest["stage"] if earliest else None,
        "current_pass": earliest["pass_no"] if earliest else None,
        "pages_total": pages_total,
        "stages": stages,
    }


def refresh_chart_status(conn: Any, chart_id: int) -> dict[str, Any]:
    """Recompute and persist chart_list.status / current_stage / current_pass."""
    chart = conn.execute(
        "SELECT id, status FROM chart_list WHERE id = %s", (chart_id,)
    ).fetchone()
    if not chart:
        raise RuntimeError(f"chart_id={chart_id} not found")

    pages_total = conn.execute(
        "SELECT COUNT(*) AS n FROM page_list WHERE chart_id = %s", (chart_id,)
    ).fetchone()["n"]

    summary = conn.execute(
        """
        SELECT final_status, document_decision
          FROM member_verification_summary WHERE chart_id = %s
        """,
        (chart_id,),
    ).fetchone()

    progress = compute_progress(
        _stage_rows(conn, chart_id),
        pages_total=int(pages_total or 0),
        previous_status=chart.get("status"),
        member_final_status=(summary or {}).get("final_status"),
        member_document_decision=(summary or {}).get("document_decision"),
    )

    status = progress["status"]
    if status not in CHART_STATUS_VALUES:
        status = "processing"

    conn.execute(
        """
        UPDATE chart_list
           SET status = %s, current_stage = %s, current_pass = %s
         WHERE id = %s
        """,
        (status, progress["current_stage"], progress["current_pass"], chart_id),
    )
    progress["status"] = status
    progress["chart_id"] = chart_id
    return progress
