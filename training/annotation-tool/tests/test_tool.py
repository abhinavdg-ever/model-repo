from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import digest, make_page, read_labels, write_labels


def fake_ocr(path: Path) -> str:
    return f"made up text for {path.name}\nsecond line of made up text"


@pytest.fixture()
def store_cls():
    from store import Store

    return Store


def test_discovery_resolves_subfolders_bare_names_and_keeps_rows_without_files(folder, store_cls):
    make_page(folder / "sub" / "deeper" / "new_page.jpg", ["MADE UP"])
    store = store_cls(folder)
    images = {row["image"]: store.files[i] for i, row in enumerate(store.rows)}
    assert images["IMG_0001.png"] == "sub/IMG_0001.png"  # bare name, one match in a sub-folder
    assert images["gone.jpg"] is None  # kept untouched
    assert images["sub/deeper/new_page.jpg"] == "sub/deeper/new_page.jpg"  # new row, relative path
    new_row = next(r for r in store.rows if r["image"] == "sub/deeper/new_page.jpg")
    assert new_row["model_type"] == "" and new_row["is_training_added"] == "No"


def test_heic_is_discovered_and_served(folder, store_cls):
    pillow_heif = pytest.importorskip("pillow_heif", reason="pillow-heif not installed")
    from PIL import Image

    pillow_heif.register_heif_opener()
    path = folder / "phone" / "IMG_9999.heic"
    path.parent.mkdir()
    try:
        Image.new("RGB", (400, 300), "white").save(path, "HEIF")
    except Exception as exc:  # encoder missing in some builds
        pytest.skip(f"HEIF encoder unavailable: {exc}")
    store = store_cls(folder)
    index = next(i for i, r in enumerate(store.rows) if r["image"] == "phone/IMG_9999.heic")
    from app import create_app

    client = create_app(store).test_client()
    response = client.get(f"/api/image/{index}")
    assert response.status_code == 200 and response.mimetype == "image/jpeg"


def test_opening_changes_nothing_on_disk(folder, store_cls):
    before = digest(folder / "image_labels.csv")
    store = store_cls(folder)
    assert "is_training_added" in store.columns  # added in memory
    assert digest(folder / "image_labels.csv") == before
    assert not (folder / "training_data.jsonl").exists()
    assert not (folder / "image_labels.backup.csv").exists()


def test_yes_without_a_record_is_reset_in_memory_only(folder, store_cls):
    write_labels(
        folder,
        [{"image": "70000001_Pg1.jpg", "page_type": "Progress Note", "page_subtype": "Progress Note",
          "model_type": "Progress Note", "is_training_added": "Yes"}],
        columns=["image", "page_type", "page_subtype", "model_type", "is_training_added"],
    )
    before = digest(folder / "image_labels.csv")
    store = store_cls(folder)
    assert store.rows[0]["is_training_added"] == "No"
    assert digest(folder / "image_labels.csv") == before


def test_label_change_writes_csv_with_no_and_one_backup(folder, store_cls):
    store = store_cls(folder)
    store.set_model_type(2, "Discharge Summary")
    rows = read_labels(folder)
    row = next(r for r in rows if r["image"] == "IMG_0001.png")
    assert (row["page_type"], row["page_subtype"], row["model_type"]) == (
        "Discharge Summary", "Discharge Summary", "Discharge Summary")
    assert row["is_training_added"] == "No"
    assert any(r["image"] == "gone.jpg" for r in rows)  # row with no file kept
    store.set_model_type(2, "Forms")
    assert len(list(folder.glob("image_labels.backup*.csv"))) == 1


