"""2026-11-25 (user): Manual Review shows the pipeline value's confidence in brackets, and a
loaded QA Ground Truth file is kept in qa_ground_truth."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("pandas")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "review-ui" / "backend"))

from app.core.schemas import ImagingDocumentResponse, ImagingPageResult  # noqa: E402
from app.services import excel_compare, qa_ground_truth  # noqa: E402
from app.services.excel_compare import database_system  # noqa: E402


def _page(**kw):
    base = dict(pageNumber=1, fileName="1.jpg", blankOrJunk="No", isDuplicate=False,
                pageType="Progress Note (Office Visit)", isCodeable="Codable", dosFrom="2024-03-15")
    return ImagingPageResult(**{**base, **kw})


def _doc(*pages):
    return ImagingDocumentResponse.model_construct(folder_id="c1", pages=list(pages), verifications=[], verification=None)


def test_confidence_columns_read_as_the_chart_view():
    page = _page(finalConfidences={"page_type": 0.6, "codability": 0.9, "dos": 0.876, "member_name": 0.95})
    cols = database_system.confidence_columns(page, {"page_type": [[0.0, 0.0], [1.0, 0.5]]})
    assert cols["Confidence: Page Type"] == "30"          # through the curve
    assert cols["Confidence: Codable / Non-Codable"] == "90"
    assert cols["Confidence: DOS From"] == cols["Confidence: DOS To"] == "88"
    assert cols["Confidence: Extracted Name"] == "95"
    assert cols["Confidence: Extracted DOB"] == "80"       # not found on a scored page
    assert "Confidence: Provider Credentials" not in cols


def test_discharge_summary_codability_is_low_at_most():
    page = _page(pageType="Discharge Summary", finalConfidences={"codability": 0.95})
    assert database_system.confidence_columns(page)["Confidence: Codable / Non-Codable"] == "49"


def test_an_unscored_page_has_no_confidences():
    assert database_system.confidence_columns(_page()) == {}


def test_compare_rows_carry_the_pipeline_confidences_and_never_compare_them():
    ground = b"PDF ID,Page Number,Page Type\nc1,1,Progress Note\nc1,2,Lab Report\n"
    system = database_system.system_csv([("c1", _doc(
        _page(finalConfidences={"page_type": 0.8}),
        _page(pageNumber=2, fileName="2.jpg", pageType="Radiology Report", finalConfidences={"page_type": 0.42}),
    ))])
    events = list(excel_compare.stream((ground, "gt.csv"), (system, "algo.csv")))
    assert events[0]["fields"] == ["Page Type"]
    rows = {row["pageNumber"]: row for row in events[1]["rows"]}
    assert rows["1"]["confidences"] == {"Page Type": 80}
    assert rows["2"]["confidences"] == {"Page Type": 42}
    assert rows["2"]["status"] == "ERROR"


def test_an_uploaded_pipeline_sheet_has_no_confidences():
    ground = b"PDF ID,Page Number,Page Type\nc1,1,Progress Note\n"
    system = b"PDF ID,Page Number,Page Type\nc1,1,Progress Note\n"
    row = list(excel_compare.stream((ground, "gt.csv"), (system, "algo.csv")))[1]["rows"][0]
    assert "confidences" not in row


def test_qa_rows_from_a_sheet():
    data = (
        b"PDF ID,Page Number,Page Type,Codable / Non-Codable,Notes\n"
        b"c1,1,Progress Note,Codable,ok\n"
        b"c1,1.0,Lab Report,Non-Codable,\n"   # the same page again: the last row wins
        b"c2,x,Lab Report,,\n"                 # no page number: skipped
        b"c2,3,,,\n"
    )
    rows = sorted(qa_ground_truth.rows_from_sheet(data, "qa.csv"), key=lambda r: (r["chart_name"], r["page_number"]))
    assert [(r["chart_name"], r["page_number"]) for r in rows] == [("c1", 1), ("c2", 3)]
    assert rows[0]["page_type"] == "Lab Report" and rows[0]["codeable"] == "Non-Codable"
    assert rows[0]["labels"]["Page Type"] == "Lab Report" and "Notes" not in rows[0]["labels"]
    assert rows[1]["page_type"] is None and rows[1]["member_name"] is None


def test_storing_qa_never_raises():
    assert qa_ground_truth.store_sheet("postgresql://nowhere.invalid/x", "public", b"PDF ID\n", "qa.csv", None, None) == 0


def test_chart_qa_endpoint_without_a_database_is_empty(monkeypatch):
    """2026-11-25 (user): the chart view's QA Ground Truth column reads /qa-ground-truth; with no
    database (or on Cosmos) it is empty, never an error."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import charts, router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    monkeypatch.setattr(charts, "database_url_usable", lambda url: False)
    assert TestClient(app).get("/api/folders/c1/qa-ground-truth").json() == {"pages": {}}


def test_chart_pages_shapes_the_rows(monkeypatch):
    from contextlib import contextmanager
    from datetime import datetime, timezone

    import app.services.db as db

    columns = list(qa_ground_truth.COLUMNS.values())
    values = {c: None for c in columns} | {"page_type": "Progress Note", "codeable": ""}
    row = (3, *[values[c] for c in columns], "qa.csv", datetime(2026, 11, 25, tzinfo=timezone.utc))

    class Cur:
        def execute(self, sql, params):
            assert params == ("c1",)

        def fetchall(self):
            return [row]

    @contextmanager
    def fake(url, schema):
        @contextmanager
        def cursor():
            yield Cur()
        yield type("C", (), {"cursor": staticmethod(cursor)})()

    monkeypatch.setattr(db, "connection", fake)
    pages = qa_ground_truth.chart_pages("postgresql://x/y", "public", "c1")
    assert pages == {"3": {"page_type": "Progress Note", "source_file": "qa.csv",
                           "updated_at": "2026-11-25T00:00:00+00:00"}}
