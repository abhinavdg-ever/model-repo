"""Tests for paginated / DB-backed folder listing helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from app.core.schemas import FolderSummary
from app.services.folder_list import (
    FolderListParams,
    filter_sort_page,
    merge_db_and_disk,
    list_disk_folder_names_light,
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
    assert result.batch_options == ["B4"]

    # No run selected → batch facet locked empty
    unlocked = filter_sort_page(rows, FolderListParams())
    assert unlocked.batch_options == []
    assert unlocked.run_options == ["R1", "R2"]

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


def test_filter_sort_page_hides_queued():
    rows = [
        _f("ready", status="IN_PROGRESS", run="R1", batch="B1"),
        _f("waiting", status="QUEUED", run="R1", batch="B1"),
        _f("only_queued_run", status="QUEUED", run="R9", batch="B9"),
    ]
    result = filter_sort_page(rows, FolderListParams())
    assert [f.id for f in result.items] == ["ready"]
    assert result.total == 1
    assert result.run_options == ["R1"]
    assert "R9" not in result.run_options


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
    (pages / "note.txt").write_text("no", encoding="utf-8")
    assert quick_page_count(chart) == 2


def test_run_after_keeps_charts_last_run_at_or_after_the_moment():
    from datetime import timedelta

    ist = timezone(timedelta(hours=5, minutes=30))
    rows = [
        _f("before", updated=datetime(2026, 10, 16, 23, 59, tzinfo=ist)),
        _f("at_midnight", updated=datetime(2026, 10, 17, 0, 0, tzinfo=ist)),
        _f("utc_after", updated=datetime(2026, 10, 17, 3, 0, tzinfo=timezone.utc)),
        _f("never_run", updated=None),
    ]
    params = FolderListParams(run_after=datetime(2026, 10, 17, 0, 0, tzinfo=ist), limit=None)
    assert sorted(f.name for f in filter_sort_page(rows, params).items) == ["at_midnight", "utc_after"]
    assert filter_sort_page(rows, FolderListParams(limit=None)).total == 4


def test_source_measure_retrieval_filter_and_offer_every_value():
    rows = [
        _f("a").model_copy(update={"source_name": "Clinic A", "measure": "CBP", "rr_method": "Fax"}),
        _f("b").model_copy(update={"source_name": "Clinic B", "measure": "CBP", "rr_method": "Onsite"}),
        _f("c"),  # no metadata: offered and picked as "(Blank)"
    ]
    result = filter_sort_page(rows, FolderListParams(measure=["CBP"], retrieval=["Fax"]))
    assert [f.id for f in result.items] == ["a"]
    assert result.source_options == ["Clinic A", "Clinic B", "(Blank)"]
    assert result.measure_options == ["CBP", "(Blank)"]
    assert result.retrieval_options == ["Fax", "Onsite", "(Blank)"]
    assert result.scope_options == ["(Blank)"]
    assert filter_sort_page(rows, FolderListParams()).total == 3
    blank = filter_sort_page(rows, FolderListParams(measure=["(Blank)"]))
    assert [f.id for f in blank.items] == ["c"]
    assert filter_sort_page(rows, FolderListParams(scope=["(Blank)"])).total == 3


def test_chart_metadata_from_the_csvs(tmp_path):
    from app.services.chart_metadata import chart_metadata_map
    from app.services.metadata_csv import clear_metadata_cache

    (tmp_path / "metadata_R1_B1.csv").write_text(
        "recordId,DummyFirstName,SourceName,Measure,RR Method,Project Scope\n"
        "c1,Ann,Clinic A,CBP,Fax,ACA\nc2,Bob,,,,\n",
        encoding="utf-8",
    )
    clear_metadata_cache()
    found = chart_metadata_map(None, metadata_dir=tmp_path)
    assert found == {
        "c1": {"project_scope": "ACA", "source_name": "Clinic A", "measure": "CBP", "rr_method": "Fax"}
    }


def test_a_workspace_only_chart_takes_its_status_from_disk(tmp_path):
    done = tmp_path / "chart_a-test"
    (done / "imaging").mkdir(parents=True)
    (done / "imaging" / "chart_a-test_final.csv").write_text("x", encoding="utf-8")
    ocr_only = tmp_path / "chart_b"
    (ocr_only / "ocr").mkdir(parents=True)
    (ocr_only / "ocr" / "1.json").write_text("{}", encoding="utf-8")
    (tmp_path / "chart_c" / "pages").mkdir(parents=True)
    status = {f.name: f.ocr_status for f in list_disk_folder_names_light(tmp_path)}
    assert status == {"chart_a-test": "IMAGING_COMPLETED", "chart_b": "COMPLETED", "chart_c": "QUEUED"}
    # Only the queued one is hidden from the list.
    shown = filter_sort_page(list_disk_folder_names_light(tmp_path), FolderListParams())
    assert sorted(f.name for f in shown.items) == ["chart_a-test", "chart_b"]
