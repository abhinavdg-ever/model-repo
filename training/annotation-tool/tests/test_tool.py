from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import digest, make_page, read_labels


def fake_ocr(path: Path) -> tuple[str, str]:
    return f"made up text for {path.name}\nsecond line of made up text", "fake ocr"


@pytest.fixture()
def store(folder, taxonomy):
    from store import Store

    return Store(folder, taxonomy, canon=folder / "no_canon.json")


def index_of(store, image):
    return next(i for i, r in enumerate(store.rows) if r["image"] == image)


# -- the taxonomy --------------------------------------------------------------

def test_path_rules(taxonomy):
    ok = taxonomy.problem
    assert ok("Note", "History", "", "Unknown") is None
    assert ok("Result", "Imaging", "CT", "Eye") is None
    assert ok("Result", "Imaging", "Others", "Eye") is None  # the fallback is a valid L3
    assert "needs an L3" in ok("Result", "Imaging", "", "Eye")  # blank is not, where L3s exist
    assert ok("Result", "Letter", "", "Eye") is None  # blank is right where none exist
    assert "takes no L3" in ok("Result", "Letter", "CT", "Eye")
    assert "not an L2 of Note" in ok("Note", "Imaging", "", "Eye")  # the mask
    assert "needs an L2" in ok("Note", "", "", "Eye")
    assert ok("Flat", "", "", "Others") is None
    assert "takes no L2" in ok("Flat", "History", "", "Unknown")
    assert "no specialty" in ok("Note", "History", "", "")
    assert "not in the list" in ok("Note", "History", "", "Dentistry")


def test_path_codeability_defaults_to_needs_review(taxonomy):
    assert taxonomy.path_codeability("Result", "Imaging", "Echo") == "codeable"
    assert taxonomy.path_codeability("Result", "Letter") == "non_codeable"
    assert taxonomy.path_codeability("Nowhere") == "needs_review"


# -- opening ---------------------------------------------------------------------

def test_opening_changes_nothing_on_disk(folder, store):
    assert digest(folder / "image_labels.csv") == digest(folder / "image_labels.csv")
    before = (folder / "image_labels.csv").read_bytes(), (folder / "training_data.jsonl").read_bytes()
    assert "is_training_added" in store.columns and "suggested_L1" in store.columns
    assert ((folder / "image_labels.csv").read_bytes(), (folder / "training_data.jsonl").read_bytes()) == before
    assert not (folder / "image_labels.v24.backup.csv").exists()


def test_retag_output_counts_as_in_training_and_snapshots_suggestions(store):
    c = store.counts()
    assert c["images"] == 5 and c["missing_files"] == 1
    assert c["in_training"] == 4 and c["untagged"] == 1
    r = store.rows[index_of(store, "70000001_Pg2.jpg")]
    assert (r["suggested_L1"], r["suggested_L3"], r["suggested_specialty"]) == ("Result", "CT", "Cardiology")
    new = store.rows[index_of(store, "sub/IMG_0001.png")]
    assert new["L1"] == "" and new["is_training_added"] == "No"


def test_queues(store):
    from store import filter_items

    names = lambda view: {store.rows[i]["image"] for i in filter_items(store, view)}  # noqa: E731
    assert names("disagreements") == {"70000001_Pg2.jpg"}
    assert names("invalid") == {"70000001_Pg3.jpg"}  # Imaging with a blank L3
    assert names("needs_review") == {"IMG_0002.png"}
    assert names("missing") == {"gone.jpg"}
    assert store.counts()["disagreements"] == 1


def test_canon_path_and_section_suggestion(store):
    item = store.item(index_of(store, "70000001_Pg2.jpg"))
    assert item["canon"] == {"name": "Visit", "L1": "Note", "L2": "History", "L3": "", "complete": True}
    # A page moved under the section L1 is offered the L2 its dominant header gave elsewhere.
    assert item["section"]["L2"] == "History"
    assert store.item(index_of(store, "IMG_0002.png"))["section"]["L2"] == "Others"


# -- saving ---------------------------------------------------------------------

