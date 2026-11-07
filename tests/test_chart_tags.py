"""2026-11-07 (user): a chart can carry tags, and History / Accuracy filter on them."""
import sys

import pytest

sys.path.insert(0, "review-ui/backend")

from app.core.schemas import FolderSummary  # noqa: E402
from app.services.chart_tags import clean_tags  # noqa: E402
from app.services.folder_list import FolderListParams, filter_sort_page  # noqa: E402


def test_tags_are_trimmed_and_unique_without_case():
    assert clean_tags(["  Pilot  run ", "pilot run", "", "Batch 7"]) == ["Pilot run", "Batch 7"]


def test_too_many_tags_are_refused():
    with pytest.raises(ValueError):
        clean_tags([f"t{i}" for i in range(21)])


def _rows():
    return [
        FolderSummary(id="a", name="a", ocr_status="IMAGING_COMPLETED", tags=["Pilot", "Recheck"]),
        FolderSummary(id="b", name="b", ocr_status="IMAGING_COMPLETED", tags=["Recheck"]),
        FolderSummary(id="c", name="c", ocr_status="IMAGING_COMPLETED"),
    ]


def test_history_filters_on_any_picked_tag():
    result = filter_sort_page(_rows(), FolderListParams(tag=["Pilot"], sort="filename", sort_dir="asc"))
    assert [f.name for f in result.items] == ["a"]
    result = filter_sort_page(_rows(), FolderListParams(tag=["Pilot", "(Blank)"], sort="filename", sort_dir="asc"))
    assert [f.name for f in result.items] == ["a", "c"]
    assert result.tag_options == ["Pilot", "Recheck", "(Blank)"]


def _tool():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "utilities" / "tag_chart.py"
    spec = importlib.util.spec_from_file_location("tag_chart", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_utility_adds_removes_and_keeps_the_spelling_in_use():
    tool = _tool()
    known = {"pilot": "Pilot"}
    assert tool.next_tags([], add=["pilot", " Recheck "], remove=[], clear=False, known=known) == ["Pilot", "Recheck"]
    assert tool.next_tags(["Pilot", "Recheck"], add=[], remove=["recheck"], clear=False, known=known) == ["Pilot"]
    assert tool.next_tags(["Pilot"], add=["New"], remove=[], clear=True, known=known) == ["New"]


def test_the_utility_reads_a_chart_file(tmp_path):
    path = tmp_path / "charts.txt"
    path.write_text("1234\n# note\n52743839_44976074, 1235  # trailing\n1234\n", encoding="utf-8")
    assert _tool().read_chart_file(str(path)) == ["1234", "52743839_44976074", "1235"]