def test_model_type_rules(folder, store_cls):
    store = store_cls(folder)
    tax = store.taxonomy
    pn_sub = next(m for m in tax.model_types if tax.page_type_of(m) == "Progress Note" and m != "Progress Note")
    item = store.set_model_type(0, pn_sub)
    assert (item["page_type"], item["page_subtype"]) == ("Progress Note", pn_sub)
    embedded = tax.embedded_types[0]
    item = store.set_model_type(1, embedded, embedded=True)
    assert (item["page_type"], item["page_subtype"], item["model_type"]) == ("Progress Note", embedded, embedded)
    assert item["embedded"] is True
    specific = tax.specific_subtypes(embedded)[0]
    item = store.set_model_type(1, embedded, page_subtype=specific)
    assert (item["page_type"], item["page_subtype"]) == (embedded, specific)
    other = next(m for m in tax.model_types if tax.page_type_of(m) not in ("Progress Note", embedded))
    item = store.set_model_type(1, other)
    assert item["page_subtype"] == other  # old sub-type does not belong: generic
    with pytest.raises(ValueError):
        store.set_model_type(1, "Not A Real Type")


def test_undo_restores_the_last_change(folder, store_cls):
    store = store_cls(folder)
    store.set_model_type(0, "Forms")
    store.undo()
    assert read_labels(folder)[0]["model_type"] == "Progress Note"


def test_generate_writes_jsonl_sets_yes_and_second_run_does_nothing(folder, store_cls):
    store = store_cls(folder)
    summary = store.generate(fake_ocr, engine="test-engine")
    assert summary.pages_read == 2 and summary.skipped_untagged == 1
    records = [json.loads(l) for l in (folder / "training_data.jsonl").read_text().splitlines()]
    assert [r["image"] for r in records] == ["70000001_Pg1.jpg", "70000001_Pg2.jpg"]
    first = records[0]
    assert first["chart_id"] == "70000001" and first["page_index"] == 1
    assert first["file_size"] == (folder / "70000001_Pg1.jpg").stat().st_size
    assert first["n_chars"] == len(first["text"]) and first["ocr_engine"] == "test-engine"
    yes = {r["image"]: r["is_training_added"] for r in read_labels(folder)}
    assert yes["70000001_Pg1.jpg"] == yes["70000001_Pg2.jpg"] == "Yes"
    assert yes["IMG_0001.png"] == "No"

    labels_before = digest(folder / "image_labels.csv")
    jsonl_before = digest(folder / "training_data.jsonl")
    calls = []
    again = store_cls(folder).generate(lambda p: calls.append(p) or "x", engine="test-engine")
    assert calls == [] and again.candidates == 0
    assert digest(folder / "image_labels.csv") == labels_before
    assert digest(folder / "training_data.jsonl") == jsonl_before


def test_label_only_change_does_no_ocr(folder, store_cls):
    store_cls(folder).generate(fake_ocr, engine="test-engine")
    store = store_cls(folder)
    store.set_model_type(1, store.taxonomy.embedded_types[0], embedded=True)
    calls = []
    summary = store.generate(lambda p: calls.append(p) or "x", engine="test-engine")
    assert calls == [] and summary.label_changes == 1 and summary.pages_read == 0
    record = [json.loads(l) for l in (folder / "training_data.jsonl").read_text().splitlines()][1]
    assert record["page_type"] == "Progress Note"


def test_mirror_drops_untagged_and_missing_rows(folder, store_cls):
    store_cls(folder).generate(fake_ocr, engine="test-engine")
    store = store_cls(folder)
    store.clear(0)
    store.generate(fake_ocr, engine="test-engine")
    images = [json.loads(l)["image"] for l in (folder / "training_data.jsonl").read_text().splitlines()]
    assert images == ["70000001_Pg2.jpg"]


def test_a_changed_file_is_read_again(folder, store_cls):
    store_cls(folder).generate(fake_ocr, engine="test-engine")
    make_page(folder / "70000001_Pg1.jpg", ["A DIFFERENT MADE UP PAGE", "with more lines", "and more"])
    store = store_cls(folder)
    store.set_model_type(0, "Forms")
    calls = []
    store.generate(lambda p: calls.append(p.name) or "new made up text", engine="test-engine")
    assert calls == ["70000001_Pg1.jpg"]


