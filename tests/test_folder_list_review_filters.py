"""2026-11-05 (user): History filters Reviewed / Not Reviewed and With / No Ground Truth."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review-ui" / "backend"))

from app.core.schemas import FolderSummary  # noqa: E402
from app.services import folder_list  # noqa: E402


def _row(name, reviewed, truth):
    return FolderSummary(id=name, name=name, ocr_status="IMAGING_COMPLETED", reviewed=reviewed,
                         ground_truth_available=truth)


def test_reviewed_and_ground_truth_labels():
    assert folder_list.reviewed_label(_row("a", True, False)) == "Yes"
    assert folder_list.reviewed_label(_row("a", False, False)) == "No"
    assert folder_list.truth_label(_row("a", False, True)) == "Yes"
    assert folder_list.truth_label(_row("a", False, False)) == "No"
    assert folder_list.FolderListParams(reviewed=["Yes"], truth=["No"]).truth == ["No"]


def test_reviewed_at_is_a_datetime_not_a_string():
    """2026-11-05: a str reviewed_at made pydantic warn on every History response."""
    from types import SimpleNamespace
    import warnings

    review = SimpleNamespace(reviewedBy="a", reviewedById=1, reviewedAt="2026-11-02T06:58:04.785312+00:00")
    row = _row("c", False, False).model_copy(update=folder_list._review_fields(review))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        row.model_dump_json()
    assert row.reviewed_at.year == 2026
