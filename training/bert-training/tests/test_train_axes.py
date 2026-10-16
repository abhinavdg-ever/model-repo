"""train_axes.py helpers on a made-up taxonomy and made-up pages. No real data,
no internet, no training run: one forward/backward pass on a tiny random model."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import train_axes as ax  # noqa: E402

TAX = {
    "version": "test",
    "rules": {"section_L1": "Note", "fallback": "Others", "specialty_unknown": "Unknown",
              "specialty_other": "Others", "codeability_values": ["codeable", "non_codeable", "discharge",
                                                                  "needs_review"]},
    "L1": [{"name": n} for n in ("Note", "Result", "Consent", "Rare", "Flat")],
    "L2": {
        "Note": [{"name": "HPI"}, {"name": "Others"}],
        "Result": [{"name": "Lab"}, {"name": "Imaging"}, {"name": "Others"}],
        "Consent": [{"name": "HIPAA"}, {"name": "Others"}],
        "Rare": [{"name": "Thing"}],
        "Flat": [],
    },
    "L3": {"Result > Lab": [{"name": "CBC"}, {"name": "Urine"}, {"name": "Others"}],
           "Result > Imaging": [{"name": "CT"}, {"name": "Others"}]},
    "specialty": [{"name": n} for n in ("Cardiology", "Eye", "Others", "Unknown")],
}


def rec(l1, l2="", l3="", specialty="Unknown", codeability="codeable", image="x.jpg", chart_id=""):
    return {"image": image, "text": "t", "chart_id": chart_id, "L1": l1, "L2": l2, "L3": l3,
            "specialty": specialty, "codeability": codeability}


def records():
    rows = []
    rows += [rec("Note", "HPI") for _ in range(4)]
    rows += [rec("Result", "Lab", "CBC", "Cardiology") for _ in range(3)]
    rows += [rec("Result", "Lab", "Urine") for _ in range(1)]  # rare L3 → folds to Others
    rows += [rec("Result", "Imaging", "CT", "Eye") for _ in range(3)]
    rows += [rec("Result", "Imaging", "") for _ in range(2)]  # blank L3 under a parent with L3s
    rows += [rec("Consent", "HIPAA", codeability="non_codeable") for _ in range(3)]
    rows += [rec("Consent", "Others") for _ in range(1)]  # rare L2
    rows += [rec("Rare", "Thing")]  # folded L1
    rows += [rec("Flat")]
    return rows


@pytest.fixture
def space():
    return ax.build_label_space(records(), TAX, min_l1=3, min_l2=3, min_l3=3, min_specialty=3)


def test_validate_names_bad_labels():
    bad = [rec("Nope"), rec("Result", "HIPAA"), rec("Result", "Lab", "MRI"), rec("Note", "HPI", specialty="Dentistry"),
           rec("Note", "HPI", codeability="maybe")]
    problems = "\n".join(ax.validate(bad, TAX))
    for name in ("'Nope'", "Result > HIPAA", "Result > Lab > MRI", "Dentistry", "maybe"):
        assert name in problems
    assert ax.validate(records(), TAX) == []


def test_l1_fold_and_classes(space):
    assert space["L1"] == ["Note", "Result", "Consent", "OTHER"]
    assert space["L1_fold"]["Rare"] == "OTHER" and space["L1_fold"]["Flat"] == "OTHER"
    assert space["L2"] == ["Result > Lab", "Result > Imaging", "Consent > HIPAA"]  # Consent > Others is rare
    assert space["L2_by_L1"]["Note"] == [] and space["L2_by_L1"]["OTHER"] == []
    assert space["L3"] == ["Result > Lab > CBC", "Result > Lab > Others", "Result > Imaging > CT"]
    assert space["L3_fold"] == {"Result > Lab > Urine": "Result > Lab > Others"}
    assert space["specialty"] == ["Cardiology", "Eye", "Unknown"]  # Others has no page


def test_targets_and_exclusions(space):
    t, why = ax.encode_targets(rec("Note", "HPI"), space)
    assert t["L1"] == 0 and t["L2"] == ax.IGNORE and "section" in why["L2"]
    t, why = ax.encode_targets(rec("Rare", "Thing"), space)
    assert t["L1"] == space["L1"].index("OTHER") and "folded" in why["L2"]
    t, why = ax.encode_targets(rec("Result", "Imaging", ""), space)
    assert t["L2"] == 1 and t["L3"] == ax.IGNORE and why["L3"].startswith("blank L3")
    t, _ = ax.encode_targets(rec("Result", "Lab", "Urine"), space)
    assert space["L3"][t["L3"]] == "Result > Lab > Others"
    t, why = ax.encode_targets(rec("Consent", "Others"), space)
    assert t["L2"] == ax.IGNORE and why["L2"] == "rare L2" and why["L3"] == "L2 has no named L3"
    t, why = ax.encode_targets(rec("Consent", "HIPAA", specialty="Others"), space)
    assert t["specialty"] == ax.IGNORE and why["specialty"] == "rare specialty"
    assert t["codeability"] == 0


def test_masks(space):
    l2, l3 = ax.mask_rows(space)
    assert l2[space["L1"].index("Result")] == [True, True, False]
    assert l2[space["L1"].index("Note")] == [False, False, False]
    assert l3[space["L2"].index("Result > Imaging")] == [False, False, True]
    assert l3[space["L2"].index("Consent > HIPAA")] == [False, False, False]


def test_head_tail():
    ids = list(range(100))
    out = ax.head_tail(ids, 22, 0.85, -1, -2)
    assert len(out) == 22 and out[0] == -1 and out[-1] == -2
    assert out[1:18] == list(range(17)) and out[18:21] == [97, 98, 99]  # 17 head + 3 tail
    assert ax.head_tail([5, 6], 22, 0.85, -1, -2) == [-1, 5, 6, -2]
    assert ax.head_tail(ids, 12, 1.0, -1, -2)[1:-1] == list(range(10))


def test_groups():
    rows = [rec("Note", image="53688890_240126_2000_Pg2.jpg", chart_id="53688890_240126_2000"),
            rec("Note", image="53688890_Pg3.jpg", chart_id="53688890"),
            rec("Note", image="IMG_100.HEIC"), rec("Note", image="IMG_101.HEIC"),
            rec("Note", image="polysomnography_report_01.png")]
    g = ax.assign_groups(rows)
    assert g[0] == g[1] == "chart:53688890"
    assert g[2:] == ["page:IMG_100.HEIC", "page:IMG_101.HEIC", "page:polysomnography_report_01.png"]


def test_split_keeps_groups_whole_and_classes_in_train():
    labels, groups = [], []
    for c in range(30):
        for p in range(5):
            labels.append("A" if c % 3 else "B")
            groups.append(f"g{c}")
    labels += ["C", "C"]
    groups += ["solo", "solo"]
    sides = ax.group_split(labels, groups, 0.15, 0.15, seed=1)
    by_group = {}
    for g, s in zip(groups, sides):
        by_group.setdefault(g, set()).add(s)
    assert all(len(s) == 1 for s in by_group.values())
    assert {"train", "val", "test"} <= set(sides)
    for c in "ABC":
        assert any(lab == c and s == "train" for lab, s in zip(labels, sides))
    assert "test" not in ax.group_split(labels, groups, 0.2, 0.0, seed=1)


def test_paths_and_hierarchical(space):
    assert ax.path_nodes("Note", "HPI", "", space) == ["Note"]
    assert ax.path_nodes("OTHER", "Thing", "", space) == ["OTHER"]
    assert ax.path_nodes("Consent", "HIPAA", "x", space) == ["Consent", "Consent > HIPAA"]
    assert ax.path_nodes("Result", "Lab", "CBC", space) == ["Result", "Result > Lab", "Result > Lab > CBC"]
    assert ax.gold_nodes(rec("Result", "Lab", "Urine"), space)[-1] == "Result > Lab > Others"
    assert ax.gold_nodes(rec("Rare", "Thing"), space) == ["OTHER"]
    gold = [["Result", "Result > Lab", "Result > Lab > CBC"], ["Note"]]
    pred = [["Result", "Result > Imaging", "Result > Imaging > CT"], ["Note"]]
    h = ax.hierarchical_pr(gold, pred)
    assert h["precision"] == pytest.approx(2 / 4) and h["recall"] == pytest.approx(2 / 4)
    assert h["full_path_accuracy"] == 0.5


def test_class_scores_macro_ignores_absent_classes():
    s = ax.class_scores([0, 0, 1], [0, 1, 1], k=4)
    assert s["support"] == [2, 1, 0, 0]
    assert s["macro_f1"] == pytest.approx((2 / 3 + 2 / 3) / 2)
    assert s["confusion"][0] == [1, 1, 0, 0]


def test_route():
    assert ax.route([("Result", 0.8), ("Note", 0.1)], "OTHER") == "model"
    assert ax.route([("Result", 0.55), ("Note", 0.5)], "OTHER") == "canon"  # no margin
    assert ax.route([("OTHER", 0.9), ("Note", 0.1)], "OTHER") == "canon"
    assert ax.route([("Result", 0.2), ("Note", 0.19)], "OTHER") == "review"


@pytest.fixture(scope="module")
def tiny_encoder(tmp_path_factory) -> Path:
    from transformers import BertTokenizerFast, DistilBertConfig, DistilBertModel

    folder = tmp_path_factory.mktemp("tiny-distilbert")
    words = ["chief", "complaint", "lab", "result", "ct", "scan", "consent", "hipaa"]
    vocab = folder / "vocab.txt"
    vocab.write_text("\n".join(["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]", *words]), encoding="utf-8")
    BertTokenizerFast(vocab_file=str(vocab)).save_pretrained(folder)
    config = DistilBertConfig(vocab_size=5 + len(words), dim=16, n_layers=1, n_heads=2, hidden_dim=32,
                              max_position_embeddings=64)
    DistilBertModel(config).save_pretrained(folder)
    return folder


def test_forward_backward_mask_and_reload(space, tiny_encoder, tmp_path):
    """Heads, masking, loss and save/load wire up: four made-up pages, one step."""
    torch = pytest.importorskip("torch")
    from transformers import AutoTokenizer

    pages = [rec("Note", "HPI"), rec("Result", "Lab", "CBC", "Cardiology"),
             rec("Result", "Imaging", "CT", "Eye"), rec("Rare", "Thing")]
    texts = ["chief complaint", "lab result " * 40, "ct scan", "consent hipaa"]
    tokenizer = AutoTokenizer.from_pretrained(tiny_encoder)
    ids = ax.encode_texts(tokenizer, texts, 16, 0.85)
    assert max(len(x) for x in ids) == 16
    targets = [ax.encode_targets(p, space)[0] for p in pages]
    loader = ax.make_loader(ids, targets, tokenizer.pad_token_id, 4, False, 0)
    batch = next(iter(loader))
    model = ax.build_model(str(tiny_encoder), space, 0.0)
    logits = model(batch["input_ids"], batch["attention_mask"])
    assert {h: logits[h].shape[-1] for h in ax.HEADS} == {
        "L1": 4, "L2": 3, "L3": 3, "specialty": 3, "codeability": 4}
    masked = ax.masked_l2(logits, model, batch["L1"])
    assert (masked[1, 2] == ax.MASKED) and (masked[1, :2] != ax.MASKED).all()  # Result: no Consent > HIPAA
    loss_fns = {h: torch.nn.CrossEntropyLoss(weight=ax.class_weights(targets, h, len(space[h])),
                                             ignore_index=ax.IGNORE) for h in ax.HEADS}
    loss = ax.compute_loss(logits, batch, model, loss_fns)
    assert torch.isfinite(loss) and loss.item() > 0
    loss.backward()
    assert model.heads["L3"].weight.grad is not None
    assert model.heads["L3"].weight.grad.abs().sum() > 0

    rows = ax.decode({h: v.detach() for h, v in logits.items()}, model, space)
    for row in rows:
        if row["L2"] is not None:
            assert f"{row['L1']} > {row['L2']}" in space["L2_by_L1"][row["L1"]]
        else:
            assert not space["L2_by_L1"][row["L1"]]
        if row["L3"] is not None:
            assert f"{row['L1']} > {row['L2']} > {row['L3']}" in space["L3"]

    ax.save_model(tmp_path / "m", model, tokenizer, space, {"max_length": 16, "head_fraction": 0.85,
                                                            "dropout": 0.0})
    model.eval()
    again, _, saved = ax.load_model(tmp_path / "m")
    assert saved["L3"] == space["L3"]
    with torch.no_grad():
        a = model(batch["input_ids"], batch["attention_mask"])["L1"]
        b = again(batch["input_ids"], batch["attention_mask"])["L1"]
    assert torch.allclose(a, b, atol=1e-5)

    import predict_axes

    clf = predict_axes.AxesClassifier(tmp_path / "m", "cpu")
    out = clf.predict("lab result")
    assert out["route"] in ("model", "canon", "review") and len(out["L1_top"]) == 3


def test_evaluation_writes_every_output(space, tiny_encoder, tmp_path):
    """The post-training half of main(): records from a JSONL, split, an
    untrained model scored (inference only) and every output file written."""
    import argparse
    import csv
    import json

    from transformers import AutoTokenizer

    data = tmp_path / "data.jsonl"
    rows = []
    for n, page in enumerate(records() * 3):
        rows.append({**page, "image": f"IMG_{1000 + n * 2}.HEIC", "text": "lab result ct scan chief complaint"})
    rows.append({**rec("Note", "HPI"), "image": "short.jpg", "text": "x"})
    data.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    loaded, short = ax.load_records(data, TAX, min_chars=5)
    assert short == 1 and len(loaded) == len(rows) - 1
    sp = ax.build_label_space(loaded, TAX, min_l1=3, min_l2=3, min_l3=3, min_specialty=3)
    targets = [ax.encode_targets(r, sp)[0] for r in loaded]
    groups = ax.assign_groups(loaded)
    sides = ax.group_split([sp["L1"][t["L1"]] for t in targets], groups, 0.2, 0.2, seed=0)
    split = {s: [i for i, x in enumerate(sides) if x == s] for s in ("train", "val", "test")}
    tokenizer = AutoTokenizer.from_pretrained(tiny_encoder)
    ids = ax.encode_texts(tokenizer, [r["text"] for r in loaded], 16, 0.85)
    out = tmp_path / "out"
    ax.save_model(out, ax.build_model(str(tiny_encoder), sp), tokenizer, sp,
                  {"max_length": 16, "head_fraction": 0.85, "dropout": 0.1})
    model, _, saved = ax.load_model(out)
    args = argparse.Namespace(batch_size=4, seed=0, route_model=0.5, route_canon=0.25)
    report = ax.evaluate_saved(model, saved, loaded, targets, groups, sides, ids, 0, args, "cpu")
    ax.write_outputs(out, report, saved, loaded, targets, split)
    summary = report["summary"]
    assert summary["scored_on"] == ("test" if split["test"] else "val")
    assert set(summary["macro_f1"]) == set(ax.HEADS)
    for name in ("category_wise_metrics.csv", "confusion/L1.csv", "confusion/L2_result.csv", "errors.csv",
                 "predictions_val.csv", "heads.pt", "label_space.json", "encoder/config.json"):
        assert (out / name).is_file(), name
    header = next(csv.reader((out / "errors.csv").open()))
    assert "text" not in header and "route" in header
