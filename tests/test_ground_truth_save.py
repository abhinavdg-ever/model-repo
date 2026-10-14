"""Ground-truth labels the imaging screen sends are stored as empty or text."""

from app.services.ground_truth import (
    LABEL_COLUMNS,
    _labelled_row_sql,
    clean_label,
    yes_no_label,
)


def test_a_chart_counts_once_any_one_label_cell_is_filled():
    sql = _labelled_row_sql()
    for column in LABEL_COLUMNS:
        assert f"->> '{column}'" in sql
    assert sql.count(" OR ") == len(LABEL_COLUMNS) - 1
    assert "'na'" in sql and "''" in sql


def test_blank_and_na_are_empty():
    assert clean_label(None) is None
    assert clean_label("  ") is None
    assert clean_label("NA") is None
    assert clean_label("n/a") is None
    assert clean_label("Not Found") is None


def test_a_typed_value_is_kept():
    assert clean_label(" Yes ") == "Yes"
    assert clean_label("Progress Notes") == "Progress Notes"


def test_signature_keeps_only_yes_or_no():
    assert yes_no_label("Yes") == "Yes"
    assert yes_no_label("Liane Kirchberger, PA") is None


def test_member_fields_keep_only_yes_or_no():
    assert yes_no_label(" yes ") == "Yes"
    assert yes_no_label("No") == "No"
    assert yes_no_label("Priya Nandakumar") is None
    assert yes_no_label("07/17/2025") is None
    assert yes_no_label("500-50") is None
    assert yes_no_label("NA") is None


def test_a_manual_save_records_exactly_the_columns_it_sets():
    """The manual_entry labels pair each saved value with its column, in the
    upsert's parameter order."""
    import re

    from app.services.ground_truth import _MANUAL_COLUMNS, _UPSERT

    insert = re.search(r"INSERT INTO page_ground_truth \((.*?)\)", _UPSERT, re.S)
    columns = [c.strip() for c in insert.group(1).split(",")]
    assert tuple(columns[3:]) == _MANUAL_COLUMNS
    assert "'manual_entry'" in _UPSERT


def test_bulk_ground_truth_is_read_from_the_entry_labels():
    from app.services.ground_truth import _ground_truth_from_labels

    truth = _ground_truth_from_labels(
        3, "3.jpg", {"rotation": "90", "codeable": "non codeable", "member_name": None}
    )
    assert truth.pageNumber == 3
    assert truth.rotation == "90"
    assert truth.codeable == "Non-Codable"
    assert truth.memberName is None


def test_bulk_mode_swaps_in_bulk_labels_and_drops_manual_only_pages():
    from app.services.accuracy_cache import _GT_CELLS, _GT_UPDATED_AT, _with_bulk_labels

    row = tuple(["chart", 1] + ["manual"] * 41)
    manual_only = tuple(["chart", 2] + ["manual"] * 41)
    out = _with_bulk_labels(
        [row, manual_only], {("chart", 1): ({"rotation": "270", "member_name": None}, "loaded")}
    )
    assert len(out) == 1
    assert out[0][_GT_CELLS["rotation"]] == "270"
    assert out[0][_GT_CELLS["member_name"]] is None
    # A column the load never set is not the manual value either.
    assert out[0][_GT_CELLS["provider_credentials"]] is None
    assert out[0][_GT_UPDATED_AT] == "loaded"


def test_accuracy_report_is_reused_for_a_minute(monkeypatch):
    from types import SimpleNamespace

    from app.services import accuracy_cache

    built = []
    monkeypatch.setattr(accuracy_cache, "_build_report", lambda _settings, source: built.append(source) or object())
    accuracy_cache.clear_report_cache()
    settings = SimpleNamespace(database_url="postgresql://x/db", db_schema="public")

    first = accuracy_cache.load_report(settings, "latest")
    assert accuracy_cache.load_report(settings, "latest") is first  # cached
    accuracy_cache.load_report(settings, "bulk_upload")  # its own entry
    assert accuracy_cache.load_report(settings, "latest", fresh=True) is not first  # Refresh rebuilds
    accuracy_cache.clear_report_cache()  # a ground-truth save
    accuracy_cache.load_report(settings, "latest")
    assert built == ["latest", "bulk_upload", "latest", "latest"]

    monkeypatch.setattr(accuracy_cache, "REPORT_TTL_SEC", 0.0)  # expired
    accuracy_cache.load_report(settings, "latest")
    assert len(built) == 5


def test_provider_credentials_come_from_blob_before_disk(tmp_path, monkeypatch):
    from app.core.schemas import ImagingPageResult
    from app.services import accuracy_cache, blob_store

    page = ImagingPageResult(pageNumber=1, fileName="1.jpg")
    chart = tmp_path / "c1" / "imaging"
    chart.mkdir(parents=True)
    (chart / "c1_provider_signature.csv").write_text(
        "chart_name,page_name,provider_name,signature_present\nc1,1.jpg,Disk Doe MD,yes\n", encoding="utf-8"
    )
    blob_rows = [{"chart_name": "c1", "page_name": "1.jpg", "provider_name": "Blob Roe DO", "signature_present": "yes"}]

    # The Blob copy (Processed output_path) wins; the disk copy is the fallback.
    from_blob = accuracy_cache._with_signatures([page], tmp_path, "c1", blob_rows)[0]
    from_disk = accuracy_cache._with_signatures([page], tmp_path, "c1", [])[0]
    assert (from_blob.providerCredentials, from_disk.providerCredentials) == ("DO", "MD")

    # Downloaded from <output_path>/imaging/<chart>_provider_signature.csv.
    asked = []
    monkeypatch.setattr(blob_store, "resolved_blob_account_url", lambda *_a: "https://acct.blob.core.windows.net")
    monkeypatch.setattr(
        blob_store,
        "download_blob_at",
        lambda **kw: asked.append((kw["container"], kw["key"])) or (b"chart_name,provider_name\nc1,X MD\n", "text/csv"),
    )
    rows = accuracy_cache.blob_signature_rows({"c1": ("imaging", "Processed/R1/B1/c1"), "c2": ("imaging", "")}, ["c1", "c2"])
    assert asked == [("imaging", "Processed/R1/B1/c1/imaging/c1_provider_signature.csv")]
    assert rows == {"c1": [{"chart_name": "c1", "provider_name": "X MD"}]}  # c2 has no output_path