def test_save_validates_and_backs_up_once(folder, store):
    i = index_of(store, "70000001_Pg3.jpg")
    with pytest.raises(ValueError, match="needs an L3"):
        store.set_labels(i, "Result", "Imaging", "", "Eye")
    with pytest.raises(ValueError, match="not an L2"):
        store.set_labels(i, "Note", "Imaging", "", "Eye")
    item = store.set_labels(i, "Result", "Imaging", "Others", "Eye")
    assert item["path"] == "Result > Imaging > Others" and item["completed"] and not item["in_training"]
    assert item["problem"] == ""
    saved = {r["image"]: r for r in read_labels(folder)}["70000001_Pg3.jpg"]
    assert (saved["L3"], saved["path"], saved["suggested_L3"], saved["is_completed"]) == ("Others", "Result > Imaging > Others", "", "Yes")
    assert (folder / "image_labels.v24.backup.csv").is_file()
    backup = digest(folder / "image_labels.v24.backup.csv")
    store.set_labels(i, "Result", "Imaging", "CT", "Eye")
    assert digest(folder / "image_labels.v24.backup.csv") == backup


def test_override_needs_a_reason_and_keeps_both_values(folder, store):
    i = index_of(store, "70000001_Pg1.jpg")
    with pytest.raises(ValueError, match="needs a reason"):
        store.set_labels(i, "Note", "History", "", "Unknown", override="non_codeable")
    item = store.set_labels(i, "Note", "History", "", "Unknown", override="non_codeable",
                            override_reason="canon tag wrong for this type")
    assert item["codeability"] == "codeable" and item["codeability_effective"] == "non_codeable"
    assert store.counts()["overridden"] == 1
    # The same value as derived is not an override.
    item = store.set_labels(i, "Note", "History", "", "Unknown", override="codeable", override_reason="x")
    assert item["codeability_override"] == "" and item["codeability_override_reason"] == ""


def test_changed_path_marks_codeability_stale(store):
    i = index_of(store, "70000001_Pg1.jpg")
    assert not store.item(i)["codeability_stale"]
    item = store.set_labels(i, "Note", "Signature", "", "Unknown")
    assert item["codeability_stale"] and item["codeability"] == "codeable"  # derived value untouched


def test_resolving_a_disagreement_takes_it_off_the_queue(store):
    from store import filter_items

    i = index_of(store, "70000001_Pg2.jpg")
    item = store.set_labels(i, "Note", "History", "", "Cardiology", resolution="canon")
    assert item["resolution"] == "canon"
    assert list(filter_items(store, "disagreements", show_completed=False)) == []
    assert store.counts()["disagreements"] == 0
    with pytest.raises(ValueError, match="resolution"):
        store.set_labels(i, "Note", "History", "", "Cardiology", resolution="maybe")


def test_specialty_others_and_unknown_stay_apart(store):
    i = index_of(store, "70000001_Pg1.jpg")
    store.set_labels(i, "Note", "History", "", "Others")
    assert store.counts()["specialty_other"] == 1
    store.set_labels(i, "Note", "History", "", "Unknown")
    assert store.counts()["specialty_other"] == 0


def test_undo(store):
    i = index_of(store, "70000001_Pg1.jpg")
    store.set_labels(i, "Note", "Signature", "", "Eye")
    item = store.undo()
    assert item["path"] == "Note > History" and item["specialty"] == "Unknown" and not item["completed"]


# -- training file ---------------------------------------------------------------

def test_add_to_training_relabels_without_ocr_and_reads_new_pages(folder, store):
    i = index_of(store, "70000001_Pg1.jpg")
    store.set_labels(i, "Note", "Signature", "", "Eye", override="discharge", override_reason="made up")
    store.set_labels(index_of(store, "sub/IMG_0001.png"), "Flat", "", "", "Unknown")
    read = []

    def ocr(path):
        read.append(path.name)
        return fake_ocr(path)

    summary = store.generate(ocr=ocr)
    assert read == ["IMG_0001.png"]  # an existing record keeps its text
    assert summary.label_changes == 1 and summary.pages_read == 1
    records = {json.loads(l)["image"]: json.loads(l) for l in (folder / "training_data.jsonl").read_text().splitlines()}
    assert len(records) == 5
    r = records["70000001_Pg1.jpg"]
    assert (r["L2"], r["path"], r["specialty"], r["codeability"], r["codeability_rule"]) == \
        ("Signature", "Note > Signature", "Eye", "discharge", "override")
    assert r["text"] == "made up text 70000001_Pg1.jpg"
    assert "L1_train" not in r and all("L2_train" not in x for x in records.values())
    new = records["sub/IMG_0001.png"]
    assert new["file_size"] > 0 and new["ocr_engine"] == "fake ocr" and new["L1"] == "Flat"
    assert store.counts()["not_in_training"] == 0


