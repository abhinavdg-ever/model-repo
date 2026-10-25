"""train_axes.py helpers on a made-up taxonomy and made-up pages. No real data,
no internet, no training run: one forward/backward pass on a tiny random model."""
from __future__ import annotations

import sys
from collections import Counter
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
    for name in ("'Nope'", "Result > HIPAA", "Result > Lab > MRI", "Dentistry"):
        assert name in problems
    assert "maybe" not in problems  # codeability is not learned, so not checked
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
    assert "codeability" not in t and "codeability" not in space  # not learned (2026-10-25)


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
        "L1": 4, "L2": 3, "L3": 3, "specialty": 3}
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
        # The best L2 / L3 under every L1 with L2s: the Final step re-places a page with it.
        assert set(row["by_L1"]) == {l1 for l1 in space["L1"] if space["L2_by_L1"].get(l1)}
        for l1, best in row["by_L1"].items():
            assert f"{l1} > {best['L2']}" in space["L2_by_L1"][l1]
            if best["L3"] is not None:
                assert f"{l1} > {best['L2']} > {best['L3']}" in space["L3"]
        # The best paths: whole paths the taxonomy allows, best first, 5 at most,
        # at most 3 under one L1.
        scores = [x["score"] for x in row["paths"]]
        assert 1 <= len(row["paths"]) <= ax.TOP_PATHS and scores == sorted(scores, reverse=True)
        assert max(Counter(x["L1"] for x in row["paths"]).values()) <= ax.PATHS_PER_L1
        for x in row["paths"]:
            assert x["path"] == " > ".join(n for n in (x["L1"], x["L2"], x["L3"]) if n)
            assert x["L2"] is None or f"{x['L1']} > {x['L2']}" in space["L2_by_L1"][x["L1"]]
            assert x["L3"] is None or x["path"] in space["L3"]

    # Over every path, the scores are a probability distribution.
    l2_given, l3_given = ax.path_probabilities({h: v.detach() for h, v in logits.items()}, model, space)
    l1_prob = torch.softmax(logits["L1"].detach(), -1)
    for i in range(l1_prob.shape[0]):
        every = ax.top_paths(l1_prob[i].tolist(), l2_given[i].tolist(), l3_given[i].tolist(), space,
                             k=1000, per_l1=1000)
        assert sum(x["score"] for x in every) == pytest.approx(1.0, abs=1e-4)

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


def test_synthetic_pages_stay_in_training():
    rows = [rec("Note", "HPI", chart_id=str(100 + i), image=f"r{i}.jpg") for i in range(20)]
    rows += [{**rec("Note", "HPI", image=f"m{i}.jpg"), "synthetic": True} for i in range(5)]
    sides = ax.split_real_pages(rows, [r["L1"] for r in rows], ax.assign_groups(rows), 0.2, 0.2, seed=0)
    assert all(side == "train" for side, r in zip(sides, rows) if r.get("synthetic"))
    assert {"val", "test"} & set(sides[:20])


def test_unknown_specialty_can_be_left_out_of_the_loss():
    rows = records()
    kept = ax.build_label_space(rows, TAX, min_l1=2, min_l2=2, min_l3=2, min_specialty=1)
    assert "Unknown" in kept["specialty"]
    space = ax.build_label_space(rows, TAX, min_l1=2, min_l2=2, min_l3=2, min_specialty=1,
                                 specialty_unknown="ignore")
    assert "Unknown" not in space["specialty"]
    targets, why = ax.encode_targets(rec("Note", "HPI", specialty="Unknown"), space)
    assert targets["specialty"] == ax.IGNORE and why["specialty"] == "Unknown specialty"


def test_lab_note_confusion_both_ways():
    classes = ["Progress Note", "Lab Report", "Other"]
    confusion = [[8, 2, 0], [3, 6, 1], [0, 0, 5]]
    c = ax.lab_note_confusion(confusion, classes)
    assert c == {"lab_pages": 10, "lab_as_note": 3, "note_pages": 10, "note_as_lab": 2}
    assert "3 of 10" in ax.lab_note_line(c) and "2 of 10" in ax.lab_note_line(c)
    assert ax.lab_note_confusion(confusion, ["A", "B", "C"]) is None


def test_top_paths_rank_whole_paths():
    space = {"L1": ["Result", "Consent", "Flat"],
             "L2": ["Result > Lab", "Result > Path", "Consent > HIPAA"],
             "L3": ["Result > Lab > CBC", "Result > Lab > BMP"],
             "L2_by_L1": {"Result": ["Result > Lab", "Result > Path"], "Consent": ["Consent > HIPAA"]},
             "L3_by_L2": {"Result > Lab": ["Result > Lab > CBC", "Result > Lab > BMP"]}}
    l2_given = [[0.5, 0.5, 0.0], [0.0, 0.0, 1.0], [0.0, 0.0, 0.0]]
    l3_given = [[0.6, 0.4], [0.5, 0.5], [0.5, 0.5]]
    top = ax.top_paths([0.6, 0.25, 0.15], l2_given, l3_given, space, k=3)
    # Two of the three share the L1 Result; every allowed path was scored.
    assert [x["path"] for x in top] == ["Result > Path", "Consent > HIPAA", "Result > Lab > CBC"]
    assert [x["score"] for x in top] == pytest.approx([0.30, 0.25, 0.18])
    assert top[0]["L3"] is None and top[2]["L3_confidence"] == pytest.approx(0.6)
    # At most per_l1 under one L1: Result's third path gives way to Flat.
    capped = ax.top_paths([0.6, 0.25, 0.15], l2_given, l3_given, space, k=5, per_l1=2)
    assert [x["path"] for x in capped] == ["Result > Path", "Consent > HIPAA", "Result > Lab > CBC", "Flat"]
    # An L1 with no L2 is a path of its own; with no L2 head, every path is an L1.
    assert [x["path"] for x in ax.top_paths([0.6, 0.25, 0.15], None, None, space)] == ["Result", "Consent", "Flat"]
    # The default keeps 5: Result > Lab > BMP (0.12) is Result's third, still allowed.
    assert [x["path"] for x in ax.top_paths([0.6, 0.25, 0.15], l2_given, l3_given, space)][3:] == [
        "Flat", "Result > Lab > BMP"]


