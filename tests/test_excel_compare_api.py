"""review-ui's Manual Review endpoints: uploaded ground truth against an uploaded
system sheet or the pipeline output; JSON or a workbook out."""

import io

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("pandas")
pytest.importorskip("httpx")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import router

GROUND = (
    "PDF Id,Page No.,DOS From,DoS To,Codable or Non Codable\n"
    "100,1,03/15/2024,03/15/2024,Codable\n"
    "100,2,03/16/2024,03/16/2024,Non Codable\n"
    "100,3,-,-,Non Codable\n"
)
SYSTEM = (
    "Pdf id,Page number,DOS From,DOS To,Codable or Non-Codable\n"
    "100,1,2024-03-15,2024-03-15,Codable\n"
    "100,2,2024-03-17,2024-03-16,Non-Codable\n"
    "100,4,-,-,Codable\n"
)


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def _files(ground=GROUND, system=SYSTEM, ground_name="ground.csv"):
    return {
        "ground_file": (ground_name, io.BytesIO(ground.encode()), "text/csv"),
        "system_file": ("system.csv", io.BytesIO(system.encode()), "text/csv"),
    }


UPLOAD = {"system_source": "upload"}


def test_compare_returns_rows_with_the_mismatched_fields(client):
    res = client.post("/api/excel-compare", files=_files(), data=UPLOAD)
    assert res.status_code == 200
    body = res.json()
    assert (body["correctCount"], body["errorCount"], body["missingCount"], body["extraCount"]) == (1, 1, 1, 0)
    assert body["fields"] == ["DOS From", "DOS To", "Codable / Non-Codable"]
    error = next(row for row in body["rows"] if row["status"] == "ERROR")
    assert error["pdfId"] == "100" and error["pageNumber"] == "2"
    assert error["mismatched"] == ["DOS From"]
    assert error["values"]["DOS From"] == "GT: 03/16/2024 | Algo: 03/17/2024"


def test_report_is_an_xlsx_workbook(client):
    res = client.post("/api/excel-compare/report", files=_files(), data=UPLOAD)
    assert res.status_code == 200
    assert res.content[:2] == b"PK"
    assert "manual_review_report.xlsx" in res.headers["content-disposition"]


def test_a_file_without_the_key_columns_is_a_400(client):
    res = client.post("/api/excel-compare", files=_files(ground="Name,DOB\nA,B\n"), data=UPLOAD)
    assert res.status_code == 400
    assert "missing required column" in res.json()["detail"]


def test_an_unsupported_file_type_is_a_400(client):
    res = client.post("/api/excel-compare", files=_files(ground_name="ground.pdf"), data=UPLOAD)
    assert res.status_code == 400


def _ground_only(ground=GROUND):
    return {"ground_file": ("ground.csv", io.BytesIO(ground.encode()), "text/csv")}


class _Repo:
    """Chart 100 with three pages of pipeline output; chart 200 has none yet."""

    def __init__(self):
        from app.core.schemas import FolderSummary, ImagingDocumentResponse, ImagingPageResult

        self.asked = []
        from datetime import datetime, timezone

        self.folders = [
            FolderSummary(id="f100", name="100", last_updated_at=datetime(2026, 10, 14, 8, 8, tzinfo=timezone.utc)),
            FolderSummary(id="f200", name="200"),
        ]
        pages = [
            ImagingPageResult(pageNumber=1, fileName="1.png", dosFrom="2024-03-15", dosTo="2024-03-15",
                              isCodeable="Codable", pageType="Progress Note (Office Visit)",
                              encounterType="Outpatient (F2F)", providerSignature="Yes",
                              blankOrJunk="No", isDuplicate=False),
            ImagingPageResult(pageNumber=2, fileName="2.png", dosFrom="2024-03-17", dosTo="2024-03-16",
                              isCodeable="Non-Codable", blankOrJunk="Yes (Junk)"),
            ImagingPageResult(pageNumber=4, fileName="4.png", isCodeable="Codable"),
        ]
        self.doc = ImagingDocumentResponse.model_construct(pages=pages, verifications=[], verification=None)

    def list_folders(self):
        return self.folders

    def get_imaging(self, folder_id):
        from fastapi import HTTPException

        self.asked.append(folder_id)
        if folder_id != "f100":
            raise HTTPException(status_code=404, detail="no imaging")
        return self.doc


