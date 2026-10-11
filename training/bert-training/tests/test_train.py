"""train.py on made-up pages with a tiny local BERT. No internet, no real pages."""
from __future__ import annotations

import csv
import json
import random
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import train  # noqa: E402

# Made-up vocabulary per class, so a tiny model can learn something.
WORDS = {
    "Progress Note": "chief complaint history present illness assessment plan follow visit",
    "Laboratory Data": "hemoglobin glucose sodium potassium reference range specimen result",
    "Radiology Report": "impression findings contrast chest view radiologist exam technique",
    "Discharge Summary": "admission discharge hospital course disposition condition stable",
}
RARE = "Sleep Study"  # fewer pages than --min-pages
VOCAB = sorted({w for text in WORDS.values() for w in text.split()} | {"sleep", "apnea", "oxygen", "night"})


@pytest.fixture(scope="session")
def tiny_model(tmp_path_factory) -> Path:
    from transformers import BertConfig, BertModel, BertTokenizerFast

    folder = tmp_path_factory.mktemp("tiny-bert")
    vocab = folder / "vocab.txt"
    vocab.write_text("\n".join(["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]", *VOCAB]), encoding="utf-8")
    BertTokenizerFast(vocab_file=str(vocab)).save_pretrained(folder)
    config = BertConfig(vocab_size=5 + len(VOCAB), hidden_size=16, num_hidden_layers=1,
                        num_attention_heads=2, intermediate_size=32, max_position_embeddings=64)
    BertModel(config).save_pretrained(folder)
    return folder


def make_data(path: Path, per_class: int = 12, charts: int = 6, seed: int = 0) -> Path:
    rng = random.Random(seed)
    rows = []
    n = 0
    for label, words in WORDS.items():
        for i in range(per_class):
            n += 1
            text = "\n".join(" ".join(rng.choices(words.split(), k=8)) for _ in range(3))
            rows.append({"image": f"{n}.jpg", "text": text, "model_type": label, "page_type": label,
                         "page_subtype": label, "chart_id": f"c{i % charts}", "page_index": i,
                         "n_chars": len(text), "ocr_engine": "tesseract", "file_size": 100})
    for i in range(3):
        n += 1
        rows.append({"image": f"{n}.jpg", "text": "sleep apnea oxygen night " * 4, "model_type": RARE,
                     "chart_id": None, "page_index": None})
    n += 1
    rows.append({"image": f"{n}.jpg", "text": "x", "model_type": "Progress Note", "chart_id": None})
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def run(tmp_path: Path, tiny_model: Path, *extra: str) -> int:
    return train.main([
        "--data", str(tmp_path / "data.jsonl"), "--output", str(tmp_path / "out" / "page-family"),
        "--model-name", str(tiny_model), "--max-length", "32", "--batch-size", "8",
        "--lr", "1e-3", "--min-pages", "10", *extra,
    ])


def test_check_saves_nothing(tmp_path, tiny_model, capsys):
    make_data(tmp_path / "data.jsonl")
    assert run(tmp_path, tiny_model, "--check") == 0
    assert not (tmp_path / "out").exists()
    assert "minutes per epoch" in capsys.readouterr().out


def test_one_epoch_writes_every_file_then_resume(tmp_path, tiny_model, capsys):
    make_data(tmp_path / "data.jsonl")
    out = tmp_path / "out" / "page-family"
    assert run(tmp_path, tiny_model, "--epochs", "1") == 0
    for name in train.OUTPUT_FILES + ("config.json", "tokenizer.json"):
        assert (out / name).is_file(), name
    config = json.loads((out / "config.json").read_text())
    assert set(config["id2label"].values()) == set(WORDS)
    assert json.loads((out / "trained_classes.json").read_text()) == [m for m in train.load_taxonomy() if m in WORDS]
    skipped = {r["model_type"]: r for r in csv.DictReader((out / "skipped_classes.csv").open())}
    assert skipped[RARE]["pages"] == "3"
    resume_dir = tmp_path / "out" / "page-family-resume"
    assert (resume_dir / train.STATE_FILE).is_file()
    assert not (out / train.STATE_FILE).exists()  # resume file stays outside the model folder
    out_text = capsys.readouterr().out
    assert "Reload check passed" in out_text
    assert "fewer than 30 characters" in out_text

    import torch

    before = torch.load(resume_dir / train.STATE_FILE, weights_only=False)
    # Without --resume an existing run is not silently overwritten.
    with pytest.raises(SystemExit):
        run(tmp_path, tiny_model)
    assert run(tmp_path, tiny_model, "--resume") == 0
    after = torch.load(resume_dir / train.STATE_FILE, weights_only=False)
    assert after["epoch"] == 2
    assert after["classes"] == before["classes"]
    assert after["val_images"] == before["val_images"]
    log = list(csv.DictReader((out / "training_log.csv").open()))
    assert [r["epoch"] for r in log] == ["1", "2"]
    assert "Reload check passed" in capsys.readouterr().out


def test_group_split_keeps_each_chart_on_one_side(tmp_path):
    make_data(tmp_path / "data.jsonl", per_class=20, charts=7)
    taxonomy = train.load_taxonomy()
    records, _ = train.load_records(tmp_path / "data.jsonl", taxonomy, 30)
    classes, _ = train.choose_classes(records, taxonomy, 10)
    kept = [r for r in records if r["label"] in classes]
    val = train.group_split(kept, classes, 0.2, seed=1)
    val_groups = {r["group"] for r in kept if r["image"] in val}
    train_groups = {r["group"] for r in kept if r["image"] not in val}
    assert val and not (val_groups & train_groups)
    for c in classes:
        assert any(r["label"] == c and r["image"] not in val for r in kept)


def test_unknown_labels_are_named(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"image": "1.jpg", "text": "made up words " * 5, "model_type": "Old Family"}) + "\n")
    with pytest.raises(SystemExit, match="Old Family"):
        train.load_records(path, train.load_taxonomy(), 30)


def test_a_new_run_defaults_to_4_epochs_and_resume_adds_1(tmp_path, tiny_model):
    make_data(tmp_path / "data.jsonl")
    assert run(tmp_path, tiny_model) == 0
    out = tmp_path / "out" / "page-family"
    assert [r["epoch"] for r in csv.DictReader((out / "training_log.csv").open())] == ["1", "2", "3", "4"]
    assert run(tmp_path, tiny_model, "--resume") == 0
    assert [r["epoch"] for r in csv.DictReader((out / "training_log.csv").open())][-1] == "5"


def test_predict_classifies_text_with_the_saved_model(tmp_path, tiny_model, capsys):
    make_data(tmp_path / "data.jsonl")
    assert run(tmp_path, tiny_model, "--epochs", "1") == 0
    import predict

    clf = predict.Classifier(tmp_path / "out" / "page-family", device="cpu")
    rows = clf.predict("made up text for a page", top=3)
    assert len(rows) == 3
    assert all(r["model_type"] in WORDS for r in rows)
    assert rows[0]["probability"] >= rows[-1]["probability"]
    assert rows[0]["page_type"]  # from taxonomy.json
    capsys.readouterr()
    clf.show("made up text for a page")
    assert "model type" in capsys.readouterr().out
