"""Manual Review across three sources: QA Ground Truth (upload), Client Ground
Truth (database or upload) and Pipeline Output (database or upload). Every
pair of the sources given, plus All 3; the downloads carry every comparison."""

import io
import json

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("pandas")
pytest.importorskip("httpx")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import router
from app.services import excel_compare

# Page 1: all three agree. Page 2: QA = Client, the pipeline's DOS differs.
# Page 3: QA and Client disagree on codability; the pipeline has no page 3.
QA = (
    "PDF ID,Page Number,DOS From,DOS To,Codable / Non-Codable,Extracted Name\n"
    "100,1,03/15/2024,03/15/2024,Codable,Yes\n"
    "100,2,03/16/2024,03/16/2024,Non Codable,No\n"
    "100,3,-,-,Non Codable,\n"
)
CLIENT = (
    "PDF ID,Page Number,DOS From,DOS To,Codable / Non-Codable,Extracted Name\n"
    "100,1,2024-03-15,2024-03-15,Codable,Lisa Anderson\n"
    "100,2,2024-03-16,2024-03-16,Non-Codable,No\n"
    "100,3,-,-,Codable,Yes\n"
)
PIPELINE = (
    "PDF ID,Page Number,DOS From,DOS To,Codable / Non-Codable,Extracted Name\n"
    "100,1,2024-03-15,2024-03-15,Codable,Lisa Anderson\n"
    "100,2,2024-03-17,2024-03-16,Non-Codable,Not Found\n"
)


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def _file(name, text):
    return (name, io.BytesIO(text.encode()), "text/csv")


def _three_files(qa=QA, client=CLIENT, pipeline=PIPELINE):
    files = {}
    if qa is not None:
        files["qa_file"] = _file("qa.csv", qa)
    if client is not None:
        files["ground_file"] = _file("client.csv", client)
    if pipeline is not None:
        files["system_file"] = _file("pipeline.csv", pipeline)
    return files


ALL_UPLOADS = {"ground_source": "upload", "system_source": "upload"}


def _statuses(comparison):
    return {row["pageNumber"]: row["status"] for row in comparison["rows"]}


def test_two_sources_without_qa_are_todays_compare(client):
    """No QA file: the response is the two-source body, unchanged, and the
    three-way engine's Client vs Pipeline reads exactly the same."""
    files = _three_files(qa=None)
    body = client.post("/api/excel-compare", files=files, data=ALL_UPLOADS).json()
    assert "comparisons" not in body
    classic = excel_compare.run((CLIENT.encode(), "client.csv"), (PIPELINE.encode(), "pipeline.csv"))
    same = excel_compare.three_way.run(
        {"qa": None, "client": (CLIENT.encode(), "client.csv"), "pipeline": (PIPELINE.encode(), "pipeline.csv")}
    )
    assert same["order"] == ["client_pipeline"]
    pair = same["comparisons"]["client_pipeline"]
    for key in (
        "groundCount", "systemCount", "correctCount", "errorCount", "missingCount", "extraCount",
        "unvalidatedCount", "matchedCount", "fields", "fieldErrors", "summary",
    ):
        assert pair[key] == classic[key] == body[key], key
    assert pair["rows"] == classic["rows"]
    assert pair["exemptedColumns"] == classic["exemptedColumns"]


