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

        # The light chart list Last Run At reads (build_folder_list).
        self.folders = [
            FolderSummary(id="100", name="100", last_updated_at=datetime(2026, 10, 14, 8, 8, tzinfo=timezone.utc)),
            FolderSummary(id="200", name="200"),
        ]
        pages = [
            ImagingPageResult(pageNumber=1, fileName="1.png", dosFrom="2024-03-15", dosTo="2024-03-15",
                              isCodeable="Codable", pageType="Progress Note (Office Visit)",
                              encounterType="Outpatient (F2F)", providerCredentials="MD",
                              blankOrJunk="No", isDuplicate=False),
            ImagingPageResult(pageNumber=2, fileName="2.png", dosFrom="2024-03-17", dosTo="2024-03-16",
                              isCodeable="Non-Codable", blankOrJunk="Yes (Junk)"),
            ImagingPageResult(pageNumber=4, fileName="4.png", isCodeable="Codable"),
        ]
        self.doc = ImagingDocumentResponse.model_construct(pages=pages, verifications=[], verification=None)

    def list_folders(self):
        # Local Mode scans every page of every chart here: Manual Review must not call it.
        raise AssertionError("Manual Review read the full folder list")

    def get_imaging(self, folder_id):
        from fastapi import HTTPException

        self.asked.append(folder_id)
        if folder_id != "100":
            raise HTTPException(status_code=404, detail="no imaging")
        return self.doc


@pytest.fixture()
def repo(client):
    from app.adapters.factory import get_repository

    from types import SimpleNamespace

    import app.api.routes as routes

    from app.services.excel_compare import database_algo

    fake = _Repo()
    client.app.dependency_overrides[get_repository] = lambda: fake

    def fetch(_url, _schema, names):
        # The batched database read, served from the same fake charts.
        fake.asked.extend(names)
        docs = {name: fake.doc for name in names if name == "100"}
        runs = {f.name: f.last_updated_at for f in fake.folders if f.name in names}
        return docs, runs

    saved = routes.build_folder_list, database_algo.fetch
    routes.build_folder_list = lambda **_kwargs: SimpleNamespace(items=fake.folders)
    database_algo.fetch = fetch
    yield fake
    routes.build_folder_list, database_algo.fetch = saved


def test_system_output_defaults_to_the_pipeline(client, repo):
    ground = GROUND.replace("100,3", "200,3")
    res = client.post("/api/excel-compare", files=_ground_only(ground))
    assert res.status_code == 200, res.text
    body = res.json()
    # Only the ground truth's PDF IDs are read; chart 200 has no output yet.
    assert repo.asked == ["100", "200"]
    assert (body["groundCount"], body["systemCount"]) == (3, 2)
    assert (body["correctCount"], body["errorCount"], body["missingCount"], body["extraCount"]) == (1, 1, 1, 0)
    error = next(row for row in body["rows"] if row["status"] == "ERROR")
    assert error["mismatched"] == ["DOS From"]


def test_pipeline_output_as_the_system_csv():
    import csv

    from app.services.excel_compare.database_system import system_csv

    rows = list(csv.DictReader(io.StringIO(system_csv([("100", _Repo().doc)]).decode())))
    first, second, fourth = rows
    # Page Type / Subtype are the classifier's, not the encounter type.
    assert (first["Page Type"], first["Page Subtype"]) == ("Progress Note", "Office Visit")
    assert (first["Provider Credentials"], second["Provider Credentials"]) == ("MD", "")
    assert (first["Is Blank or Junk?"], first["Duplicate"]) == ("No", "No")
    assert second["Is Blank or Junk?"] == "Yes"
    # A page with no DOS stays blank (no 2222-02-02 default).
    assert (fourth["DOS From"], fourth["DOS To"]) == ("", "")


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
    assert repo.asked == ["100"]
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
    assert repo.asked == ["100", "200"]


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


