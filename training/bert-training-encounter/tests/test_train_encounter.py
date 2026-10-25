"""train_encounter.py helpers on made-up pages. No real data, no training run."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import train_encounter as enc  # noqa: E402

IP, F2F, EV, HV, NA, NONE = enc.CLASSES


def page(label, image, chart_id="", basis="stated", train=True, visit="", page_index=1, text="plenty of page text for the minimum"):
    return {"image": image, "text": text, "encounter_type": label, "basis": basis, "train": train,
            "visit": visit, "chart_id": chart_id, "page_index": str(page_index)}


def write(tmp_path, rows, name="tags.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_load_records_drops_short_pages_and_refuses_unknown_classes(tmp_path):
    rows, short = enc.load_records(write(tmp_path, [page(F2F, "a"), page(NA, "b", text="x")]), 20)
    assert [r["image"] for r in rows] == ["a"] and short == 1
    with pytest.raises(SystemExit, match="Telehealth"):
        enc.load_records(write(tmp_path, [page("Telehealth", "c")]), 20)


def test_default_rows_are_not_learned_and_inherited_rows_weigh_less(tmp_path):
    rows, _ = enc.load_records(write(tmp_path, [page(EV, "d", basis="default", train=False),
                                                page(F2F, "i", basis="inherited"), page(F2F, "s")]), 20)
    assert [enc.loss_weight(r, 0.5) for r in rows] == [0.0, 0.5, 1.0]


def test_split_keeps_charts_whole_and_mock_pages_in_training(tmp_path):
    rows = [page(enc.CLASSES[i % 5], f"p{i}", chart_id=f"{1000 + i // 4}_24012{i % 2}") for i in range(80)]
    rows += [page(HV, f"m{i}") for i in range(3)]
    records, _ = enc.load_records(write(tmp_path, rows), 5)
    for r in records[-3:]:
        r["synthetic"] = True
    groups = enc.assign_groups(records)
    sides = enc.group_split(records, groups, 0.2, 0.2, seed=0)
    by_group = {}
    for g, s in zip(groups, sides):
        by_group.setdefault(g, set()).add(s)
    assert all(len(s) == 1 for s in by_group.values())
    assert sides[-3:] == ["train"] * 3
    assert {"val", "test"} <= set(sides)


def test_document_vote_follows_the_pipeline_rule():
    assert enc.document_vote([(1, NA), (2, NA)]) == NA
    assert enc.document_vote([(1, NA), (2, NA), (3, F2F)]) == F2F  # one real vote beats any number of NA
    assert enc.document_vote([(1, IP), (2, F2F), (3, F2F)]) == IP  # 2 vs 2: a tie goes to the first page
    assert enc.document_vote([(1, IP), (2, F2F), (3, F2F), (4, F2F)]) == F2F
    assert enc.document_vote([(1, NA), (2, F2F)]) == F2F  # NA is set aside once a real vote exists
    assert enc.document_vote([]) == NONE
    assert enc.document_vote([(1, NONE), (2, F2F), (3, NONE)]) == F2F  # no evidence abstains
    assert enc.document_vote([(1, NONE), (2, NONE)]) == NONE


def test_class_scores_and_visit_scores():
    s = enc.class_scores([0, 0, 1, 4], [0, 1, 1, 4])
    assert s["recall"][0] == 0.5 and s["confusion"][0][1] == 1
    rows = [{"visit": "v1", "page_index": 1, "visit_label": F2F, "pred": F2F},
            {"visit": "v1", "page_index": 2, "visit_label": F2F, "pred": NA},
            {"visit": "v2", "page_index": 1, "visit_label": IP, "pred": F2F},
            {"visit": "", "page_index": 1, "visit_label": NA, "pred": NA}]
    v = enc.visit_scores(rows)
    assert v["n"] == 2 and v["accuracy"] == 0.5


def test_head_tail_keeps_start_and_end():
    ids = list(range(100))
    out = enc.head_tail(ids, 12, 0.8, 101, 102)
    assert out[0] == 101 and out[-1] == 102 and len(out) == 12
    assert out[1:9] == list(range(8)) and out[9:11] == [98, 99]


def test_the_page_target_reads_encounter_on_page_and_keeps_the_visit(tmp_path):
    rows = [{**page(F2F, "a"), "encounter_on_page": NONE}, {**page(IP, "b"), "encounter_on_page": IP},
            page(EV, "c")]  # a synthetic-style row with no page-level column
    records, _ = enc.load_records(write(tmp_path, rows), 20, target="page")
    assert [(r["label"], r["visit_label"]) for r in records] == [(NONE, F2F), (IP, IP), (EV, EV)]
    visit, _ = enc.load_records(write(tmp_path, rows), 20, target="visit")
    assert [r["label"] for r in visit] == [F2F, IP, EV]


def test_a_five_class_model_still_loads_its_label_space():
    assert enc.VISIT_CLASSES == enc.CLASSES[:5] and enc.NO_EVIDENCE not in enc.VISIT_CLASSES