def test_interrupted_generate_resumes(folder, store_cls, monkeypatch):
    import store as store_module

    for i in range(3, 10):
        make_page(folder / f"70000002_Pg{i}.jpg", [f"MADE UP PAGE {i}"])
    store = store_cls(folder)
    for i, row in enumerate(store.rows):
        if row["image"].startswith("70000002"):
            store.set_model_type(i, "Forms")
    monkeypatch.setattr(store_module, "CHECKPOINT_EVERY", 2)
    seen = []

    def flaky(path: Path) -> str:
        if len(seen) == 5:
            raise KeyboardInterrupt
        seen.append(path.name)
        return "made up"

    with pytest.raises(KeyboardInterrupt):
        store.generate(flaky, engine="test-engine")
    saved = [json.loads(l)["image"] for l in (folder / "training_data.jsonl").read_text().splitlines()]
    assert len(saved) >= 4  # checkpoints kept the work done
    calls = []
    store_cls(folder).generate(lambda p: calls.append(p.name) or "made up", engine="test-engine")
    assert set(calls).isdisjoint(saved)


def test_cli_generate_prints_no_page_text(folder, capsys, monkeypatch):
    import ocr

    monkeypatch.setattr(ocr, "ocr_image", lambda p: "SECRET MADE UP PATIENT TEXT")
    monkeypatch.setattr(ocr, "engine_label", lambda: "test-engine")
    monkeypatch.setattr(ocr, "tesseract_version", lambda: "5")
    from app import main

    assert main([str(folder), "--generate"]) == 0
    out = capsys.readouterr()
    assert "SECRET" not in out.out + out.err
    assert "pages read (OCR): 2" in out.out


def test_api_never_returns_page_text(folder, store_cls):
    store = store_cls(folder)
    store.generate(lambda p: "SECRET MADE UP PATIENT TEXT", engine="test-engine")
    from app import create_app

    client = create_app(store).test_client()
    body = client.get("/api/items").get_data(as_text=True)
    assert "SECRET" not in body
    response = client.post("/api/label", json={"index": 2, "page_type": "Forms", "page_subtype": ""})
    assert response.status_code == 200 and response.get_json()["item"]["model_type"] == "Forms"


def test_real_tesseract_reads_a_made_up_page(tmp_path):
    import ocr

    try:
        ocr.tesseract_version()
    except ocr.TesseractMissing:
        pytest.skip("tesseract not installed")
    page = make_page(tmp_path / "p.png", ["DISCHARGE SUMMARY", "MADE UP PATIENT"])
    text = ocr.ocr_image(page)
    assert "DISCHARGE" in text.upper()
    assert "\n\n\n" not in text


# --- the two boxes: page type + sub-type -----------------------------------


def test_set_labels_from_page_type_and_sub_type(folder, store_cls):
    store = store_cls(folder)
    tax = store.taxonomy
    pn = "Progress Note"
    specific = next(s for s in tax.sub_types[pn] if s != pn)
    item = store.set_labels(2, pn, specific)
    assert (item["page_type"], item["page_subtype"], item["model_type"]) == (pn, specific, specific)

    other = next(p for p in tax.page_types if p != pn and tax.specific_subtypes(p))
    sub = tax.specific_subtypes(other)[0]
    item = store.set_labels(2, other, sub)
    assert (item["page_type"], item["page_subtype"], item["model_type"]) == (other, sub, other)

    item = store.set_labels(2, other, "")  # empty sub-type = the generic one
    assert item["page_subtype"] == other
    assert read_labels(folder)[2]["is_training_added"] == "No"


def test_a_sub_type_of_another_page_type_is_refused(folder, store_cls):
    store = store_cls(folder)
    tax = store.taxonomy
    a, b = [p for p in tax.page_types if tax.specific_subtypes(p)][:2]
    with pytest.raises(ValueError, match="not a sub-type"):
        store.set_labels(2, a, tax.specific_subtypes(b)[0])


