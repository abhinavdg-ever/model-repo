"""review-ui's Excel Compare endpoints: two uploads in, JSON or a workbook out."""

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


def test_compare_returns_rows_with_the_mismatched_fields(client):
    res = client.post("/api/excel-compare", files=_files())
    assert res.status_code == 200
    body = res.json()
    assert (body["correctCount"], body["errorCount"], body["missingCount"], body["extraCount"]) == (1, 1, 1, 1)
    assert body["fields"] == ["DOS From", "DOS To", "Codable / Non-Codable"]
    error = next(row for row in body["rows"] if row["status"] == "ERROR")
    assert error["pdfId"] == "100" and error["pageNumber"] == "2"
    assert error["mismatched"] == ["DOS From"]
    assert error["values"]["DOS From"] == "GT: 03/16/2024 | System: 2024-03-17"


def test_report_is_an_xlsx_workbook(client):
    res = client.post("/api/excel-compare/report", files=_files())
    assert res.status_code == 200
    assert res.content[:2] == b"PK"
    assert "comparison_report.xlsx" in res.headers["content-disposition"]


def test_a_file_without_the_key_columns_is_a_400(client):
    res = client.post("/api/excel-compare", files=_files(ground="Name,DOB\nA,B\n"))
    assert res.status_code == 400
    assert "missing required column" in res.json()["detail"]


def test_an_unsupported_file_type_is_a_400(client):
    res = client.post("/api/excel-compare", files=_files(ground_name="ground.pdf"))
    assert res.status_code == 400


def _system_only():
    return {"system_file": ("system.csv", io.BytesIO(SYSTEM.encode()), "text/csv")}


def test_ground_truth_can_come_from_the_database(client, monkeypatch):
    import app.api.routes as routes

    asked = {}

    def labels(_url, charts, source, _schema):
        asked["charts"], asked["source"] = charts, source
        return [
            ("100", 1, {"dos_from": "03/15/2024", "dos_to": "03/15/2024", "codeable": "Codable"}),
            ("100", 2, {"dos_from": "03/17/2024", "dos_to": "03/16/2024", "codeable": "Non Codable"}),
        ]

    monkeypatch.setattr(routes, "database_url_usable", lambda _url: True)
    monkeypatch.setattr(routes, "ground_truth_labels", labels)
    res = client.post(
        "/api/excel-compare",
        files=_system_only(),
        data={"ground_source": "db", "ground_truth": "bulk_upload"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    # Only the PDF IDs in the system file are looked up.
    assert asked == {"charts": ["100"], "source": "bulk_upload"}
    assert (body["groundCount"], body["correctCount"], body["extraCount"]) == (2, 2, 1)


def test_no_database_ground_truth_for_the_pdf_ids_is_a_400(client, monkeypatch):
    import app.api.routes as routes

    monkeypatch.setattr(routes, "database_url_usable", lambda _url: True)
    monkeypatch.setattr(routes, "ground_truth_labels", lambda *_args: [])
    res = client.post("/api/excel-compare", files=_system_only(), data={"ground_source": "db"})
    assert res.status_code == 400
    assert "PDF ID must be the chart name" in res.json()["detail"]


def test_upload_mode_needs_the_ground_truth_file(client):
    res = client.post("/api/excel-compare", files=_system_only())
    assert res.status_code == 400
    assert res.json()["detail"] == "Ground Truth file is required"


def test_columns_lists_what_the_system_file_needs(client):
    body = client.get("/api/excel-compare/columns").json()
    assert [c["column"] for c in body["required"]] == ["PDF ID", "Page Number"]
    assert "DOS From" in [c["column"] for c in body["compared"]]
    assert "Orient. (CW)" in body["exempt"]