def test_label_split_spreads_rare_labels_and_keeps_the_rarest_in_train():
    # 12 charts: L1 A everywhere; label "X" in 4 charts, label "Y" in 1 chart only.
    labels, groups = [], []
    for g in range(12):
        labs = ["L1:A"] + (["L2:X"] if g < 4 else []) + (["L2:Y"] if g == 11 else [])
        labels += [labs, labs]
        groups += [f"chart:{g}"] * 2
    sides = ax.label_split(labels, groups, 0.25, 0.25, seed=1, min_eval_groups=3)
    by_group = {g: s for g, s in zip(groups, sides)}
    assert len(set(by_group.values())) == 3                       # every side used
    assert all(sides[i] == sides[i + 1] for i in range(0, len(sides), 2))  # groups stay whole
    assert by_group["chart:11"] == "train"                        # Y is in 1 group: train only
    x_sides = {by_group[f"chart:{g}"] for g in range(4)}
    assert "train" in x_sides and len(x_sides) >= 2               # X reaches evaluation too


def test_repeat_factors_lift_small_classes_only(space):
    targets = [ax.encode_targets(r, space)[0] for r in records()]
    repeat = ax.repeat_factors(targets, space, threshold=4, max_repeat=4.0)
    assert len(repeat) == len(targets) and all(1.0 <= r <= 4.0 for r in repeat)
    note = records().index(next(r for r in records() if r["L1"] == "Note"))
    assert repeat[note] == pytest.approx(1.0)  # Note has 4 pages: not small
    assert max(repeat) > 1.0
    assert ax.repeat_factors(targets, space, threshold=0, max_repeat=4.0) == [1.0] * len(targets)


def test_class_weight_modes(space):
    pytest.importorskip("torch")
    targets = [ax.encode_targets(r, space)[0] for r in records()]
    balanced = ax.class_weights(targets, "L1", len(space["L1"]), "balanced")
    soft = ax.class_weights(targets, "L1", len(space["L1"]), "sqrt")
    assert soft.tolist() == pytest.approx([w ** 0.5 for w in balanced.tolist()])
    assert ax.class_weights(targets, "L1", len(space["L1"]), "none").tolist() == [1.0] * len(space["L1"])
    # Repeats count as pages: doubling every page leaves the weights alone.
    assert ax.class_weights(targets, "L1", len(space["L1"]), "balanced", [2.0] * len(targets)).tolist() == \
        pytest.approx(balanced.tolist())


def test_label_split_properties_the_pack_asks_for():
    """No group on two sides; every label in 3+ groups has train pages; mock pages are
    always train (Claude Cowork pack, 2026-10-25)."""
    import random as rnd

    rng = rnd.Random(7)
    records, labels, groups = [], [], []
    for g in range(60):
        pages = rng.randint(1, 4)
        labs = ["L1:" + rng.choice("ABCD")] + (["L2:" + rng.choice("xyz")] if rng.random() < 0.7 else []) + (
            ["specialty:rare"] if g in (3, 40) else [])
        for p in range(pages):
            records.append({"synthetic": False})
            labels.append(labs)
            groups.append(f"chart:{g}")
    for m in range(5):  # mock pages, each its own group
        records.append({"synthetic": True})
        labels.append(["L1:A"])
        groups.append(f"page:mock{m}")
    sides = ax.split_by_labels(records, labels, groups, 0.15, 0.15, seed=42, min_eval_groups=3)

    by_group = {}
    for g, s in zip(groups, sides):
        by_group.setdefault(g, set()).add(s)
    assert all(len(s) == 1 for s in by_group.values())              # no group on two sides
    assert {"train", "val", "test"} <= set(sides)
    groups_of = {}
    for labs, g in zip(labels, groups):
        for lab in labs:
            groups_of.setdefault(lab, set()).add(g)
    for lab, gs in groups_of.items():
        if len(gs) >= 3:
            assert any(lab in labs and s == "train" for labs, s in zip(labels, sides)), lab
    assert all(s == "train" for r, s in zip(records, sides) if r["synthetic"])   # mock pages train
    assert all(by_group[f"chart:{g}"] == {"train"} for g in (3, 40))  # a label in 2 groups stays in train


def test_the_old_l1_split_still_works():
    records = [{"synthetic": False} for _ in range(40)] + [{"synthetic": True}]
    l1 = [("A", "B")[i % 2] for i in range(40)] + ["A"]
    groups = [f"chart:{i // 2}" for i in range(40)] + ["page:mock"]
    sides = ax.split_real_pages(records, l1, groups, 0.2, 0.2, seed=1)
    assert sides[-1] == "train" and {"train", "val", "test"} <= set(sides)
