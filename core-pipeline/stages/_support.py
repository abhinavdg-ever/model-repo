"""Shared plumbing for pipeline stages.

Every stage needs the same five things: open a job row, work out which pages
still need doing (resume), mark pages processing/completed/failed/skipped,
write a CSV, close the job. Doing that once here keeps the stage modules about
their actual work.
"""
from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Sequence

from db import (
    connect,
    create_job,
    get_chart,
    list_pages,
    pages_needing_stage,
    set_page_stage,
    set_pages_stage,
    update_job,
)

logger = logging.getLogger(__name__)

# A page classified blank, junk or duplicate is excluded from downstream work.
BJ_EXCLUDE = frozenset({"blank", "junk", "duplicate"})


@dataclass
class StageContext:
    """Everything a stage needs to know before it starts working."""

    chart_id: int
    chart_name: str
    stage_name: str
    pass_no: int
    job_id: int
    pages: list[dict[str, Any]]
    todo: set[int]
    force: bool
    errors: list[str] = field(default_factory=list)
    done: int = 0
    skipped: int = 0

    @property
    def pages_todo(self) -> list[dict[str, Any]]:
        return [p for p in self.pages if p["id"] in self.todo]

    def page_map(self) -> dict[int, dict[str, Any]]:
        return {p["id"]: p for p in self.pages}


@contextlib.contextmanager
def stage_run(
    chart_id: int,
    stage_name: str,
    *,
    pass_no: int = 1,
    force: bool = False,
) -> Iterator[StageContext]:
    """Open a stage: create the job row, load pages, compute the resume set.

    On a clean exit the job is closed as completed (or failed when the stage
    collected per-page errors). On an exception the job is marked failed and
    the exception propagates.
    """
    with connect() as conn:
        chart = get_chart(conn, chart_id)
        if not chart:
            raise RuntimeError(f"chart_id={chart_id} not found")
        pages = list_pages(conn, chart_id)
        todo = pages_needing_stage(conn, chart_id, stage_name, pass_no, force=force)
        job_id = create_job(
            conn,
            chart_id=chart_id,
            stage_name=stage_name,
            pass_no=pass_no,
            status="running",
            pages_total=len(pages),
        )
        update_job(conn, job_id, started=True)

    ctx = StageContext(
        chart_id=chart_id,
        chart_name=chart["chart_name"],
        stage_name=stage_name,
        pass_no=pass_no,
        job_id=job_id,
        pages=pages,
        todo=todo,
        force=force,
    )
    from logging_setup import (
        reset_current_chart,
        reset_current_stage,
        set_current_chart,
        set_current_stage,
    )

    chart_token = set_current_chart(ctx.chart_name)
    stage_token = set_current_stage(stage_label(stage_name, pass_no))
    logger.info(
        "starting — %s of %s page(s)%s",
        len(todo), len(pages), " (forced)" if force else "",
    )

    try:
        yield ctx
    except Exception as exc:
        with connect() as conn:
            update_job(
                conn, job_id, status="failed", error_message=str(exc), completed=True
            )
        raise
    else:
        with connect() as conn:
            update_job(
                conn,
                job_id,
                status="failed" if ctx.errors else "completed",
                error_message="; ".join(ctx.errors[:20]) if ctx.errors else None,
                completed=True,
                pages_done=ctx.done,
                pages_failed=len(ctx.errors),
                pages_skipped=ctx.skipped,
            )
    finally:
        reset_current_stage(stage_token)
        reset_current_chart(chart_token)


def mark_skipped(
    conn: Any,
    ctx: StageContext,
    page_ids: Sequence[int],
    reason: str,
) -> None:
    """Record pages this stage deliberately does not process."""
    ids = [pid for pid in page_ids if pid in ctx.todo]
    if not ids:
        return
    set_pages_stage(
        conn,
        chart_id=ctx.chart_id,
        page_ids=ids,
        stage_name=ctx.stage_name,
        pass_no=ctx.pass_no,
        status="skipped",
        skip_reason=reason,
    )
    ctx.skipped += len(ids)
    ctx.todo.difference_update(ids)
    for page_id in ids:
        _page_status(ctx, page_id, f"skipped — {reason}")


# Short display names for log lines. pipeline_stage.label is the long form for
# the UI; these are the one-per-page form, kept here so every stage reports
# identically instead of each inventing its own wording.
STAGE_LABELS = {
    "ocr_prelim": "Prelim OCR",
    "ocr_quality": "Rotation + Handwriting",
    "blank_junk": "Blank/Junk",
    "ocr_final1": "Final OCR 1",
    "ocr_final2": "Final OCR 2",
    "section_headers": "Section Headers",
    "kv_extract": "Key/Value Extraction",
    "member_verify": "Member Verify",
    "dos_extract": "Date of Service",
    "page_subtype": "Codeable / Non Codeable",
    "encounter_type": "Encounter Type",
    "page_sequencing": "Page Sequencing",
    "download_blob": "Download",
}


