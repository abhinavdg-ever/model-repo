"""2026-11-25 (user): History filters on the Pages and Confidence columns."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review-ui" / "backend"))

from app.core.schemas import FolderSummary  # noqa: E402
from app.services import folder_list  # noqa: E402
from app.services.folder_list import FolderListParams, filter_sort_page  # noqa: E402

ROWS = [FolderSummary(id=n, name=n, ocr_status="IMAGING_COMPLETED", page_count=pages)
        for n, pages in (("a", 5), ("b", 40), ("c", 120), ("d", 300))]
CONF = {"a": {"level": "high", "value": 0.86}, "b": {"level": "medium", "value": 0.62},
        "c": {"level": "low", "value": 0.31}, "d": {"level": None, "value": None}}


def _names(params, calls=None):
    def read(names):
        if calls is not None:
            calls.append(list(names))
        return {n: CONF[n] for n in names}
    return sorted(f.id for f in filter_sort_page(ROWS, params, read).items)


def test_pages_range_is_inclusive():
    assert _names(FolderListParams(pages_min=40, pages_max=120)) == ["b", "c"]
    assert _names(FolderListParams(pages_min=100)) == ["c", "d"]
    assert _names(FolderListParams(pages_max=40)) == ["a", "b"]


def test_confidence_levels_and_blank():
    assert _names(FolderListParams(confidence=["High", "Low"])) == ["a", "c"]
    assert _names(FolderListParams(confidence=["(Blank)"])) == ["d"]


def test_confidence_percent_range_on_the_shown_value():
    assert _names(FolderListParams(confidence_min=62)) == ["a", "b"]
    assert _names(FolderListParams(confidence_max=62)) == ["b", "c"]  # not scored: out


def test_confidence_read_only_when_filtering_and_after_other_filters():
    calls = []
    _names(FolderListParams(pages_min=40), calls)
    assert calls == []
    _names(FolderListParams(pages_min=40, confidence=["Medium"]), calls)
    assert calls == [["b", "c", "d"]]


def test_passes_confidence_missing_chart():
    assert folder_list.passes_confidence(None, FolderListParams(confidence=["(Blank)"]))
    assert not folder_list.passes_confidence(None, FolderListParams(confidence_min=0))


def test_qa_ground_truth_filter():
    """2026-11-25 (user): History's QA Ground Truth column filters Yes / No."""
    rows = [FolderSummary(id=n, name=n, ocr_status="IMAGING_COMPLETED", qa_ground_truth_available=qa)
            for n, qa in (("a", True), ("b", False), ("c", True))]
    names = lambda p: sorted(f.id for f in filter_sort_page(rows, p).items)  # noqa: E731
    assert names(FolderListParams(qa=["Yes"])) == ["a", "c"]
    assert names(FolderListParams(qa=["No"])) == ["b"]
    assert names(FolderListParams(qa=["Yes", "No"])) == ["a", "b", "c"]
    assert names(FolderListParams()) == ["a", "b", "c"]
