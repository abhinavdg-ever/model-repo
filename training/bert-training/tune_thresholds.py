#!/usr/bin/env python3
"""Pick the page-axes gates from a trained model's validation predictions.

    python tune_thresholds.py                                  # output/page-axes/predictions_val.csv
    python tune_thresholds.py --predictions BASE_PATH/page-axes/predictions_val.csv
    python tune_thresholds.py --target 0.95                    # stricter

For each head the pipeline gates (L1, L2, specialty) it sweeps the
threshold and prints, per value, how many pages the model would decide
(coverage) and how many of those it gets right (precision). The suggested
value is the lowest threshold whose precision reaches ``--target`` on at least
``--min-pages`` decided pages. Copy the suggestions into
``core-pipeline/stages/lib/keyword-canon/page_arbitration.json`` → ``axes``.
Below a gate the keyword canon decides, so a stricter gate costs coverage of
the model, not of the page. L3 and codeability are not tuned here: the canon decides them.

Validation, not test: tune on validation and keep test for the final score.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
DEFAULT_PREDICTIONS = HERE / "output" / "page-axes" / "predictions_val.csv"
THRESHOLDS = [round(0.30 + 0.05 * i, 2) for i in range(14)]  # 0.30 … 0.95
NOT_ASSERTED = ("Unknown", "Others")
OTHER_L1 = "OTHER"
# page_arbitration.json → axes key for each head.
SETTING = {"L1": "route_model", "L2": "l2_min", "specialty": "specialty_min"}


def ranking(cell: str) -> list[tuple[str, float]]:
    """'Lab Report/Result 0.62; Progress Note 0.20' → [(name, p), …]."""
    out = []
    for part in (cell or "").split(";"):
        name, _, p = part.strip().rpartition(" ")
        if name:
            out.append((name, float(p)))
    return out


def lead(cell: str) -> float:
    top = ranking(cell)
    if not top:
        return 0.0
    return top[0][1] - (top[1][1] if len(top) > 1 else 0.0)


def _number(cell: str) -> Optional[float]:
    return float(cell) if cell not in ("", None) else None


def sweep(rows: list[dict], eligible: Callable[[dict], bool], decided: Callable[[dict, float], bool],
          correct: Callable[[dict], bool]) -> list[dict]:
    pool = [r for r in rows if eligible(r)]
    out = []
    for t in THRESHOLDS:
        chosen = [r for r in pool if decided(r, t)]
        right = sum(1 for r in chosen if correct(r))
        out.append({"threshold": t, "pages": len(pool), "decided": len(chosen),
                    "coverage": len(chosen) / len(pool) if pool else 0.0,
                    "precision": right / len(chosen) if chosen else 0.0})
    return out


def suggest(table: list[dict], target: float, min_pages: int) -> Optional[float]:
    for row in table:
        if row["decided"] >= min_pages and row["precision"] >= target:
            return row["threshold"]
    return None


def tables(rows: list[dict], l1_margin: float, specialty_lead: float) -> dict[str, list[dict]]:
    def l1_decided(r, t):
        return r["pred_L1"] != OTHER_L1 and _number(r["L1_confidence"]) >= t and lead(r["L1_top3"]) >= l1_margin

    def specialty_decided(r, t):
        confidence = _number(r["specialty_confidence"])
        return (r["pred_specialty"] not in ("", *NOT_ASSERTED) and confidence is not None
                and confidence >= t and lead(r.get("specialty_top3", "")) >= specialty_lead)

    return {
        "L1": sweep(rows, lambda r: True, l1_decided, lambda r: r["gold_L1"] == r["pred_L1"]),
        # L2 is gated only once the model decided L1, so judge it where L1 is right.
        "L2": sweep(rows, lambda r: bool(r["pred_L2"]) and r["gold_L1"] == r["pred_L1"],
                    lambda r, t: (_number(r["L2_confidence"]) or 0.0) >= t,
                    lambda r: r["gold_L2"] == r["pred_L2"]),
        "specialty": sweep(rows, lambda r: r["gold_specialty"] not in ("", *NOT_ASSERTED), specialty_decided,
                           lambda r: r["gold_specialty"] == r["pred_specialty"]),
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS, help="predictions_val.csv")
    p.add_argument("--target", type=float, default=0.90, help="precision a gate must reach")
    p.add_argument("--min-pages", type=int, default=10, help="decided pages a suggestion needs")
    p.add_argument("--l1-margin", type=float, default=0.10, help="axes.route_margin")
    p.add_argument("--specialty-lead", type=float, default=0.20, help="axes.specialty_lead")
    args = p.parse_args(argv)

    if not args.predictions.is_file():
        raise SystemExit(f"No predictions at {args.predictions} — train with train_axes.py first")
    with args.predictions.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if "specialty_top3" not in (rows[0] if rows else {}):
        print("Note: no specialty_top3 column (model trained before it existed); the specialty lead is ignored.")
        args.specialty_lead = 0.0

    suggestions = {}
    for head, table in tables(rows, args.l1_margin, args.specialty_lead).items():
        print(f"\n{head}  ({table[0]['pages']} pages scored)")
        print("  threshold  decided  coverage  precision")
        for row in table:
            print(f"  {row['threshold']:>9.2f}  {row['decided']:>7}  {row['coverage']:>8.0%}  {row['precision']:>9.0%}")
        value = suggest(table, args.target, args.min_pages)
        suggestions[SETTING[head]] = value
        print(f"  → {SETTING[head]}: " + (f"{value}" if value is not None else
                                           f"none reaches {args.target:.0%} on {args.min_pages}+ pages; "
                                           "keep the current value or let the canon decide"))
    print("\nSuggested page_arbitration.json → axes values (null: keep yours):")
    print(json.dumps(suggestions, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