def test_qa_against_client_ground_truth(client):
    res = client.post(
        "/api/excel-compare",
        files=_three_files(pipeline=None),
        data={"ground_source": "upload", "system_source": "none"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sources"] == ["qa", "client"]
    assert body["order"] == ["qa_client"] and body["defaultComparison"] == "qa_client"
    pair = body["comparisons"]["qa_client"]
    assert pair["sides"] == ["QA Ground Truth", "Client Ground Truth"]
    assert pair["fields"] == ["Extracted Name", "DOS From", "DOS To", "Codable / Non-Codable"]
    rows = {row["pageNumber"]: row for row in pair["rows"]}
    # Page 1: QA's "Yes" (a name is on the page) agrees with Client's name.
    # Page 2: "No" on both sheets agrees (a presence label is not a value).
    assert rows["1"]["status"] == "CORRECT" and rows["2"]["status"] == "CORRECT"
    # Page 3: codability differs; QA left the name unlabelled, so it is not scored.
    assert rows["3"]["status"] == "ERROR"
    assert rows["3"]["mismatched"] == ["Codable / Non-Codable"]


def test_qa_presence_no_against_a_client_name_is_a_mismatch():
    qa = "PDF ID,Page Number,Extracted Name\n100,1,No\n100,2,Not Found\n100,3,Yes\n"
    other = "PDF ID,Page Number,Extracted Name\n100,1,Lisa Anderson\n100,2,No\n100,3,No\n"
    body = excel_compare.three_way.run({"qa": (qa.encode(), "qa.csv"), "client": (other.encode(), "c.csv")})
    statuses = _statuses(body["comparisons"]["qa_client"])
    assert statuses == {"1": "ERROR", "2": "CORRECT", "3": "ERROR"}


def test_three_sources_every_pair_and_all_three(client):
    res = client.post("/api/excel-compare", files=_three_files(), data=ALL_UPLOADS)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sources"] == ["qa", "client", "pipeline"]
    assert body["order"] == ["qa_client", "qa_pipeline", "client_pipeline", "all"]
    comparisons = body["comparisons"]
    assert _statuses(comparisons["qa_client"]) == {"1": "CORRECT", "2": "CORRECT", "3": "ERROR"}
    assert _statuses(comparisons["qa_pipeline"]) == {"1": "CORRECT", "2": "ERROR", "3": "MISSING"}
    assert _statuses(comparisons["client_pipeline"]) == {"1": "CORRECT", "2": "ERROR", "3": "MISSING"}
    everything = comparisons["all"]
    assert everything["tokens"] == ["QA", "Client", "Pipeline"]
    # A page matches only where all three agree; QA's page 3 has no pipeline row.
    assert _statuses(everything) == {"1": "CORRECT", "2": "ERROR", "3": "MISSING"}
    page2 = next(row for row in everything["rows"] if row["pageNumber"] == "2")
    assert page2["mismatched"] == ["DOS From"]
    assert page2["values"]["DOS From"] == "QA: 2024-03-16 | Client: 2024-03-16 | Pipeline: 2024-03-17"
    assert "QA vs Pipeline" in page2["reason"] and "Client vs Pipeline" in page2["reason"]
    assert "QA vs Client" not in page2["reason"]
    page3 = next(row for row in everything["rows"] if row["pageNumber"] == "3")
    assert "Pipeline Output" in page3["reason"]
    assert (everything["groundCount"], everything["systemCount"], everything["correctCount"]) == (3, 2, 1)
    # One reportId covers every comparison.
    assert {c["reportId"] for c in comparisons.values()} == {body["reportId"]}


def test_all_three_needs_every_pair_to_agree():
    """QA = Pipeline and Client = Pipeline are not enough when QA and Client differ."""
    qa = "PDF ID,Page Number,Codable / Non-Codable,DOS From\n100,1,Codable,03/15/2024\n"
    other = "PDF ID,Page Number,Codable / Non-Codable,DOS From\n100,1,Non Codable,03/15/2024\n"
    pipeline = "PDF ID,Page Number,Codable / Non-Codable,DOS From\n100,1,Codable,2024-03-15\n"
    body = excel_compare.three_way.run({
        "qa": (qa.encode(), "qa.csv"), "client": (other.encode(), "c.csv"), "pipeline": (pipeline.encode(), "p.csv"),
    })
    row = body["comparisons"]["all"]["rows"][0]
    assert row["status"] == "ERROR" and row["mismatched"] == ["Codable / Non-Codable"]
    assert row["values"]["DOS From"] == "2024-03-15"


def test_fields_not_in_every_source_are_left_out_of_all_three():
    qa = "PDF ID,Page Number,DOS From,Duplicate\n100,1,03/15/2024,No\n"
    other = "PDF ID,Page Number,DOS From\n100,1,03/15/2024\n"
    pipeline = "PDF ID,Page Number,DOS From,Duplicate\n100,1,2024-03-15,No\n"
    body = excel_compare.three_way.run({
        "qa": (qa.encode(), "qa.csv"), "client": (other.encode(), "c.csv"), "pipeline": (pipeline.encode(), "p.csv"),
    })
    assert body["comparisons"]["qa_pipeline"]["fields"] == ["DOS From", "Duplicate"]
    assert body["comparisons"]["all"]["fields"] == ["DOS From"]
    reasons = {e["column"]: e["reason"] for e in body["comparisons"]["all"]["exemptedColumns"]}
    assert "every pair" in reasons["Duplicate"]


@pytest.mark.parametrize(
    "files, data",
    [
        ({"qa_file": _file("qa.csv", QA)}, {"ground_source": "none", "system_source": "none"}),
        ({}, {"ground_source": "db", "system_source": "none"}),
        ({}, {"ground_source": "none", "system_source": "db"}),
    ],
)
def test_fewer_than_two_sources_is_a_400(client, files, data):
    res = client.post("/api/excel-compare", files=files or None, data=data)
    assert res.status_code == 400
    assert "at least two sources" in res.json()["detail"]
    stream = client.post("/api/excel-compare/stream", files=files or None, data=data)
    assert stream.status_code == 400


def test_an_unknown_source_is_a_400(client):
    res = client.post("/api/excel-compare", files=_three_files(), data={"ground_source": "x"})
    assert res.status_code == 400


def _events(res):
    return [json.loads(line) for line in res.text.splitlines() if line.strip()]


def test_three_sources_stream_matches_the_whole_compare(client):
    whole = client.post("/api/excel-compare", files=_three_files(), data=ALL_UPLOADS).json()
    events = _events(client.post("/api/excel-compare/stream", files=_three_files(), data=ALL_UPLOADS))
    assert [e["type"] for e in events] == ["start", "chart", "done"]
    start, chart, done = events
    assert [c["key"] for c in start["comparisons"]] == whole["order"]
    for key in whole["order"]:
        assert chart["comparisons"][key]["rows"] == whole["comparisons"][key]["rows"]
        for count in ("correctCount", "errorCount", "missingCount", "summary", "fieldErrors"):
            assert done["comparisons"][key][count] == whole["comparisons"][key][count]


def test_report_and_summary_include_every_comparison(client):
    openpyxl = pytest.importorskip("openpyxl")
    pytest.importorskip("reportlab")
    body = client.post("/api/excel-compare", files=_three_files(), data=ALL_UPLOADS).json()
    report = client.post(
        "/api/excel-compare/report", files=_three_files(), data={**ALL_UPLOADS, "report_id": body["reportId"]}
    )
    assert report.status_code == 200, report.text
    book = openpyxl.load_workbook(io.BytesIO(report.content))
    names = book.sheetnames
    assert names[0] == "Overview"
    for prefix in ("QA-Client", "QA-Pipeline", "Client-Pipeline", "All 3"):
        assert f"{prefix} Results" in names and f"{prefix} Summary" in names
    overview = [[cell.value for cell in row] for row in book["Overview"].iter_rows()]
    assert overview[0] == ["Metric", "QA vs Client", "QA vs Pipeline", "Client vs Pipeline", "All 3"]
    # A pair's cells name its sources in the download, not GT / Algo.
    cells = [cell.value for row in book["QA-Pipeline Results"].iter_rows() for cell in row]
    assert "QA: 2024-03-16 | Pipeline: 2024-03-17" in cells
    assert not any(isinstance(value, str) and value.startswith("GT: ") for value in cells)

    summary = client.post(
        "/api/excel-compare/summary", files=_three_files(), data={**ALL_UPLOADS, "report_id": body["reportId"]}
    )
    assert summary.status_code == 200, summary.text
    assert summary.content[:4] == b"%PDF"
    pypdf = pytest.importorskip("pypdf")
    text = " ".join(
        " ".join(page.extract_text() for page in pypdf.PdfReader(io.BytesIO(summary.content)).pages).split()
    )
    assert "Comparisons at a glance" in text
    for title in ("QA vs Client", "QA vs Pipeline", "Client vs Pipeline", "All 3"):
        assert f"Master Summary — {title}" in text


def test_downloads_compare_again_without_a_report_id(client):
    openpyxl = pytest.importorskip("openpyxl")
    report = client.post("/api/excel-compare/report", files=_three_files(), data=ALL_UPLOADS)
    assert report.status_code == 200, report.text
    assert "All 3 Results" in openpyxl.load_workbook(io.BytesIO(report.content)).sheetnames


def test_two_source_report_keeps_its_sheets(client):
    openpyxl = pytest.importorskip("openpyxl")
    report = client.post("/api/excel-compare/report", files=_three_files(qa=None), data=ALL_UPLOADS)
    names = openpyxl.load_workbook(io.BytesIO(report.content)).sheetnames
    assert names == ["Comparison Results", "Summary", "Exempted Columns", "Excluded Records"]


class _Repo:
    def __init__(self):
        from app.core.schemas import ImagingDocumentResponse, ImagingPageResult

        pages = [
            ImagingPageResult(pageNumber=1, fileName="1.png", dosFrom="2024-03-15", dosTo="2024-03-15",
                              isCodeable="Codable", blankOrJunk="No", isDuplicate=False),
            ImagingPageResult(pageNumber=2, fileName="2.png", dosFrom="2024-03-17", dosTo="2024-03-16",
                              isCodeable="Non-Codable", blankOrJunk="No", isDuplicate=False),
        ]
        self.doc = ImagingDocumentResponse.model_construct(pages=pages, verifications=[], verification=None)
        self.asked = []

    def list_folders(self):
        raise AssertionError("Manual Review read the full folder list")


def test_database_client_and_pipeline_cover_the_qa_charts(client, monkeypatch):
    import app.routers.excel_compare as routes
    from app.services.excel_compare import database_algo
    from app.store.factory import get_repository

    fake = _Repo()
    client.app.dependency_overrides[get_repository] = lambda: fake
    asked = {}

    def fetch(_url, _schema, names):
        fake.asked.extend(names)
        return {name: fake.doc for name in names if name == "100"}, {}

    def labels(_url, charts, source, _schema):
        asked["charts"] = charts
        return [
            ("100", 1, {"dos_from": "03/15/2024", "dos_to": "03/15/2024", "codeable": "Codable"}),
            ("100", 2, {"dos_from": "03/16/2024", "dos_to": "03/16/2024", "codeable": "Non Codable"}),
        ]

    monkeypatch.setattr(routes, "database_url_usable", lambda _url: True)
    monkeypatch.setattr(routes, "ground_truth_labels", labels)
    monkeypatch.setattr(routes, "ground_truth_charts", lambda *_args: pytest.fail("every chart was read"))
    monkeypatch.setattr(database_algo, "fetch", fetch)

    qa = QA.replace("100,3", "200,3")
    res = client.post(
        "/api/excel-compare",
        files={"qa_file": _file("qa.csv", qa)},
        data={"ground_source": "db", "system_source": "db"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # Client ground truth for the QA file's charts; the pipeline for every ground-truth chart.
    assert asked["charts"] == ["100", "200"]
    assert sorted(set(fake.asked)) == ["100", "200"]
    assert body["order"] == ["qa_client", "qa_pipeline", "client_pipeline", "all"]
    assert _statuses(body["comparisons"]["client_pipeline"]) == {"1": "CORRECT", "2": "ERROR"}
    assert _statuses(body["comparisons"]["all"]) == {"1": "CORRECT", "2": "ERROR", "3": "MISSING"}