def test_database_ground_truth_page_type_is_encounter_type_only():
    import csv

    from app.services.excel_compare.database_ground import ground_csv

    rows = list(csv.DictReader(io.StringIO(ground_csv([
        ("100", 1, {"encounter_type": "Inpatient", "page_type": "Outpatient (F2F)", "provider_credentials": "MD"}),
        # An older row kept credentials in provider_signature; a bare Yes / No there is not one.
        ("100", 2, {"page_type": "Outpatient (F2F)", "provider_signature": "D.O."}),
        ("100", 3, {"provider_signature": "Yes"}),
    ]).decode())))
    # encounter_type only; page_type is ignored.
    assert [row["Page Type"] for row in rows] == ["Inpatient", "", ""]
    assert [row["Provider Credentials"] for row in rows] == ["MD", "D.O.", ""]


def test_pipeline_final_values_win_over_extracted():
    import csv

    from app.core.schemas import ImagingDocumentResponse, ImagingPageResult
    from app.services.excel_compare.database_system import system_csv

    page = ImagingPageResult(
        pageNumber=3, fileName="3.png",
        dosFrom="2024-01-01", dosTo="2024-01-01", finalDosFrom="2024-02-02",
        isCodeable="Non-Codable", finalCodeable="Codable",
        pageType="Lab Report", finalPageType="Progress Note (Office Visit)",
    )
    doc = ImagingDocumentResponse.model_construct(pages=[page], verifications=[], verification=None)
    (row,) = csv.DictReader(io.StringIO(system_csv([("100", doc)]).decode()))
    # As the Accuracy tab reads them: Final where the run has it, else extracted.
    assert (row["DOS From"], row["DOS To"]) == ("2024-02-02", "2024-02-02")
    assert row["Codable / Non-Codable"] == "Codable"
    assert row["Page Subtype"] == "Office Visit"


def test_unlabelled_and_presence_ground_truth():
    from app.services.excel_compare.field_comparator import compare_fields

    def compare(field, gt, algo):
        return compare_fields({field: gt}, {field: algo}, [field])[field]

    # A blank cell is "not labelled": not an error, both sides shown.
    assert compare("DOS From", "", "2025-08-27") == {"match": True, "display": "GT:  | Algo: 08/27/2025"}
    # "-" is a label ("no date") and is compared.
    assert compare("DOS From", "-", "2025-08-27")["match"] is False
    # Yes / No member labels are presence: is a value on the page?
    assert compare("Member ID", "Yes", "500-50")["match"] is True
    assert compare("Member ID", "Yes", "")["match"] is False
    assert compare("Member ID", "No", "")["match"] is True
    assert compare("Member ID", "500-50", "500-51")["match"] is False


def test_credentials_match_as_the_accuracy_tab_scores_them():
    from app.services.excel_compare.field_comparator import compare_fields

    def compare(gt, algo):
        result = compare_fields({"Provider Credentials": gt}, {"Provider Credentials": algo}, ["Provider Credentials"])
        return result["Provider Credentials"]

    assert compare("M.D., Ph.D.", "PhD MD") == {"match": True, "display": "GT: M.D., Ph.D. | Algo: PhD MD"}
    assert compare("MD", "MD")["display"] == "MD"
    assert compare("MD", "MD, PhD")["match"] is True  # some shared counts
    assert compare("MD", "NP")["match"] is False
    assert compare("NA", "")["match"] is True
    assert compare("MD", "")["match"] is False


def test_a_page_ground_truth_export_uploads_as_ground_truth(client):
    # The columns of a page_ground_truth export: page_type and encounter_type
    # both name Page Type, and blank / junk are two columns.
    ground = (
        "id,chart_name,page_number,member_id,dos_from,dos_to,encounter_type,page_type,"
        "codeable,blank_page,junk_page,provider_signature,provider_credentials\n"
        "1,100,1,,03/15/2024,03/15/2024,,Progress Note,Codable,No,No,,MD\n"
        "2,100,2,,03/16/2024,03/16/2024,Inpatient,Progress Note,Non Codable,No,Yes,,\n"
    )
    algo = (
        "PDF ID,Page Number,DOS From,DOS To,Page Type,Codable / Non-Codable,Is Blank or Junk?,Provider Credentials\n"
        "100,1,2024-03-15,2024-03-15,Progress Note,Codable,No,M.D.\n"
        "100,2,2024-03-16,2024-03-16,Inpatient,Non-Codable,Yes,\n"
    )
    res = client.post("/api/excel-compare", files=_files(ground=ground, system=algo), data=UPLOAD)
    assert res.status_code == 200, res.text
    body = res.json()
    assert "Page Type" in body["fields"] and "Is Blank or Junk?" in body["fields"]
    # Page 1: encounter_type is blank -> Page Type unlabelled (page_type is ignored).
    # Page 2: encounter_type "Inpatient" is the Page Type ground truth.
    assert (body["correctCount"], body["errorCount"]) == (2, 0)


