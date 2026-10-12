#!/usr/bin/env python3
"""Score page-type predictions on real pages against the client's ground truth.

The ground-truth label of a real page is a free-text value ("Progress note",
"Lab Report", ...). acceptable_ground_truth_by_model_type.csv lists, for each
model type, which values count: match_level "Exact", or "Broader label (page
marked only as Progress Note)". A value is matched on its match_key: lower
case, spaces collapsed.

    python evaluate.py pages.csv                          # predictions already in the CSV
    python evaluate.py pages.csv --model output/page-family   # predict from a text column

Input CSV columns (names can be changed with the options):
    ground_truth           the client's label for the page
    predicted_model_type   the model type predicted (or use --model and a text column)
    text                   page text, only read with --model; never printed

Accuracy is reported twice: counting only Exact matches, and counting Exact plus
Broader-label matches. A page whose predicted model type has no ground-truth
value in the pivot ("No ground-truth value in the pivot") is not scorable: it is
counted separately, not as wrong. Prints labels and counts only, never text.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
ACCEPTABLE = HERE / "acceptable_ground_truth_by_model_type.csv"
EXACT = "Exact"
BROADER_PREFIX = "Broader label"
NOT_SCORABLE = "No ground-truth value in the pivot"


def match_key(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def load_acceptable(path: Path) -> tuple[dict[str, set[str]], dict[str, set[str]], set[str]]:
    """model type -> exact keys, model type -> broader keys, model types not scorable."""
    exact: dict[str, set[str]] = defaultdict(set)
    broader: dict[str, set[str]] = defaultdict(set)
    flagged: set[str] = set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            model_type = (row.get("model_type") or "").strip()
            level = (row.get("match_level") or "").strip()
            key = match_key(row.get("match_key") or row.get("ground_truth_value") or "")
            if level == EXACT and key:
                exact[model_type].add(key)
            elif level.startswith(BROADER_PREFIX) and key:
                broader[model_type].add(key)
            elif level == NOT_SCORABLE:
                flagged.add(model_type)
    # A model type is not scorable when the pivot has no value for it at all.
    not_scorable = {m for m in flagged if not exact.get(m) and not broader.get(m)}
    return exact, broader, not_scorable


def score(rows: list[dict[str, str]], exact, broader, not_scorable) -> dict:
    result = Counter()
    by_model: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        truth = match_key(row["ground_truth"])
        predicted = (row["predicted"] or "").strip()
        if not truth:
            result["no_ground_truth"] += 1
            continue
        if predicted in not_scorable:
            result["not_scorable"] += 1
            by_model[predicted]["not_scorable"] += 1
            continue
        result["scored"] += 1
        by_model[predicted]["scored"] += 1
        if truth in exact.get(predicted, ()):
            result["exact"] += 1
            by_model[predicted]["exact"] += 1
        elif truth in broader.get(predicted, ()):
            result["broader"] += 1
            by_model[predicted]["broader"] += 1
    return {"totals": result, "by_model": by_model}


def _percent(hits: int, total: int) -> str:
    return f"{100 * hits / total:.1f}%" if total else "—"


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("pages", type=Path, help="CSV of real pages")
    p.add_argument("--acceptable", type=Path, default=ACCEPTABLE)
    p.add_argument("--truth-col", default="ground_truth")
    p.add_argument("--pred-col", default="predicted_model_type")
    p.add_argument("--text-col", default="text")
    p.add_argument("--model", type=Path, default=None,
                   help="predict with this trained model folder instead of reading --pred-col")
    args = p.parse_args(argv)

    with args.pages.open(newline="", encoding="utf-8-sig") as handle:
        raw = list(csv.DictReader(handle))
    if not raw:
        raise SystemExit(f"{args.pages} has no rows")
    if args.truth_col not in raw[0]:
        raise SystemExit(f"{args.pages}: no {args.truth_col!r} column")
    if args.model is None and args.pred_col not in raw[0]:
        raise SystemExit(f"{args.pages}: no {args.pred_col!r} column; pass --model to predict from {args.text_col!r}")

    classifier = None
    if args.model is not None:
        from predict import Classifier

        classifier = Classifier(args.model)
    rows = []
    for row in raw:
        if classifier is not None:
            top = classifier.predict(row.get(args.text_col) or "", top=1)
            predicted = top[0]["model_type"] if top else ""
        else:
            predicted = row.get(args.pred_col) or ""
        rows.append({"ground_truth": row.get(args.truth_col) or "", "predicted": predicted})

    exact, broader, not_scorable = load_acceptable(args.acceptable)
    out = score(rows, exact, broader, not_scorable)
    t = out["totals"]
    scored = t["scored"]
    print(f"Pages: {len(rows)}; scored {scored}, not scorable {t['not_scorable']} "
          f"(model type has no ground-truth value in the pivot), no ground truth {t['no_ground_truth']}")
    print(f"Accuracy, Exact only:            {_percent(t['exact'], scored)}  ({t['exact']}/{scored})")
    print(f"Accuracy, Exact + Broader label: {_percent(t['exact'] + t['broader'], scored)}  "
          f"({t['exact'] + t['broader']}/{scored})")
    print(f"  {'predicted model type':<34} {'scored':>6} {'exact':>6} {'broader':>8} {'not scorable':>13}")
    for model_type, c in sorted(out["by_model"].items(), key=lambda kv: -kv[1]["scored"]):
        print(f"  {model_type:<34} {c['scored']:>6} {c['exact']:>6} {c['broader']:>8} {c['not_scorable']:>13}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
