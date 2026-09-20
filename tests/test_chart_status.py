"""Chart status derivation from per-page stage rows (schema v7).

The rule: the current stage is the earliest stage, in pipeline_stage.seq order,
where not every page is completed|skipped.
"""
from __future__ import annotations

import pytest

from db.chart_status import CHART_STATUS_VALUES, compute_progress

STAGES = [
    ("ocr_prelim", 1, 10),
    ("ocr_quality", 1, 20),
    ("blank_junk", 1, 30),
    ("ocr_final1", 1, 40),
    ("ocr_final2", 1, 50),
    ("blank_junk", 2, 60),
    ("member_verify", 1, 70),
    ("dos_extract", 1, 80),
]


def rows(total: int, **done_counts: int):
    """Build v_chart_stage_progress rows.

    `done_counts` maps "stage" or "stage_2" to how many pages are completed.
    Anything unnamed is fully pending.
    """
    out = []
    for name, pass_no, seq in STAGES:
        key = name if pass_no == 1 else f"{name}_{pass_no}"
        completed = done_counts.get(key, 0)
        out.append(
            {
                "stage_name": name,
                "pass_no": pass_no,
                "seq": seq,
                "label": name,
                "pages_total": total,
                "pending": max(0, total - completed),
                "processing": 0,
                "completed": completed,
                "failed": 0,
                "skipped": 0,
            }
        )
    return out


class TestEmptyChart:
    def test_no_pages_stays_received(self):
        out = compute_progress([], pages_total=0)
        assert out["status"] == "received"
        assert out["current_stage"] is None

    def test_no_pages_keeps_downloading(self):
        out = compute_progress([], pages_total=0, previous_status="downloading")
        assert out["status"] == "downloading"


class TestEarliestIncompleteStage:
    def test_fresh_chart_sits_at_the_first_stage(self):
        out = compute_progress(rows(3), pages_total=3)
        assert out["status"] == "processing"
        assert out["current_stage"] == "ocr_prelim"
        assert out["current_pass"] == 1

    def test_advances_when_a_stage_completes(self):
        out = compute_progress(rows(3, ocr_prelim=3), pages_total=3)
        assert out["current_stage"] == "ocr_quality"

    def test_partial_completion_does_not_advance(self):
        out = compute_progress(rows(3, ocr_prelim=2), pages_total=3)
        assert out["current_stage"] == "ocr_prelim"

    def test_blank_junk_pass_two_is_its_own_stage(self):
        """v6 could not express this: one status column for two passes meant a
        chart in pass 2 reported blank/junk already finished."""
        done = dict(
            ocr_prelim=4, ocr_quality=4, blank_junk=4, ocr_final1=4, ocr_final2=4
        )
        out = compute_progress(rows(4, **done), pages_total=4)
        assert out["current_stage"] == "blank_junk"
        assert out["current_pass"] == 2

    def test_pass_two_complete_moves_to_member(self):
        done = dict(
            ocr_prelim=4, ocr_quality=4, blank_junk=4, ocr_final1=4,
            ocr_final2=4, blank_junk_2=4,
        )
        out = compute_progress(rows(4, **done), pages_total=4)
        assert out["current_stage"] == "member_verify"


class TestTerminalStates:
    def all_done(self, total=2):
        return dict(
            ocr_prelim=total, ocr_quality=total, blank_junk=total,
            ocr_final1=total, ocr_final2=total, blank_junk_2=total,
            member_verify=total, dos_extract=total,
        )

    def test_everything_done_is_completed(self):
        out = compute_progress(rows(2, **self.all_done()), pages_total=2)
        assert out["status"] == "completed"
        assert out["current_stage"] is None

    def test_member_needs_review_wins_over_completed(self):
        out = compute_progress(
            rows(2, **self.all_done()),
            pages_total=2,
            member_final_status="needs_review",
        )
        assert out["status"] == "needs_review"

    def test_member_reject_does_not_set_chart_rejected(self):
        """Accept/reject is on the summary; chart lifecycle stays completed."""
        out = compute_progress(
            rows(2, **self.all_done()),
            pages_total=2,
            member_document_decision="reject",
        )
        assert out["status"] == "completed"

    def test_reject_does_not_outrank_needs_review(self):
        out = compute_progress(
            rows(2, **self.all_done()),
            pages_total=2,
            member_final_status="needs_review",
            member_document_decision="reject",
        )
        assert out["status"] == "needs_review"


class TestFailures:
    def test_failed_page_in_an_incomplete_stage_fails_the_chart(self):
        data = rows(3)
        data[0]["failed"] = 1
        data[0]["pending"] = 2
        out = compute_progress(data, pages_total=3)
        assert out["status"] == "failed"

    def test_failed_page_in_an_otherwise_complete_stage_does_not(self):
        """Every page reached a terminal state, so the stage is done; the page's
        own failure is recorded on the page, not escalated to the chart."""
        data = rows(3, ocr_prelim=2)
        data[0]["failed"] = 1
        data[0]["pending"] = 0
        data[0]["skipped"] = 1
        out = compute_progress(data, pages_total=3)
        assert out["status"] != "failed"


class TestSkippedCountsAsDone:
    def test_skipped_pages_advance_a_stage(self):
        data = rows(4, ocr_prelim=4, ocr_quality=4)
        bj = next(r for r in data if r["stage_name"] == "blank_junk" and r["pass_no"] == 1)
        bj["completed"] = 3
        bj["skipped"] = 1
        bj["pending"] = 0
        out = compute_progress(data, pages_total=4)
        assert out["current_stage"] == "ocr_final1"


class TestProgressShape:
    def test_every_stage_is_reported(self):
        out = compute_progress(rows(2), pages_total=2)
        assert len(out["stages"]) == len(STAGES)
        assert out["stages"][0]["stage"] == "ocr_prelim"
        assert out["stages"][-1]["stage"] == "dos_extract"

    def test_status_is_always_a_legal_value(self):
        for done in ({}, dict(ocr_prelim=2), dict(ocr_prelim=2, ocr_quality=2)):
            out = compute_progress(rows(2, **done), pages_total=2)
            assert out["status"] in CHART_STATUS_VALUES

    def test_stage_counts_add_up(self):
        out = compute_progress(rows(5, ocr_prelim=2), pages_total=5)
        first = out["stages"][0]
        assert first["completed"] + first["pending"] == 5
        assert first["total"] == 5
        assert first["complete"] is False
