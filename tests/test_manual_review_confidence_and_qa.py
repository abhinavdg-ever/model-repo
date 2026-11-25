"""2026-11-25 (user): Manual Review shows the pipeline value's confidence in brackets, and a
loaded QA Ground Truth file is kept in qa_ground_truth."""
import json
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
    row = (3, *[values[c] for c in columns], "qa.csv", datetime(2026, 11, 25, tzinfo=timezone.utc),
           {"Page Type": "Progress Note", "cv:orientation": "90", "cv:quality": ""})

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
    # A chart-view field kept in labels comes back by its id; an empty one does not.
    assert pages == {"3": {"page_type": "Progress Note", "orientation": "90", "source_file": "qa.csv",
                           "updated_at": "2026-11-25T00:00:00+00:00"}}


def _qa_app(monkeypatch, user):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import charts, deps, router

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[deps.current_user] = lambda: user
    monkeypatch.setattr(charts, "database_url_usable", lambda url: True)
    return TestClient(app)


def test_only_the_testing_user_saves_qa_in_the_chart_view(monkeypatch):
    from app.services.auth import SignedInUser

    saved = []
    monkeypatch.setattr(qa_ground_truth, "save_page",
                        lambda url, schema, chart, page, values, user_id: saved.append((chart, page, values, user_id))
                        or {"page_type": "Lab Report"})
    body = {"values": {"page_type": "Lab Report"}}
    assert _qa_app(monkeypatch, None).put("/api/folders/c1/qa-ground-truth/2", json=body).status_code == 401
    reviewer = SignedInUser(id=5, username="someone", name="S", role="reviewer")
    assert _qa_app(monkeypatch, reviewer).put("/api/folders/c1/qa-ground-truth/2", json=body).status_code == 403
    tester = SignedInUser(id=7, username="Testing-User", name="T", role="reviewer")
    response = _qa_app(monkeypatch, tester).put("/api/folders/c1/qa-ground-truth/2", json=body)
    assert response.status_code == 200 and response.json() == {"page": {"page_type": "Lab Report"}}
    assert saved == [("c1", 2, {"page_type": "Lab Report"}, 7)]
    assert _qa_app(monkeypatch, tester).put("/api/folders/c1/qa-ground-truth/2", json={"x": 1}).status_code == 400


def test_save_page_sets_only_the_given_columns(monkeypatch):
    from contextlib import contextmanager

    import app.services.db as db

    seen = []

    class Cur:
        def execute(self, sql, params):
            seen.append((sql, params))

        def fetchall(self):
            return []

    @contextmanager
    def fake(url, schema):
        @contextmanager
        def cursor():
            yield Cur()
        yield type("C", (), {"cursor": staticmethod(cursor)})()

    monkeypatch.setattr(db, "connection", fake)
    qa_ground_truth.save_page("postgresql://x/y", "public", "c1", 2,
                              {"page_type": "Lab Report", "dos_from": "", "orientation": "90", "Bad Key!": "x"}, 7)
    sql, params = seen[0]
    assert "page_type = EXCLUDED.page_type" in sql and "dos_from = EXCLUDED.dos_from" in sql
    assert "orientation" not in sql and "codeable" not in sql   # a chart-view field goes to labels
    assert params[:4] == ("c1", 2, "Lab Report", None)   # "" clears
    labels = json.loads(params[4])
    assert labels["cv:orientation"] == "90" and "Bad Key!" not in str(labels)
    assert params[-2:] == (qa_ground_truth.CHART_VIEW_SOURCE, 7)
    # Only chart-view fields: no column in the INSERT, labels only.
    seen.clear()
    qa_ground_truth.save_page("postgresql://x/y", "public", "c1", 2, {"quality": "Good"}, 7)
    assert "(chart_name, page_number, labels," in seen[0][0]
    with pytest.raises(ValueError):
        qa_ground_truth.save_page("postgresql://x/y", "public", "c1", 2, {"Bad Key!": "x"}, 7)