@pytest.fixture()
def repo(client):
    from app.adapters.factory import get_repository

    fake = _Repo()
    client.app.dependency_overrides[get_repository] = lambda: fake
    return fake


def test_system_output_defaults_to_the_pipeline(client, repo):
    ground = GROUND.replace("100,3", "200,3")
    res = client.post("/api/excel-compare", files=_ground_only(ground))
    assert res.status_code == 200, res.text
    body = res.json()
    # Only the ground truth's PDF IDs are read; chart 200 has no output yet.
    assert repo.asked == ["f100", "f200"]
    assert (body["groundCount"], body["systemCount"]) == (3, 2)
    assert (body["correctCount"], body["errorCount"], body["missingCount"], body["extraCount"]) == (1, 1, 1, 0)
    error = next(row for row in body["rows"] if row["status"] == "ERROR")
    assert error["mismatched"] == ["DOS From"]


def test_pipeline_output_as_the_system_csv():
    import csv

    from app.services.excel_compare.database_system import system_csv

    rows = list(csv.DictReader(io.StringIO(system_csv([("100", _Repo().doc)]).decode())))
    first, second, fourth = rows
    # Page Type is the encounter type; the classifier's sub-type is Page Subtype.
    assert (first["Page Type"], first["Page Subtype"]) == ("Outpatient (F2F)", "Office Visit")
    assert (first["Provider Signature"], second["Provider Signature"]) == ("Yes", "")
    assert (first["Is Blank or Junk?"], first["Duplicate"]) == ("No", "No")
    assert second["Is Blank or Junk?"] == "Yes"
    # A page with no DOS carries the default Download Results ships.
    assert (fourth["DOS From"], fourth["DOS To"]) == ("2222-02-02", "2222-02-02")


def test_no_system_output_for_the_pdf_ids_is_a_400(client, repo):
    res = client.post("/api/excel-compare", files=_ground_only(GROUND.replace("100,", "999,")))
    assert res.status_code == 400
    assert "PDF ID must be the chart name" in res.json()["detail"]


def test_upload_mode_needs_the_ground_truth_file(client, repo):
    res = client.post("/api/excel-compare", data=UPLOAD)
    assert res.status_code == 400
    assert res.json()["detail"] == "Ground Truth file is required"


_DB_LABELS = [
    ("100", 1, {"dos_from": "03/15/2024", "dos_to": "03/15/2024", "codeable": "Codable"}),
    ("100", 2, {"dos_from": "03/17/2024", "dos_to": "03/16/2024", "codeable": "Non Codable"}),
]


def _database_ground(monkeypatch, asked):
    import app.api.routes as routes

    def labels(_url, charts, source, _schema):
        asked["charts"], asked["source"] = charts, source
        return _DB_LABELS

    monkeypatch.setattr(routes, "database_url_usable", lambda _url: True)
    monkeypatch.setattr(routes, "ground_truth_labels", labels)
    monkeypatch.setattr(routes, "ground_truth_charts", lambda *_args: ["100", "300"])