def test_changed_image_is_read_again(folder, store):
    store.generate(ocr=fake_ocr)  # nothing waiting: no reads
    i = index_of(store, "sub/IMG_0001.png")
    store.set_labels(i, "Flat", "", "", "Unknown")
    store.generate(ocr=fake_ocr)
    make_page(folder / "sub" / "IMG_0001.png", ["MADE UP", "now longer", "and longer still"])
    read = []
    store.generate(ocr=lambda p: (read.append(p.name), fake_ocr(p))[1])
    assert read == ["IMG_0001.png"]


# -- the web API -----------------------------------------------------------------

def test_api_round_trip(store):
    from app import create_app

    client = create_app(store).test_client()
    tax = client.get("/api/taxonomy").get_json()
    assert tax["rules"]["section_L1"] == "Note"
    data = client.get("/api/items?view=disagreements").get_json()
    assert [x["image"] for x in data["items"]] == ["70000001_Pg2.jpg"]
    assert client.get("/api/items?view=nonsense").status_code == 400
    index = data["items"][0]["index"]
    bad = client.post("/api/label", json={"index": index, "L1": "Note", "L2": "Imaging", "specialty": "Eye"})
    assert bad.status_code == 400 and "not an L2" in bad.get_json()["error"]
    good = client.post("/api/label", json={"index": index, "L1": "Result", "L2": "Imaging", "L3": "CT",
                                           "specialty": "Cardiology", "resolution": "label"})
    assert good.status_code == 200 and good.get_json()["item"]["resolution"] == "label"
    assert client.get(f"/api/image/{index}").status_code == 200


def test_the_canon_file_resolves_canon_names(folder, taxonomy, tmp_path):
    from store import Store

    canon = tmp_path / "canon.json"
    canon.write_text(json.dumps({"page_types": [
        {"page_type": "Visit", "path": {"L1": "Note", "L2": "", "L3": ""}},
        {"page_type": "Scan Report", "path": {"L1": "Result", "L2": "Imaging", "L3": "Echo"}}]}))
    store = Store(folder, taxonomy, canon=canon)
    item = store.item(index_of(store, "70000001_Pg2.jpg"))
    # The canon puts Visit under the section L1 with no L2: the section suggestion fills it.
    assert item["canon"] == {"name": "Visit", "L1": "Note", "L2": "History", "L3": "", "complete": True}
    assert store.canon_paths["Scan Report"] == ("Result", "Imaging", "Echo")


def test_a_synonym_flag_with_the_same_path_is_not_a_disagreement(folder, taxonomy, tmp_path):
    import csv

    from store import Store

    rows = list(csv.DictReader((folder / "image_labels.csv").open(encoding="utf-8")))
    columns = list(rows[0])
    extra = [
        # canon page type "Visit" resolves to Note > History: the same path as the label
        {**rows[0], "image": "70000001_Pg1.jpg", "canon_suggests": "Visit", "label_agrees_with_text": "n"},
        # a less specific reading: Result > Imaging with no L3 under a label with L3 CT
        {**rows[2], "image": "70000001_Pg3.jpg", "L3": "CT", "path": "Result > Imaging > CT",
         "canon_suggests": "Scan Report", "label_agrees_with_text": "n"},
    ]
    by_image = {r["image"]: r for r in rows}
    by_image.update({r["image"]: r for r in extra})
    with (folder / "image_labels.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(by_image.values())
    store = Store(folder, taxonomy, canon=folder / "no_canon.json")
    from store import filter_items

    queue = {store.rows[i]["image"] for i in filter_items(store, "disagreements")}
    assert queue == {"70000001_Pg2.jpg"}  # the real conflict only
    assert not store.item(index_of(store, "70000001_Pg1.jpg"))["flagged"]