def stage_label(stage_name: str, pass_no: int = 1) -> str:
    """'Blank/Junk pass 2' — the name a human reads in the log."""
    label = STAGE_LABELS.get(stage_name, stage_name)
    return f"{label} pass {pass_no}" if pass_no and pass_no > 1 else label


def _page_no(ctx: StageContext, page_id: int) -> str:
    for page in ctx.pages:
        if page.get("id") == page_id:
            number = page.get("page_number")
            if number not in (None, ""):
                return str(number)
            name = page.get("page_name")
            return str(name or page_id)
    return str(page_id)


def _page_status(ctx: StageContext, page_id: int, status: str) -> None:
    """One line: ``[chart][page] [stage] status``."""
    from logging_setup import reset_current_page, set_current_page

    total = len(ctx.pages) or len(ctx.todo) or 1
    seen = ctx.done + len(ctx.errors)
    token = set_current_page(_page_no(ctx, page_id))
    try:
        logger.info("%s", status)
    finally:
        reset_current_page(token)
    try:
        from db.paths import write_folder_progress

        write_folder_progress(
            ctx.chart_name,
            seen,
            total,
            detail=f"{stage_label(ctx.stage_name, ctx.pass_no)}: {status}",
        )
    except OSError:
        logger.debug("progress.txt write failed for %s", ctx.chart_name, exc_info=True)


def mark_processing(conn: Any, ctx: StageContext, page_id: int) -> None:
    set_page_stage(
        conn,
        chart_id=ctx.chart_id,
        page_id=page_id,
        stage_name=ctx.stage_name,
        pass_no=ctx.pass_no,
        status="processing",
    )
    from logging_setup import set_current_page

    set_current_page(_page_no(ctx, page_id))


def mark_completed(conn: Any, ctx: StageContext, page_id: int) -> None:
    set_page_stage(
        conn,
        chart_id=ctx.chart_id,
        page_id=page_id,
        stage_name=ctx.stage_name,
        pass_no=ctx.pass_no,
        status="completed",
    )
    ctx.done += 1
    total = len(ctx.pages) or ctx.done
    _page_status(ctx, page_id, f"completed ({ctx.done} of {total})")
    from logging_setup import set_current_page

    set_current_page("")


def mark_failed(
    conn: Any, ctx: StageContext, page_id: int, error: str, page_name: str = ""
) -> None:
    set_page_stage(
        conn,
        chart_id=ctx.chart_id,
        page_id=page_id,
        stage_name=ctx.stage_name,
        pass_no=ctx.pass_no,
        status="failed",
        error_message=error[:2000],
    )
    ctx.errors.append(f"{page_name or page_id}: {error}")
    _page_status(ctx, page_id, f"failed — {error[:160]}")
    from logging_setup import set_current_page

    set_current_page("")


def eligible_for_downstream(
    quality: dict[int, dict[str, Any]],
    bj_flags: dict[int, str],
    page_id: int,
    *,
    handwritten_always: bool = False,
) -> bool:
    """Skip rule shared by final OCR and the extraction stages.

    A page that is blank / junk / duplicate is dropped. Handwritten pages can be
    carried through anyway (`handwritten_always`) because pass 1 never judged
    them — their blank/junk verdict only exists after final OCR.
    """
    if handwritten_always:
        from db import page_type

        if page_type(quality.get(page_id)) == "handwritten":
            return True
    return bj_flags.get(page_id, "not_blank_junk") not in BJ_EXCLUDE


def optional_float(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def unwrap_ocr_content(raw: Optional[str]) -> str:
    """final1/final2 rows may store JSON with a content/markdown field."""
    if not raw:
        return ""
    text = str(raw).strip()
    if not text.startswith("{"):
        return text
    try:
        import json

        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return str(parsed.get("content") or parsed.get("markdown") or "")
        return text
    except (ValueError, TypeError, AttributeError):
        return text


def best_page_text(
    *,
    final2: Optional[str] = None,
    final1: Optional[str] = None,
    prelim: Optional[str] = None,
    quality_row: Optional[dict[str, Any]] = None,
) -> str:
    """Pick text for member/DOS: final2 → final1 → prelim (restricted).

    Prelim is never used for handwritten / uncertain / mixed / low-quality
    pages — those must come from final OCR.
    """
    from db import page_blocks_prelim

    text = unwrap_ocr_content(final2)
    if text.strip():
        return text
    text = unwrap_ocr_content(final1)
    if text.strip():
        return text
    if page_blocks_prelim(quality_row):
        return ""
    return (prelim or "").strip()
