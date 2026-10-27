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
    # ... then who saved it, which is not a label.
    assert tuple(columns[3:-1]) == _MANUAL_COLUMNS and columns[-1] == "user_id"
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


def test_provider_credentials_come_from_the_database_row():
    """imaging_final.provider_name carries the name and its credentials; no staging, no CSV."""
    from app.services import accuracy_cache

    base = (None,) * 34
    row = ("c1", 1, "1.jpg") + base[3:]
    extra = (None,) * 7 + ("MD",) + ("Ramsey Luce, R.N.",)
    page = accuracy_cache._page(row, extra)
    assert (page.providerName, page.providerCredentials) == ("Ramsey Luce", "RN")
    assert page.groundTruth.providerCredentials == "MD"


def test_subtype_and_specialty_are_saved_and_read():
    """2026-10-27 (user): Page Subtype and Specialty ground truth, free text, stored in
    page_subtype and provider_specialty."""
    from app.services.ground_truth import (
        _COLUMNS,
        _MANUAL_COLUMNS,
        _UPSERT,
        _ground_truth_from_labels,
        _ground_truth_from_row,
    )

    assert _MANUAL_COLUMNS[-5:-3] == ("page_subtype", "provider_specialty")
    assert {"page_subtype", "provider_specialty"} <= set(LABEL_COLUMNS)
    assert "'page_subtype'" in _COLUMNS and "'provider_specialty'" in _COLUMNS
    assert "page_subtype = EXCLUDED.page_subtype" in _UPSERT
    assert "provider_specialty = EXCLUDED.provider_specialty" in _UPSERT
    row = (4, "4.jpg", *([None] * 17), "Wound Care Visit", "Podiatry")
    truth = _ground_truth_from_row(row)
    assert (truth.pageSubtype, truth.specialty) == ("Wound Care Visit", "Podiatry")
    bulk = _ground_truth_from_labels(4, "4.jpg", {"page_subtype": "CC/HPI", "provider_specialty": "Cardiology"})
    assert (bulk.pageSubtype, bulk.specialty) == ("CC/HPI", "Cardiology")


def test_encounter_tilt_and_quality_are_saved_and_read():
    """2026-10-27 (user): Encounter Type, Tilt and Quality ground truth. The encounter type
    goes to encounter_label: encounter_type holds the client sheet's page type."""
    from app.services.ground_truth import (
        _ADD_ENTRY_TABLE,
        _COLUMNS,
        _MANUAL_COLUMNS,
        _UPSERT,
        _ground_truth_from_labels,
        _ground_truth_from_row,
    )

    assert _MANUAL_COLUMNS[-3:] == ("encounter_label", "tilt", "quality")
    assert {"encounter_label", "tilt", "quality"} <= set(LABEL_COLUMNS)
    for column in ("encounter_label", "tilt", "quality"):
        assert f"'{column}'" in _COLUMNS
        assert f"{column} = EXCLUDED.{column}" in _UPSERT
    # One placeholder per inserted column, plus the entry's labels and user id.
    inserted = _UPSERT.split("INSERT INTO page_ground_truth (")[1].split(")")[0]
    values = _UPSERT.split(") VALUES (")[1].split(")")[0]
    assert inserted.count(",") == values.count(",")
    assert "ADD COLUMN IF NOT EXISTS encounter_label" in _ADD_ENTRY_TABLE
    row = (4, "4.jpg", *([None] * 19), "Inpatient", "Slight tilt", "Low")
    truth = _ground_truth_from_row(row)
    assert (truth.encounterLabel, truth.tilt, truth.quality) == ("Inpatient", "Slight tilt", "Low")
    bulk = _ground_truth_from_labels(4, "4.jpg", {"encounter_label": "Home", "tilt": "3", "quality": "High"})
    assert (bulk.encounterLabel, bulk.tilt, bulk.quality) == ("Home", "3", "High")
