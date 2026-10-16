"""tune_thresholds.py: the sweep and the suggestion, on a made-up predictions CSV."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import tune_thresholds as tune  # noqa: E402

HEADER = ["image", "split", "group", "route", "L1_confidence", "L1_top3", "path_correct",
          "gold_L1", "pred_L1", "gold_L2", "pred_L2", "gold_L3", "pred_L3",
          "gold_specialty", "pred_specialty", "gold_codeability", "pred_codeability",
          "L2_confidence", "L3_confidence", "specialty_confidence", "codeability_confidence",
          "specialty_top3"]


def row(l1_conf, gold_l1, pred_l1, *, spec=("Unknown", "Unknown", 0.9, "Unknown 0.90; Cardiology 0.05")):
    gold_spec, pred_spec, spec_conf, spec_top = spec
    return {
        "image": "x.jpg", "split": "val", "group": "page:x", "route": "model",
        "L1_confidence": l1_conf, "L1_top3": f"{pred_l1} {l1_conf:.2f}; Progress Note 0.05",
        "path_correct": "y", "gold_L1": gold_l1, "pred_L1": pred_l1,
        "gold_L2": "Radiology", "pred_L2": "Radiology", "gold_L3": "", "pred_L3": "",
        "gold_specialty": gold_spec, "pred_specialty": pred_spec,
        "gold_codeability": "non_codeable", "pred_codeability": "non_codeable",
        "L2_confidence": 0.8, "L3_confidence": "", "specialty_confidence": spec_conf,
        "codeability_confidence": 0.9, "specialty_top3": spec_top,
    }


def test_suggests_the_lowest_threshold_that_is_precise_enough():
    # Wrong answers sit below 0.5, right ones above: 0.50 is the first precise gate.
    rows = [row(0.45, "Lab Report/Result", "Administrative") for _ in range(5)]
    rows += [row(0.80, "Lab Report/Result", "Lab Report/Result") for _ in range(20)]
    table = tune.tables(rows, l1_margin=0.10, specialty_lead=0.20)["L1"]
    assert tune.suggest(table, target=0.9, min_pages=10) == 0.5
    at_030 = next(r for r in table if r["threshold"] == 0.3)
    assert (at_030["decided"], round(at_030["precision"], 2)) == (25, 0.8)


def test_unknown_specialty_is_never_counted_as_decided():
    rows = [row(0.9, "A", "A") for _ in range(12)]
    table = tune.tables(rows, 0.1, 0.2)["specialty"]
    assert all(r["pages"] == 0 and r["decided"] == 0 for r in table)
    assert tune.suggest(table, 0.9, 10) is None


def test_specialty_lead_is_applied():
    close = ("Cardiology", "Cardiology", 0.8, "Cardiology 0.80; Nursing 0.70")
    rows = [row(0.9, "A", "A", spec=close) for _ in range(12)]
    assert all(r["decided"] == 0 for r in tune.tables(rows, 0.1, 0.2)["specialty"])
    assert tune.tables(rows, 0.1, 0.0)["specialty"][0]["decided"] == 12


def test_cli_reads_a_csv_and_prints_suggestions(tmp_path, capsys):
    path = tmp_path / "predictions_val.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, HEADER)
        writer.writeheader()
        writer.writerows(row(0.8, "A", "A") for _ in range(12))
    assert tune.main(["--predictions", str(path)]) == 0
    out = capsys.readouterr().out
    assert '"route_model": 0.3' in out and '"specialty_min": null' in out
