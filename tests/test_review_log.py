"""2026-10-28 (user): the per-user Review Log, a chart marked Reviewed freezing its ground
truth, and Last Reviewed By on each page."""

from datetime import datetime, timezone
from pathlib import Path

from app.core.schemas import FolderSummary, PageGroundTruth
from app.services.chart_review import _CREATE_TABLE, _LAST_MANUAL, _LOG_ENTRIES, log_entries

REPO_ROOT = Path(__file__).resolve().parents[1]


def _at(minute: int) -> datetime:
    return datetime(2026, 10, 28, 9, minute, tzinfo=timezone.utc)


def _row(entry_id, page_id, page, mode, labels, minute, user_id, by):
    return (entry_id, page_id, "chart-a", page, mode, labels, _at(minute), user_id, by)


def test_a_save_lists_only_the_fields_it_changed():
    rows = [
        # A bulk upload sets the labels before; it is not a save.
        _row(1, 10, 3, "bulk_upload", {"page_type": "Lab Report", "dos_from": "2024-01-01"}, 0, None, None),
        _row(2, 10, 3, "manual_entry", {"page_type": "Progress Note", "dos_from": "2024-01-01"}, 5, 7, "Asha"),
        # NA is an empty cell: the date is cleared.
        _row(3, 10, 3, "manual_entry", {"page_type": "Progress Note", "dos_from": "NA "}, 6, 7, "Asha"),
        _row(4, 10, 3, "manual_entry", {"page_sequence": "4"}, 7, 7, "Asha"),
    ]
    items = log_entries(rows, user_id=7, all_users=False)
    assert [item.pageNumber for item in items] == [3, 3, 3]
    assert items[0].fields == [{"field": "page_type", "before": "Lab Report", "after": "Progress Note"}]
    assert items[1].fields == [{"field": "dos_from", "before": "2024-01-01", "after": None}]
    assert items[2].fields == [{"field": "page_sequence", "before": None, "after": "4"}]
    assert items[0].at.startswith("2026-10-28T09:05") and items[0].at.endswith("+00:00")


def test_another_users_save_counts_as_the_before_but_is_not_listed():
    rows = [
        _row(1, 10, 3, "manual_entry", {"codeable": "Codable"}, 1, 8, "Ravi"),
        _row(2, 10, 3, "manual_entry", {"codeable": "Non-Codable"}, 2, 7, "Asha"),
    ]
    mine = log_entries(rows, user_id=7, all_users=False)
    assert len(mine) == 1 and mine[0].user == "Asha"
    assert mine[0].fields == [{"field": "codeable", "before": "Codable", "after": "Non-Codable"}]
    everyone = log_entries(rows, user_id=None, all_users=True)
    assert [item.user for item in everyone] == ["Ravi", "Asha"]


def test_pages_keep_separate_histories():
    rows = [
        _row(1, 10, 3, "manual_entry", {"rotation": "90"}, 1, 7, "Asha"),
        _row(2, 11, 4, "manual_entry", {"rotation": "90"}, 2, 7, "Asha"),
    ]
    items = log_entries(rows, user_id=7, all_users=False)
    assert [item.fields[0]["before"] for item in items] == [None, None]


def test_the_log_reads_one_users_pages_and_the_review_mark_is_one_row_per_chart():
    assert "entry_mode = 'manual_entry' {user_filter}" in _LOG_ENTRIES
    assert "ORDER BY g.id, e.created_at, e.id" in _LOG_ENTRIES
    assert "UNIQUE (chart_name)" in _CREATE_TABLE
    assert "DISTINCT ON (g.page_number)" in _LAST_MANUAL and "'manual_entry'" in _LAST_MANUAL


def test_the_schema_defines_chart_review_and_the_changes_file_adds_it():
    v1 = (REPO_ROOT / "schema" / "v1.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE chart_review (" in v1
    assert "CREATE TRIGGER trg_chart_review_updated_at" in v1
    assert "idx_page_ground_truth_entry_user_id_entry_mode" in v1
    changes = (REPO_ROOT / "schema" / "changes_2026-10-28.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS chart_review" in changes


def test_every_ground_truth_save_route_refuses_a_frozen_chart():
    routes = (REPO_ROOT / "review-ui/backend/app/api/routes.py").read_text(encoding="utf-8")
    for route in (
        '@router.post("/folders/{folder_id}/ground-truth"',
        '@router.put("/folders/{folder_id}/ground-truth/sequence"',
        '@router.put("/folders/{folder_id}/encounter-review"',
        '@router.put("/folders/{folder_id}/chart-ground-truth"',
    ):
        body = routes.split(route)[1].split("@router.")[0]
        assert "_refuse_if_frozen(settings, folder_id)" in body, route


def test_reviewed_fields_ride_on_the_page_and_the_chart_list():
    truth = PageGroundTruth(pageNumber=1, reviewedBy="Asha", reviewedAt="2026-10-28T09:05:00+00:00")
    assert truth.reviewedBy == "Asha"
    row = FolderSummary(id="c", name="c", reviewed=True, reviewed_by="Asha")
    assert row.reviewed and row.reviewed_by == "Asha"


def test_chart_review_entry_is_in_the_schema_and_the_changes_file():
    v1 = (REPO_ROOT / "schema" / "v1.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE chart_review_entry (" in v1
    assert "CREATE TRIGGER trg_chart_review_entry_updated_at" in v1
    changes = (REPO_ROOT / "schema" / "changes_2026-10-28.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS chart_review_entry" in changes
    from app.services.chart_review import _CREATE_ENTRY_TABLE

    for action in ("'reviewed'", "'review_cleared'", "'member_decision'", "'encounters'"):
        assert action in v1.split("CREATE TABLE chart_review_entry (")[1].split(");")[0]
        assert action in _CREATE_ENTRY_TABLE


def test_marking_and_clearing_a_review_are_both_logged_and_the_log_reads_them():
    import inspect

    from app.services import chart_review

    body = inspect.getsource(chart_review.set_chart_review)
    assert '"reviewed" if reviewed else "review_cleared"' in body
    assert "RETURNING id" in body  # a no-op mark or clear is not logged
    assert "FROM chart_review_entry r" in inspect.getsource(chart_review.review_log)


def test_member_decision_and_encounter_saves_are_logged():
    import inspect

    from app.services import chart_ground_truth, encounter_review

    assert '"member_decision", changed' in inspect.getsource(chart_ground_truth.save_chart_ground_truth)
    assert '"encounters", changed' in inspect.getsource(encounter_review.save_encounter_review)


def test_encounter_changes_lists_each_encounter_added_removed_or_changed():
    from app.core.schemas import EncounterReviewRow
    from app.services.encounter_review import encounter_changes

    def row(start, end, page_type, dos=None):
        return EncounterReviewRow(
            seq=1, startPage=start, endPage=end, pageType=page_type, encounterType=None,
            dosFrom=dos, dosTo=dos,
        )

    before = [row(1, 2, "Progress Note", "2024-01-01"), row(3, 3, "Lab Report")]
    after = [row(1, 2, "Progress Note", "2024-01-01"), row(3, 3, "Radiology"), row(4, 6, "Lab Report")]
    assert encounter_changes(before, after) == [
        {"field": "encounter", "before": "Pages 3 · Lab Report", "after": "Pages 3 · Radiology"},
        {"field": "encounter", "before": None, "after": "Pages 4–6 · Lab Report"},
    ]
    assert encounter_changes(after, after) == []
