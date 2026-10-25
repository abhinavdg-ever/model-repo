"""Taxonomy v29.2, the SSD layout, claude-reviewed, encounter type and the
name-map migration. Made-up pages, rows and text only; the label space is the
tool's own taxonomy.json (no patient data)."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from conftest import TOOL, make_page

REPO_TAXONOMY = TOOL.parent / "taxonomy" / "taxonomy.json"
REPO_NAME_MAP = TOOL.parent / "taxonomy" / "rules" / "name_map.json"
REPO_LABEL_SETS = TOOL.parent / "taxonomy" / "rules" / "label_sets.json"

COLUMNS = ["image", "page_id", "chart_id", "page_index", "L1", "L2", "L3", "path", "specialty", "codeability",
           "codeability_rule", "codeability_reason", "page_type", "dominant_section", "sections",
           "canon_suggests", "label_agrees_with_text"]
ENCOUNTER_CLASSES = ["Inpatient", "Outpatient (F2F)", "Outpatient (Evisit)", "Outpatient (Home Visit)",
                     "Not Available"]
# A made-up name map with the pack's shape.
NAME_MAP = {
    "_comment": "made up",
    "L1": {"Lab Report/Result": "Lab Report"},
    "L2": {"Lab Report/Result > Laboratory": "Lab Report > Lab Result",
           "Demographics > Registration Form": "Administrative > Patient Registration",
           "Progress Note > Lab Results": "Progress Note > Lab and Imaging Results"},
    "L2_split": {"Progress Note > Lab Results": "Lab and Imaging Results, or Lab Report when wholly results",
                 "Patient Communication > Provider Letter": "Provider Letter > one of four"},
    "L3": {"INR and Prothrombin Time": "Coagulation Report"},
    "L3_extra": {"Myocardial Perfusion Study": "Stress Test", "Pap and HPV Report": "Diagnostic Study or Cytology"},
}


def label(image, l1, l2="", l3="", specialty="Unknown", **extra):
    return {"image": image, "L1": l1, "L2": l2, "L3": l3, "path": " > ".join(p for p in (l1, l2, l3) if p),
            "specialty": specialty, "codeability": "codeable", "codeability_rule": "page_type_tag",
            "codeability_reason": "made up", **extra}


def write_csv(path: Path, rows: list[dict], columns: list[str], newline: str = "\r\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator=newline)
        writer.writeheader()
        writer.writerows({c: r.get(c, "") for c in columns} for r in rows)
    return path


def lines(path: Path) -> list[bytes]:
    """One entry per record: a CSV record with a quoted line break stays whole."""
    from rawfile import _records

    return [r.encode("utf-8") for r in _records(path.read_bytes().decode("utf-8")) if r.strip()]


@pytest.fixture()
def ssd(tmp_path: Path) -> Path:
    """The SSD layout in miniature: Training/processed (labels, originals),
    Training/ocr-jpeg (OCR copies), Training/encounter (tags)."""
    processed, copies = tmp_path / "Training" / "processed", tmp_path / "Training" / "ocr-jpeg"
    # Phone photos: the key ends .HEIC and only the OCR copy is a JPEG. The
    # originals are stand-ins (a browser never gets them while a copy exists).
    for stem in ("Chart1_Pg1", "Chart1_Pg2", "Chart1_Pg3"):
        processed.mkdir(parents=True, exist_ok=True)
        (processed / f"{stem}.HEIC").write_bytes(b"not a real heic")
        make_page(copies / f"{stem}.jpg", ["MADE UP", stem])
    make_page(processed / "60000001_Pg1.jpg", ["MADE UP"])
    rows = [
        label("Chart1_Pg1.HEIC", "Lab Report", "Lab Result", "Coagulation Report", "Cardiology",
              label_agrees_with_text="claude-reviewed", sections="vitals; labs, \"quoted\"\nsecond line"),
        # the CSV names the copy's .jpg while the original on disk is .HEIC
        label("Chart1_Pg2.jpg", "Progress Note", "CC/HPI", label_agrees_with_text="claude-reviewed"),
        label("Chart1_Pg3.HEIC", "Demographics", "Patient Information", label_agrees_with_text="n",
              canon_suggests="Made Up Type"),
        label("60000001_Pg1.jpg", "Lab Report", "Radiology Report", label_agrees_with_text=""),
    ]
    write_csv(processed / "image_labels.csv", rows, COLUMNS)
    with (processed / "training_data.jsonl").open("w", encoding="utf-8") as handle:
        for r in rows:
            record = {k: r[k] for k in ("image", "L1", "L2", "L3", "path", "specialty", "codeability")}
            handle.write(json.dumps({**record, "text": f"made up text é {r['image']}", "L1_train": r["L1"]},
                                    ensure_ascii=False) + "\n")
    tags = tmp_path / "Training" / "encounter" / "encounter_tags.jsonl"
    tags.parent.mkdir(parents=True)
    with tags.open("w", encoding="utf-8") as handle:
        for r, kind in zip(rows[:3], ("Inpatient", "Outpatient (F2F)", "Not Available")):
            handle.write(json.dumps({
                "image": r["image"], "encounter_type": kind, "basis": "inferred", "visit": "",
                "evidence": "made up quote", "confidence": "high", "reviewer": "claude (2026-10-25)",
                "train": True, "L1": r["L1"], "L2": r["L2"], "chart_id": "Chart1", "page_index": 1,
                "page_id": "", "folder": "", "text": "made up text ü", "n_chars": 14, "specialty": "Unknown",
                "page_type": ""}, ensure_ascii=False) + "\n")
    write_csv(processed / "encounter_review.csv",
              [{"image": r["image"], "encounter_type": kind, "basis": "inferred", "visit": "",
                "evidence": "made up, quoted", "confidence": "high", "reviewer": "claude (2026-10-25)"}
               for r, kind in zip(rows[:3], ("Inpatient", "Outpatient (F2F)", "Not Available"))],
              ["image", "encounter_type", "basis", "visit", "evidence", "confidence", "reviewer"])
    (processed / "encounter_canon.json").write_text(json.dumps({"classes": ENCOUNTER_CLASSES}))
    (tmp_path / "name_map.json").write_text(json.dumps(NAME_MAP))
    return processed


@pytest.fixture()
def store(ssd):
    from store import Store

    return Store(ssd, name_map=ssd.parent.parent / "name_map.json")


def at(store, image):
    return next(i for i, r in enumerate(store.rows) if r["image"] == image)


# -- 1. the v29.2 label space -----------------------------------------------------

def test_the_tool_loads_v29_2_and_its_copy_is_the_repo_file():
    from taxonomy import TAXONOMY_PATH, Taxonomy

    assert TAXONOMY_PATH.name == "taxonomy.json"
    assert TAXONOMY_PATH.read_bytes() == REPO_TAXONOMY.read_bytes()
    tax = Taxonomy()
    assert tax.version == "v29.2" and len(tax.l1) == tax.counts["L1"]
    assert "Clinical Summary" in tax.l1 and "Lab Report/Result" not in tax.l1


def test_no_old_name_is_offered():
    """No name the name map renames, and no older L1, is in a picker."""
    from taxonomy import Taxonomy

    tax = Taxonomy()
    offered = tax.names()
    assert not {"Lab Report/Result", "Consent Form", "Authorization"} & offered
    old = json.loads(REPO_NAME_MAP.read_text(encoding="utf-8")) if REPO_NAME_MAP.is_file() else NAME_MAP
    for key in ("L1", "L2", "L2_split", "L3"):
        assert not {k for k in old[key] if not k.startswith("_")} & offered, key


def test_label_sets_agree_with_the_taxonomy():
    from taxonomy import Taxonomy

    if not REPO_LABEL_SETS.is_file():
        pytest.skip("no label_sets.json copy in the repo")
    tax = Taxonomy()
    assert tax.label_sets_problems(json.loads(REPO_LABEL_SETS.read_text(encoding="utf-8"))) == []
    bad = {"L1": [*tax.l1, "Lab Report/Result"], "L2_by_L1": {}}
    assert any("Lab Report/Result" in p for p in tax.label_sets_problems(bad))


def test_a_blank_l3_is_valid_where_v29_2_lists_the_l2_path(store):
    tax = store.taxonomy
    assert tax.problem("Lab Report", "Radiology Report", "", "Unknown") is None
    assert tax.problem("Lab Report", "Lab Result", "Coagulation Report", "Unknown") is None
    assert "not an L3" in tax.problem("Lab Report", "Lab Result", "INR and Prothrombin Time", "Unknown")
    assert store.counts()["invalid"] == 0


def test_the_canon_callout_offers_only_v29_2_names(ssd, tmp_path):
    from store import Store

    canon = tmp_path / "canon.json"
    canon.write_text(json.dumps({"page_types": [
        # an old path the name map renames
        {"page_type": "Made Up Type", "path": {"L1": "Lab Report/Result", "L2": "Laboratory",
                                              "L3": "INR and Prothrombin Time"}},
        # an L1 v29.2 does not have at all
        {"page_type": "Gone Type", "path": {"L1": "No Such L1", "L2": "", "L3": ""}}]}))
    store = Store(ssd, canon=canon, name_map=tmp_path / "Training" / ".." / "name_map.json")
    assert store.canon_paths["Made Up Type"] == ("Lab Report", "Lab Result", "Coagulation Report")
    assert "Gone Type" not in store.canon_paths
    offered = store.taxonomy.names()
    for path in store.canon_paths.values():
        assert all(name in offered for name in (path[0], path[2]) if name)
    item = store.item(at(store, "Chart1_Pg3.HEIC"))
    assert item["canon"]["L1"] == "Lab Report" and item["canon"]["complete"]


def test_api_taxonomy_carries_v29_2_and_the_encounter_classes(store):
    pytest.importorskip("flask")
    from app import create_app

    client = create_app(store).test_client()
    tax = client.get("/api/taxonomy").get_json()
    assert tax["version"] == "v29.2" and tax["encounter_types"] == ENCOUNTER_CLASSES
    assert "Lab Report/Result" not in [x["name"] for x in tax["L1"]]


# -- 2. the SSD layout ------------------------------------------------------------

def test_heic_keys_open_the_ocr_jpeg_copy(ssd, store):
    copies = ssd.parent / "ocr-jpeg"
    assert store.images_dir == copies.resolve()
    assert store.image_path(at(store, "Chart1_Pg1.HEIC")) == copies.resolve() / "Chart1_Pg1.jpg"
    # A .jpg key whose original is a .HEIC claims that original (no extra row)
    # and shows the copy.
    assert store.image_path(at(store, "Chart1_Pg2.jpg")) == copies.resolve() / "Chart1_Pg2.jpg"
    assert store.image_path(at(store, "60000001_Pg1.jpg")) == ssd.resolve() / "60000001_Pg1.jpg"
    c = store.counts()
    assert c["rows"] == 4 and c["missing_files"] == 0 and c["in_training"] == 4


def test_the_image_endpoint_serves_the_copy(store):
    pytest.importorskip("flask")
    from app import create_app

    client = create_app(store).test_client()
    response = client.get(f"/api/image/{at(store, 'Chart1_Pg1.HEIC')}")
    assert response.status_code == 200 and response.mimetype == "image/jpeg"


def test_images_flag_and_missing_folder(ssd, tmp_path):
    from store import Store

    other = tmp_path / "elsewhere"
    make_page(other / "Chart1_Pg1.jpg", ["MADE UP"])
    store = Store(ssd, images=other)
    assert store.image_path(at(store, "Chart1_Pg1.HEIC")) == other.resolve() / "Chart1_Pg1.jpg"
    with pytest.raises(FileNotFoundError):
        Store(ssd, images=tmp_path / "nowhere")


def test_opening_and_an_empty_add_to_training_change_nothing(ssd, store):
    before = {p.name: p.read_bytes() for p in ssd.iterdir() if p.is_file()}
    tags = ssd.parent / "encounter" / "encounter_tags.jsonl"
    tags_before = tags.read_bytes()
    store.generate(ocr=lambda p: ("never", "x"))  # every row is in training already
    assert {p.name: p.read_bytes() for p in ssd.iterdir() if p.is_file()} == before
    assert tags.read_bytes() == tags_before


# -- 3. claude-reviewed -----------------------------------------------------------

def test_claude_reviewed_is_its_own_queue(store):
    from store import filter_items

    queue = {store.rows[i]["image"] for i in filter_items(store, "claude_reviewed")}
    assert queue == {"Chart1_Pg1.HEIC", "Chart1_Pg2.jpg"}
    assert store.counts()["claude_reviewed"] == 2


def test_an_unchanged_save_keeps_claude_reviewed_and_an_edit_sets_y(ssd, store):
    i = at(store, "Chart1_Pg1.HEIC")
    item = store.set_labels(i, "Lab Report", "Lab Result", "Coagulation Report", "Cardiology")
    assert item["agrees"] == "claude-reviewed" and item["in_training"]
    j = at(store, "Chart1_Pg2.jpg")
    item = store.set_labels(j, "Progress Note", "Vitals", "", "Unknown")
    assert item["agrees"] == "y" and not item["in_training"]
    saved = {r["image"]: r for r in csv.DictReader((ssd / "image_labels.csv").open(encoding="utf-8"))}
    assert saved["Chart1_Pg1.HEIC"]["label_agrees_with_text"] == "claude-reviewed"
    assert saved["Chart1_Pg2.jpg"]["label_agrees_with_text"] == "y"
    assert store.undo()["agrees"] == "claude-reviewed"


def test_a_save_rewrites_only_that_row(ssd, store):
    original = lines(ssd / "image_labels.csv")
    assert original[0].endswith(b"\r\n")
    store.set_labels(at(store, "60000001_Pg1.jpg"), "Lab Report", "Radiology Report", "", "Radiology")
    after = lines(ssd / "image_labels.csv")
    added = len(store.columns) - len(COLUMNS)
    assert added > 0 and len(after) == len(original)
    # Untouched rows (one with a quoted multi-line cell) keep their bytes; the
    # tool's new columns are empty cells after them.
    for old, new in zip(original[1:-1], after[1:-1]):
        assert new == old.rstrip(b"\r\n") + b"," * added + b"\r\n"
    assert b"Radiology" in after[-1] and after[-1].endswith(b"\r\n")
    # Training records are untouched until Add to Training JSON, which
    # rewrites only the changed one.
    training = lines(ssd / "training_data.jsonl")
    store.generate(ocr=lambda p: ("never", "x"))
    changed = [n for n, (a, b) in enumerate(zip(training, lines(ssd / "training_data.jsonl"))) if a != b]
    assert changed == [3]
    record = json.loads(lines(ssd / "training_data.jsonl")[3])
    assert record["specialty"] == "Radiology" and "L1_train" not in record and record["text"].startswith("made up")


# -- 4. encounter type ------------------------------------------------------------

def test_encounter_is_read_from_both_files(store):
    item = store.item(at(store, "Chart1_Pg1.HEIC"))
    assert item["encounter"]["encounter_type"] == "Inpatient" and "evidence" not in item["encounter"]
    assert store.item(at(store, "60000001_Pg1.jpg"))["encounter"]["encounter_type"] == ""
    assert store.encounters.classes == ENCOUNTER_CLASSES


def test_a_human_encounter_edit_is_written_back_and_nothing_else_moves(ssd, store):
    tags = ssd.parent / "encounter" / "encounter_tags.jsonl"
    review = ssd / "encounter_review.csv"
    tags_before, review_before = lines(tags), lines(review)
    i = at(store, "Chart1_Pg2.jpg")
    store.set_labels(i, "Progress Note", "CC/HPI", "", "Unknown", encounter_type="Outpatient (Home Visit)")
    tags_after, review_after = lines(tags), lines(review)
    assert [n for n, (a, b) in enumerate(zip(tags_before, tags_after)) if a != b] == [1]
    assert [n for n, (a, b) in enumerate(zip(review_before, review_after)) if a != b] == [2]  # header is 0
    record = json.loads(tags_after[1])
    assert (record["encounter_type"], record["reviewer"], record["basis"], record["train"]) == \
        ("Outpatient (Home Visit)", "human", "hand-labelled", True)
    assert list(record) == list(json.loads(tags_before[1]))  # same fields, same order
    row = next(r for r in csv.DictReader(review.open(encoding="utf-8")) if r["image"] == "Chart1_Pg2.jpg")
    assert (row["encounter_type"], row["reviewer"], row["basis"]) == ("Outpatient (Home Visit)", "human",
                                                                       "hand-labelled")
    # The label did not change, so claude-reviewed stays.
    assert store.item(i)["agrees"] == "claude-reviewed"
    # Undo puts both files back byte for byte.
    store.undo()
    assert lines(tags) == tags_before and lines(review) == review_before


def test_an_unchanged_encounter_writes_nothing_and_a_bad_one_is_refused(ssd, store):
    tags = ssd.parent / "encounter" / "encounter_tags.jsonl"
    before = tags.read_bytes()
    store.set_labels(at(store, "Chart1_Pg1.HEIC"), "Lab Report", "Lab Result", "Coagulation Report", "Cardiology",
                     encounter_type="Inpatient")
    assert tags.read_bytes() == before
    with pytest.raises(ValueError, match="not an encounter type"):
        store.set_labels(at(store, "Chart1_Pg1.HEIC"), "Lab Report", "Lab Result", "", "Cardiology",
                         encounter_type="Outpatient")


def test_a_page_without_an_encounter_gets_a_new_entry(ssd, store):
    tags = ssd.parent / "encounter" / "encounter_tags.jsonl"
    before = lines(tags)
    store.set_labels(at(store, "60000001_Pg1.jpg"), "Lab Report", "Radiology Report", "", "Unknown",
                     encounter_type="Not Available")
    after = lines(tags)
    assert after[:-1] == before
    new = json.loads(after[-1])
    assert (new["image"], new["reviewer"], new["train"], new["L1"]) == ("60000001_Pg1.jpg", "human", True,
                                                                         "Lab Report")
    assert new["text"].startswith("made up text")
    assert store.encounters.get("60000001_Pg1.jpg")["encounter_type"] == "Not Available"


def test_api_saves_the_encounter(store):
    pytest.importorskip("flask")
    from app import create_app

    client = create_app(store).test_client()
    index = at(store, "Chart1_Pg3.HEIC")
    response = client.post("/api/label", json={"index": index, "L1": "Demographics", "L2": "Patient Information",
                                               "specialty": "Unknown", "encounter_type": "Inpatient"})
    assert response.status_code == 200
    assert response.get_json()["item"]["encounter"]["reviewer"] == "human"


# -- 5. migration -----------------------------------------------------------------

@pytest.fixture()
def old_names(ssd):
    """The same folder with labels in the 23 Oct names."""
    path = ssd / "image_labels.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    rows[0].update(L1="Lab Report/Result", L2="Laboratory", L3="INR and Prothrombin Time",
                   path="Lab Report/Result > Laboratory > INR and Prothrombin Time")
    rows[1].update(L2="Lab Results", path="Progress Note > Lab Results")
    rows[2].update(L1="Patient Communication", L2="Provider Letter", path="Patient Communication > Provider Letter")
    extra = [label("Chart9_Pg1.jpg", "Demographics", "Registration Form"),
             label("Chart9_Pg2.jpg", "Lab Report", "Diagnostic Study", "Myocardial Perfusion Study"),
             label("Chart9_Pg3.jpg", "Lab Report", "Diagnostic Study", "Pap and HPV Report")]
    write_csv(path, rows + extra, COLUMNS)
    # The first page went into training under its old labels.
    training = ssd / "training_data.jsonl"
    records = [json.loads(line) for line in training.read_text(encoding="utf-8").splitlines()]
    records[0].update({k: rows[0][k] for k in ("L1", "L2", "L3", "path")})
    training.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return ssd


def test_migration_dry_run_reports_and_writes_nothing(old_names, capsys):
    from store import Store

    before = (old_names / "image_labels.csv").read_bytes()
    store = Store(old_names, name_map=old_names.parent.parent / "name_map.json")
    report = store.migrate(apply=False)
    renames = {c.image: c.new for c in report.changes}
    assert renames == {
        "Chart1_Pg1.HEIC": ("Lab Report", "Lab Result", "Coagulation Report"),
        "Chart9_Pg1.jpg": ("Administrative", "Patient Registration", ""),
        "Chart9_Pg2.jpg": ("Lab Report", "Diagnostic Study", "Stress Test"),
    }
    # Split names and an L3 with no single target are listed, never guessed.
    assert {d.image for d in report.decisions} == {"Chart1_Pg2.jpg", "Chart1_Pg3.HEIC", "Chart9_Pg3.jpg"}
    report.print()
    out = capsys.readouterr().out
    assert "rows needing a person's decision: 3" in out and "made up text" not in out
    assert (old_names / "image_labels.csv").read_bytes() == before


def test_migration_apply_renames_only_the_mapped_rows(old_names):
    from store import Store

    store = Store(old_names, name_map=old_names.parent.parent / "name_map.json")
    before = lines(old_names / "image_labels.csv")
    training_before = lines(old_names / "training_data.jsonl")
    store.migrate(apply=True)
    after = lines(old_names / "image_labels.csv")
    added = len(store.columns) - len(COLUMNS)
    changed = [n for n, (a, b) in enumerate(zip(before, after))
               if b != a.rstrip(b"\r\n") + b"," * added + b"\r\n"]
    assert changed == [0, 1, 5, 6]  # the header (new columns) and the three renamed rows
    rows = {r["image"]: r for r in csv.DictReader((old_names / "image_labels.csv").open(encoding="utf-8"))}
    assert rows["Chart1_Pg1.HEIC"]["path"] == "Lab Report > Lab Result > Coagulation Report"
    assert rows["Chart1_Pg2.jpg"]["L2"] == "Lab Results"  # split: left for a person
    # The renamed row was in training under its old labels: its record follows.
    training_after = lines(old_names / "training_data.jsonl")
    assert [n for n, (a, b) in enumerate(zip(training_before, training_after)) if a != b] == [0]
    assert json.loads(training_after[0])["L1"] == "Lab Report"
    # Opening it again finds nothing more to rename.
    again = Store(old_names, name_map=old_names.parent.parent / "name_map.json").migrate(apply=False)
    assert again.changes == [] and len(again.decisions) == 3


def test_migration_is_a_no_op_on_v29_2_labels(ssd, store):
    before = {p.name: p.read_bytes() for p in ssd.iterdir() if p.is_file()}
    report = store.migrate(apply=True)
    assert report.changes == [] and report.decisions == [] and report.suggested_changes == 0
    assert {p.name: p.read_bytes() for p in ssd.iterdir() if p.is_file()} == before


def test_the_repo_name_map_points_at_v29_2_names():
    from migrate import NameMap
    from taxonomy import Taxonomy

    if not REPO_NAME_MAP.is_file():
        pytest.skip("no name map in the repo")
    tax, names = Taxonomy(), NameMap.load(REPO_NAME_MAP)
    for old, new in names.l2.items():
        if old not in names.l2_split:
            l1, l2 = new.split(" > ")
            assert l2 in tax.l2_of(l1), new
    assert set(names.l1.values()) <= set(tax.l1)
    all_l3 = {c for key in tax.data["L3"] for c in tax.l3_of(*key.split(" > "))}
    assert set(names.l3.values()) <= all_l3


def test_check_taxonomy_passes_on_v29_2_and_fails_on_old_names(ssd, old_names, capsys):
    import check_taxonomy

    name_map = str(ssd.parent.parent / "name_map.json")
    assert check_taxonomy.main([str(old_names), "--name-map", name_map]) == 1
    out = capsys.readouterr().out
    assert "needing a decision" in out and "FAILED" in out and "made up text" not in out