def test_inside_a_progress_note_writes_the_embedded_pair(folder, store_cls):
    store = store_cls(folder)
    lab = store.taxonomy.embedded_types[0]
    item = store.set_labels(2, lab, "", embedded=True)
    assert (item["page_type"], item["page_subtype"], item["model_type"]) == ("Progress Note", lab, lab)
    assert item["embedded"] is True
    with pytest.raises(ValueError, match="cannot sit inside"):
        store.set_labels(2, "Forms", "", embedded=True)


def test_add_to_training_rereads_a_changed_image_already_in_training(folder, store_cls):
    """Tagged, in training, then the image file is replaced: Add reads it again."""
    store_cls(folder).generate(fake_ocr, engine="test-engine")
    make_page(folder / "70000001_Pg2.jpg", ["A REPLACED MADE UP PAGE", "longer", "and longer still"])
    store = store_cls(folder)
    calls = []
    summary = store.generate(lambda p: calls.append(p.name) or "new made up text", engine="test-engine")
    assert calls == ["70000001_Pg2.jpg"]
    assert summary.pages_read == 1


def test_cli_add_to_training_flag(folder, capsys, monkeypatch):
    import app
    import store as store_module

    monkeypatch.setattr(store_module.Store, "generate",
                        lambda self, **k: store_module.GenerateSummary(candidates=0))
    assert app.main([str(folder), "--add-to-training"]) == 0
    assert app.main([str(folder), "--generate"]) == 0


def test_a_page_saved_inside_a_progress_note_saves_again_without_a_tick(folder, store_cls):
    """The tick box is gone; a pair saved with it must not start failing."""
    store = store_cls(folder)
    lab = store.taxonomy.embedded_types[0]
    item = store.set_labels(2, "Progress Note", lab)
    assert (item["page_type"], item["page_subtype"], item["model_type"]) == ("Progress Note", lab, lab)


def test_the_page_has_no_inside_a_progress_note_tick(folder, store_cls):
    from app import create_app

    page = create_app(store_cls(folder)).test_client().get("/").get_data(as_text=True)
    assert "sits inside a Progress Note" not in page
    assert page.index('id="pending"') < page.index('id="add"')  # count beside the button, top right


def test_one_backup_ever_the_original_csv(folder, store_cls):
    """A new session does not add another backup; the one kept is the CSV as
    it was before the tool first wrote to it."""
    original = (folder / "image_labels.csv").read_bytes()
    store_cls(folder).set_labels(2, "Forms")
    store_cls(folder).set_labels(2, "Orders")  # a second session
    backups = list(folder.glob("image_labels.backup*.csv"))
    assert [b.name for b in backups] == ["image_labels.backup.csv"]
    assert backups[0].read_bytes() == original


# --- completed -------------------------------------------------------------


def test_save_marks_completed_even_with_the_same_labels(folder, store_cls):
    store_cls(folder).generate(fake_ocr, engine="test-engine")
    store = store_cls(folder)
    row = store.rows[0]
    item = store.set_labels(0, row["page_type"], row["page_subtype"])
    assert item["completed"] is True
    # Same labels: still in training, nothing to re-add.
    assert item["in_training"] is True
    assert read_labels(folder)[0]["is_completed"] == "Yes"


def test_completed_pages_are_hidden_unless_asked_for(folder, store_cls):
    from app import create_app

    store = store_cls(folder)
    store.set_labels(0, "Forms")
    client = create_app(store).test_client()
    hidden = [i["index"] for i in client.get("/api/items").get_json()["items"]]
    shown = [i["index"] for i in client.get("/api/items?completed=1").get_json()["items"]]
    assert 0 not in hidden and 0 in shown
    counts = client.get("/api/items").get_json()["counts"]
    assert counts["completed"] == 1 and counts["to_do"] == counts["images"] - 1


def test_untag_and_undo_take_the_page_back_to_to_do(folder, store_cls):
    store = store_cls(folder)
    store.set_labels(0, "Forms")
    assert store.clear(0)["completed"] is False
    store.set_labels(1, "Forms")
    assert store.undo()["completed"] is False
