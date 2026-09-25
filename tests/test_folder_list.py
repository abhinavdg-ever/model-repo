"""Tests for paginated / DB-backed folder listing helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.schemas import FolderSummary
from app.services.folder_list import (
    FolderListParams,
    filter_sort_page,
    merge_db_and_disk,
    quick_page_count,
)


def _f(
    name: str,
    *,
    pages: int = 1,
    status: str = "IMAGING_COMPLETED",
    run: str | None = None,
    batch: str | None = None,
    updated: datetime | None = None,
) -> FolderSummary:
    return FolderSummary(
        id=name,
        name=name,
        page_count=pages,
        ocr_processed=0,
        imaging_processed=0,
        ocr_status=status,  # type: ignore[arg-type]
        last_updated_at=updated,
        run_id=run,
        batch_id=batch,
    )


def test_filter_sort_page_paginates_and_facets():
    rows = [
        _f("a_chart", pages=3, run="R1", batch="B1"),
        _f("b_chart", pages=10, run="R2", batch="B4"),
        _f("c_chart", pages=1, run="R2", batch="B4", status="FAILED"),
    ]
    # q=chart matches all three; run=R2 → b_chart, c_chart
    result = filter_sort_page(
        rows,
        FolderListParams(
            q="chart",
            run=["R2"],
            sort="pages",
            sort_dir="desc",
            limit=1,
            offset=0,
        ),
    )
    assert result.total == 2
    assert result.items[0].id == "b_chart"
    assert result.page_count_sum == 11
    assert result.run_options == ["R1", "R2"]
    assert result.batch_options == ["B1", "B4"]

    page2 = filter_sort_page(
        rows,
        FolderListParams(
            q="chart",
            run=["R2"],
            sort="pages",
            sort_dir="desc",
            limit=1,
            offset=1,
        ),
    )
    assert page2.items[0].id == "c_chart"


def test_merge_db_and_disk_prefers_db():
    db = [_f("x", pages=5, run="R2", batch="B4")]
    disk = [
        _f("x", pages=99),
        _f("y", pages=2),
    ]
    merged = merge_db_and_disk(db, disk)
    by_id = {f.id: f for f in merged}
    assert by_id["x"].page_count == 5
    assert by_id["x"].run_id == "R2"
    assert by_id["y"].page_count == 2


def test_quick_page_count(tmp_path):
    chart = tmp_path / "chart"
    pages = chart / "pages"
    pages.mkdir(parents=True)
    (pages / "1.jpg").write_bytes(b"x")
    (pages / "2.png").write_bytes(b"x")
    (pages / "note.txt").write_text("no")
    assert quick_page_count(chart) == 2
