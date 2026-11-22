"""Score the local repair on the shuffle test cases (Brief 2 Q2, 2026-11-22), as the pipeline runs it:

  1. continuity pass 1 (the continuity clue model) on the scan order -> encounters, P(break) and clues;
  2. hard / soft boundaries and repair groups (``repair.hard_boundary``, ``repair_groups``);
  3. mismatches and the +-5 window fix (``repair.repair``) with the sequencing pair model;
  4. the final continuity pass on the repaired order (encounters merged back are counted).

Settings ``min_gain`` and the mismatch ``cut`` are tuned on the val cases (wrong moves under 1% first,
then the most fixed); test is scored once with ``--score-test``.

    python -m modules.sequencing.ml.evaluate_synth --cases <local_synth_cases.jsonl> \\
        --text <training_data.jsonl> --page-types <page_types.csv> --out <training/sequencing/data/synth_eval.json>

Writes ids, case types and counts only.
"""
from __future__ import annotations

import argparse
import itertools
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

from modules.continuity.ml import clues as cclues
from modules.continuity.ml import model as cmodel
from modules.continuity.ml.build_data import page_types
from modules.sequencing.ml import model as smodel
from modules.sequencing.ml import repair as rp
from modules.sequencing.ml.synth import split_by_break

GAINS = [2.0, 4.0, 6.0, 8.0, 12.0, 16.0]
CUTS = [0.01, 0.02, 0.05, 0.1]


def prepare(case: dict, texts: dict[str, str], types: dict[str, dict]) -> dict[str, Any]:
    """Continuity pass 1 and the repair groups for one case (scan order)."""
    scan = case["scan_order"]
    recs = [{"text": texts.get(i, ""), **(types.get(i) or {"L1": "", "L2": ""}), "visual": None} for i in scan]
    decisions = cmodel.decide(recs) or []
    rows = cclues.chart_rows(recs)
    doc, docs = [0], 0
    for d in decisions:
        docs += int(d["break"])
        doc.append(docs)
    hard = {i: rp.hard_boundary(rows[i], decisions[i]["p_break"]) for i in range(len(decisions)) if decisions[i]["break"]}
    dos = [""] * len(scan)  # one DOS block per case
    groups = rp.repair_groups(doc, hard, dos)
    return {"scan": scan, "texts": [texts.get(i, "") for i in scan], "groups": groups, "docs": docs + 1,
            "hard": sum(hard.values()), "soft": sum(1 for d in decisions if d["break"]) - sum(hard.values()),
            "group_sizes": [len({doc[k] for k in range(s, e + 1)}) for s, e in groups]}


def run_case(prep: dict, case: dict, score, min_gain: float, cut: float) -> dict[str, Any]:
    info = [smodel.page_info(t) for t in prep["texts"]]
    res = rp.repair(len(prep["scan"]), score, info, prep["groups"], min_gain=min_gain, cut=cut)
    pred = [prep["scan"][k] for k in res.order]
    true = case["true_order"]
    true_pos = {im: k for k, im in enumerate(true)}
    moved = [k for k, p in enumerate(res.order) if p != k]
    wrong = [k for k in moved if true_pos[pred[k]] != k]
    max_move = max((abs(p - k) for k, p in enumerate(res.order)), default=0)
    return {"exact": int(pred == true), "pages": len(pred), "moved": len(moved), "wrong_moves": len(wrong),
            "max_move": max_move, "held": len(res.held), "pred": pred}


def score_side(preps, cases, scorers, min_gain, cut) -> dict[str, Any]:
    by_type: dict[str, list] = defaultdict(list)
    pages = wrong = 0
    max_move = 0
    for prep, case, score in zip(preps, cases, scorers):
        r = run_case(prep, case, score, min_gain, cut)
        by_type[case["shuffle_type"]].append(r["exact"])
        pages += r["pages"]
        wrong += r["wrong_moves"]
        max_move = max(max_move, r["max_move"])
    shuffled = [e for t, v in by_type.items() if t != "none" for e in v]
    return {"fixed_exactly": round(sum(shuffled) / max(1, len(shuffled)), 3),
            "in_order_untouched": round(sum(by_type.get("none", [])) / max(1, len(by_type.get("none", []))), 3),
            "wrong_moves_pct": round(100 * wrong / max(1, pages), 2), "max_move": max_move,
            "by_type": {t: round(sum(v) / len(v), 3) for t, v in sorted(by_type.items())}}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cases", type=Path, required=True)
    ap.add_argument("--text", type=Path, required=True)
    ap.add_argument("--page-types", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--score-test", action="store_true")
    ap.add_argument("--model", type=Path, help="the pair model folder (default SEQUENCING_MODEL_DIR)")
    ap.add_argument("--test-only", nargs=2, type=float, metavar=("MIN_GAIN", "CUT"),
                    help="score only the test cases with these settings (chosen earlier on val)")
    a = ap.parse_args(argv)
    cases = [json.loads(l) for l in a.cases.read_text(encoding="utf-8").splitlines() if l.strip()]
    wanted = {i for c in cases for i in c["scan_order"]}
    texts = {}
    with a.text.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                r = json.loads(line)
                if r.get("image") in wanted:
                    texts[r["image"]] = r.get("text") or ""
    cases += split_by_break(cases, texts)
    types = page_types(a.page_types)
    sides = {s: [c for c in cases if c["split"] == s] for s in (("test",) if a.test_only else ("val", "test"))}
    preps = {s: [prepare(c, texts, types) for c in cs] for s, cs in sides.items()}
    loaded = smodel.load(a.model) if a.model else None
    scorers = {s: [smodel.scorer(p["texts"], loaded) for p in ps] for s, ps in preps.items()}

    if a.test_only:
        g, c = a.test_only
        result = {"settings": {"min_gain": g, "cut": c},
                  "test": score_side(preps["test"], sides["test"], scorers["test"], g, c)}
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(json.dumps(result, indent=1))
        return 0
    grid = []
    for g, c in itertools.product(GAINS, CUTS):
        grid.append({"min_gain": g, "cut": c, **score_side(preps["val"], sides["val"], scorers["val"], g, c)})
    ok = [r for r in grid if r["wrong_moves_pct"] < 1.0] or grid
    best = max(ok, key=lambda r: (r["fixed_exactly"], -r["wrong_moves_pct"], r["min_gain"]))
    out = {"cases": dict(Counter((c["split"], c["shuffle_type"]) for c in cases).most_common()),
           "chosen": {"min_gain": best["min_gain"], "cut": best["cut"]}, "val": best,
           "val_grid": grid,
           "continuity_pass_1_val": {
               "hard_boundaries": sum(p["hard"] for p in preps["val"]),
               "soft_boundaries": sum(p["soft"] for p in preps["val"]),
               "groups_by_encounters": dict(Counter(min(n, 3) for p in preps["val"] for n in p["group_sizes"]))}}
    if a.score_test:
        out["test"] = score_side(preps["test"], sides["test"], scorers["test"], best["min_gain"], best["cut"])
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps({**out, "cases": {f"{k[0]}:{k[1]}": v for k, v in out["cases"].items()}}, indent=1),
                     encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k not in ("val_grid", "cases")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