def test_offline_ground_truth_page_type_is_encounter_type_only():
    import pandas as pd

    from app.services.excel_compare.header_normalizer import normalize_headers

    frame = normalize_headers(pd.DataFrame({
        "chart_name": ["c", "c"], "page_number": [1, 2],
        "page_type": ["Progress Note", "Lab Report"], "encounter_type": ["Inpatient", None],
    }))
    assert list(frame["Page Type"].fillna("")) == ["Inpatient", ""]
    # Without an encounter_type column, Page Type is the page type column as before.
    plain = normalize_headers(pd.DataFrame({"PDF ID": ["c"], "Page Number": [1], "page type": ["Lab Report"]}))
    assert list(plain["Page Type"]) == ["Lab Report"]


def test_an_uploaded_page_ground_truth_mirror_reads_like_the_database(client, repo):
    ground = (
        "chart_name,page_number,member_id,dos_from,dos_to,encounter_type,page_type,codeable,"
        "blank_page,junk_page,provider_signature,provider_credentials\n"
        "100,1,Yes,03/15/2024,03/15/2024,,Lab Report,Codable,No,No,,MD\n"
        "100,2,,03/17/2024,03/16/2024,,,Non Codable,No,No,,\n"
    )
    res = client.post("/api/excel-compare", files=_ground_only(ground))
    assert res.status_code == 200, res.text
    body = res.json()
    # page_type is ignored and encounter_type is blank: no Page Type column.
    assert "Page Type" not in body["fields"]
    # Member ID "Yes" is presence (none extracted); page 2's blank / junk "No"
    # against the pipeline's junk page. Everything else agrees.
    mismatched = {row["pageNumber"]: row["mismatched"] for row in body["rows"]}
    assert mismatched == {"1": ["Member ID"], "2": ["Is Blank or Junk?"]}


def test_batched_database_rows_map_like_the_chart_view():
    from datetime import date

    from app.services.excel_compare.database_algo import _page

    def row(**cells):
        base = dict(
            chart="100", page_number=3, page_name="3.jpg",
            name=None, dob=None, member_id=None, dos_from=None, dos_to=None,
            flag="not_blank_junk", junk_subtype=None,
            classified=False, page_type=None, subtype=None, category=None,
            final_type=None, final_subtype=None, final_category=None, final_from=None, final_to=None,
            sequenced=False, current=None, actual=None,
            provider=None, signed=None, credentials=None, signers=None, version=None,
        )
        base.update(cells)
        return tuple(base.values())

    classified = _page(row(
        classified=True, page_type="Progress Note", subtype="Office Visit", category="codeable",
        dos_from=date(2025, 8, 27), final_from=date(2025, 9, 1), sequenced=True, current=3, actual=5,
        flag="duplicate",
    ))
    assert classified.pageType == "Progress Note (Office Visit)"
    assert (classified.isCodeable, classified.isDuplicate, classified.blankOrJunk) == ("Codable", True, "No")
    assert (classified.dosFrom, classified.finalDosFrom) == ("2025-08-27", "2025-09-01")
    assert (classified.currentSequence, classified.actualSequence) == (3, 5)
    # A main page with no classification row reads Not Sure, as get_imaging stamps it.
    assert _page(row()).isCodeable == "Not Sure"
    # A classified page with a blank category is Not Sure too; a junk page keeps its tag.
    assert _page(row(classified=True, page_type="Lab", subtype="Lab")).isCodeable == "Not Sure"
    assert _page(row(flag="junk", junk_subtype="Fax Cover")).blankOrJunk == "Yes (Junk)"
    # Provider: imaging_final's signature row, name and credentials split.
    signed = _page(row(provider="Kenneth Kummerfeld, MD", signed=True))  # version 1: split
    assert (signed.providerName, signed.providerCredentials, signed.providerSignature) == (
        "Kenneth Kummerfeld", "MD", "Yes")