def test_one_bad_chart_is_skipped_not_the_run():
    """2026-11-25 (user): "Stopped at chart ...": one chart's error skips that chart only."""
    ground = (b"PDF ID,Page Number,Page Type\n"
              b"c1,1,Progress Note\nbad,1,Lab Report\nc3,1,Lab Report\n")

    def load(pdf_id):
        if pdf_id == "bad":
            raise ValueError("finalConfidences.member_raw Input should be a valid number")
        return f"PDF ID,Page Number,Page Type\n{pdf_id},1,Lab Report\n".encode()

    events = list(excel_compare.stream((ground, "gt.csv"), None, load_system=load))
    kinds = [e["type"] for e in events]
    assert kinds == ["start", "chart", "skipped", "chart", "done"]
    assert events[2]["pdfId"] == "bad" and "member_raw" in events[2]["detail"]
    done = events[-1]
    assert done["groundCount"] == 2 and done["correctCount"] == 1 and done["errorCount"] == 1
    assert done["skipped"] == [{"pdfId": "bad", "detail": events[2]["detail"]}]


def test_one_bad_chart_is_skipped_in_the_three_way_review():
    from app.services.excel_compare import three_way

    qa = b"PDF ID,Page Number,Page Type\nc1,1,Progress Note\nbad,1,Lab Report\n"
    client = b"PDF ID,Page Number,Page Type\nc1,1,Progress Note\nbad,1,Lab Report\n"

    def load(pdf_id):
        if pdf_id == "bad":
            raise ValueError("cannot read")
        return b"PDF ID,Page Number,Page Type\nc1,1,Progress Note\n"

    events = list(three_way.stream({"qa": (qa, "qa.csv"), "client": (client, "c.csv"), "pipeline": None},
                                   load_pipeline=load))
    assert [e["type"] for e in events] == ["start", "chart", "skipped", "done"]
    done = events[-1]
    assert done["skipped"] == [{"pdfId": "bad", "detail": "cannot read"}]
    # The skipped chart counts in no comparison.
    assert all(c["groundCount"] == 1 for c in done["comparisons"].values())


def test_a_bad_page_fails_its_chart_only_in_the_batch_read(monkeypatch):
    from contextlib import contextmanager

    import app.services.excel_compare.database_algo as algo

    rows = [("good", 1, "g1"), ("bad", 1, "b1"), ("bad", 2, "b2"), ("good", 2, "g2")]

    class Cur:
        def __init__(self):
            self.last = None

        def execute(self, sql, params):
            self.last = sql

        def fetchall(self):
            return rows if "page_list" in self.last else []

    @contextmanager
    def fake(url, schema):
        @contextmanager
        def cursor():
            yield Cur()
        yield type("C", (), {"cursor": staticmethod(cursor)})()

    def page(row):
        if row[0] == "bad" and row[1] == 2:
            raise ValueError("member_raw\nInput should be a valid number")
        return ImagingPageResult(pageNumber=row[1], fileName=f"{row[1]}.jpg")

    monkeypatch.setattr(algo, "connection", fake)
    monkeypatch.setattr(algo, "_page", page)
    docs, _ = algo.fetch("postgresql://x/y", "public", ["good", "bad"])
    assert [p.pageNumber for p in docs["good"].pages] == [1, 2]
    assert isinstance(docs["bad"], algo.FailedChart) and docs["bad"].detail == "page 2: member_raw"


def test_all_qa_endpoint_is_for_the_testing_user(monkeypatch):
    from app.services.auth import SignedInUser

    monkeypatch.setattr(qa_ground_truth, "all_pages", lambda url, schema: {"c1": {"1": {"page_type": "Lab Report"}}})
    reviewer = SignedInUser(id=5, username="someone", name="S", role="reviewer")
    assert _qa_app(monkeypatch, reviewer).get("/api/qa-ground-truth").status_code == 403
    tester = SignedInUser(id=7, username="testing-user", name="T", role="reviewer")
    assert _qa_app(monkeypatch, tester).get("/api/qa-ground-truth").json() == {
        "charts": {"c1": {"1": {"page_type": "Lab Report"}}}}