def test_ground_truth_from_the_database_for_the_system_file(client, monkeypatch):
    asked = {}
    _database_ground(monkeypatch, asked)
    res = client.post(
        "/api/excel-compare",
        files={"system_file": ("system.csv", io.BytesIO(SYSTEM.encode()), "text/csv")},
        data={"ground_source": "db", "ground_truth": "bulk_upload", "system_source": "upload"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # Only the PDF IDs in the system file are looked up.
    assert asked == {"charts": ["100"], "source": "bulk_upload"}
    assert (body["groundCount"], body["correctCount"], body["extraCount"]) == (2, 2, 0)


def test_both_sides_from_the_database(client, repo, monkeypatch):
    asked = {}
    _database_ground(monkeypatch, asked)
    res = client.post("/api/excel-compare", data={"ground_source": "db"})
    assert res.status_code == 200, res.text
    body = res.json()
    # Every chart with ground truth, then the pipeline output for the labelled ones.
    assert asked == {"charts": ["100", "300"], "source": "latest"}
    assert repo.asked == ["f100"]
    assert (body["groundCount"], body["systemCount"]) == (2, 2)
    assert (body["correctCount"], body["errorCount"], body["extraCount"]) == (2, 0, 0)


def test_database_ground_truth_needs_a_database(client, monkeypatch):
    import app.api.routes as routes

    monkeypatch.setattr(routes, "database_url_usable", lambda _url: False)
    res = client.post("/api/excel-compare", data={"ground_source": "db"})
    assert res.status_code == 503


def test_upload_mode_needs_the_system_file(client):
    res = client.post("/api/excel-compare", files=_ground_only(), data=UPLOAD)
    assert res.status_code == 400
    assert res.json()["detail"] == "Algo Generated file is required"


def test_columns_lists_what_the_system_file_needs(client):
    body = client.get("/api/excel-compare/columns").json()
    assert [c["column"] for c in body["required"]] == ["PDF ID", "Page Number"]
    assert "DOS From" in [c["column"] for c in body["compared"]]
    assert "Orient. (CW)" in body["exempt"]


def _events(res):
    import json

    return [json.loads(line) for line in res.text.splitlines() if line.strip()]


def test_stream_sends_one_chart_at_a_time(client, repo):
    ground = GROUND.replace("100,3", "200,3")
    res = client.post("/api/excel-compare/stream", files=_ground_only(ground))
    assert res.status_code == 200, res.text
    events = _events(res)
    assert [e["type"] for e in events] == ["start", "chart", "chart", "done"]
    start, first, second, done = events
    assert start["charts"] == 2 and start["fields"] == ["DOS From", "DOS To", "Codable / Non-Codable"]
    # Chart 100 against its pipeline output, then 200, which has none: all Missing.
    assert first["pdfId"] == "100" and len(first["rows"]) == 2
    assert [row["status"] for row in second["rows"]] == ["MISSING"]
    # Each chart carries its last pipeline run; one never run has none.
    assert (first["lastRunAt"], second["lastRunAt"]) == ("2026-10-14T08:08:00+00:00", None)
    assert (done["correctCount"], done["errorCount"], done["missingCount"], done["extraCount"]) == (1, 1, 1, 0)
    assert any(item["metric"] == "Match Percentage" for item in done["summary"])
    # The pipeline is read chart by chart, as the stream reaches each one.
    assert repo.asked == ["f100", "f200"]


def test_stream_with_an_uploaded_algo_file_matches_the_whole_compare(client):
    whole = client.post("/api/excel-compare", files=_files(), data=UPLOAD).json()
    done = _events(client.post("/api/excel-compare/stream", files=_files(), data=UPLOAD))[-1]
    for count in ("groundCount", "systemCount", "correctCount", "errorCount", "missingCount", "extraCount"):
        assert done[count] == whole[count]


def test_stream_bad_headers_fail_before_the_stream(client, repo):
    res = client.post("/api/excel-compare/stream", files=_ground_only("Name,DOB\nA,B\n"))
    assert res.status_code == 400
    assert "missing required column" in res.json()["detail"]


def test_a_chart_header_is_the_pdf_id(client):
    ground = GROUND.replace("PDF Id", "Chart Name")
    body = client.post("/api/excel-compare", files=_files(ground=ground), data=UPLOAD).json()
    assert (body["correctCount"], body["errorCount"], body["missingCount"], body["extraCount"]) == (1, 1, 1, 0)


def test_database_ground_truth_page_type_is_the_encounter_type():
    import csv

    from app.services.excel_compare.database_ground import ground_csv

    rows = list(csv.DictReader(io.StringIO(ground_csv([
        ("100", 1, {"encounter_type": "Inpatient", "page_type": "Outpatient (F2F)", "provider_signature": "No"}),
        ("100", 2, {"page_type": "Outpatient (F2F)"}),
    ]).decode())))
    assert [row["Page Type"] for row in rows] == ["Inpatient", "Outpatient (F2F)"]
    assert rows[0]["Provider Signature"] == "No"
