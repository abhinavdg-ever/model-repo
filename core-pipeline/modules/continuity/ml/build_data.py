"""The continuity clue table (Brief 3 E2, 2026-11-22): every reviewed meeting point (page n -> n+1)
with its label and the clues ``clues.py`` computes -- the same code the pipeline runs -- written
for ``training/continuity/clue-model`` to train on. Labels are never edited here: the meeting
points, ``doc_breaks``, ``unsure`` and ``split`` come from the reviewed documents
(``Training/segmentation_training_data/continuity/document_pairs.csv``, split by chart, frozen).

    python -m modules.continuity.ml.build_data \\
        --labels "<Training>/segmentation_training_data/continuity" \\
        --text "<Training>/processed/training_data.jsonl" \\
        [--page-types <page_types.csv>] --out ../training/continuity/data

``--page-types``: the pipeline's predicted page types per image (``leaf_eval predict``: L1, L2,
specialty, top5_l1). Without it the reviewed L1 / L2 stand in (and the top-5 clues are NaN).

Writes ``clue_table.csv`` (ids, labels, clues; no text) and ``build_report.json``: counts, the
break rate of each signature type on train + val, and the meeting points a reviewer should look
at (chart and pages only). Never prints page text.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

from modules.continuity.ml.clues import FEATURES, chart_rows

TEXT_CLUE = "p_break_text"
ID_COLUMNS = ["pair_id", "chart_id", "page_n", "page_n1", "image_n", "image_n1", "split", "doc_breaks", "unsure",
              "group_n", "group_n1", "L1_n", "L1_n1"]
REPORT_ONLY = ["sig_any_tail_a"]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def page_types(path: Optional[Path]) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    out = {}
    for row in _read_csv(path):
        out[row["image"]] = {"L1": row.get("l1") or "", "L2": row.get("l2") or "",
                             "specialty": row.get("specialty") or "",
                             "top5_l1": [x for x in (row.get("top5_l1") or "").split("|") if x]}
    return out


CORRECTIONS = "label_corrections.csv"


def apply_corrections(pairs: list[dict[str, str]], labels: Path) -> int:
    """A reviewer's corrections (``label_corrections.csv`` beside the labels: chart_id, page_n, page_n1,
    doc_breaks, reviewed_on, reason) applied over the built labels -- the label files themselves are
    never edited. Returns how many meeting points changed."""
    path = labels / CORRECTIONS
    if not path.is_file():
        return 0
    fixes = {(r["chart_id"], r["page_n"], r["page_n1"]): r for r in _read_csv(path)}
    changed = 0
    for p in pairs:
        fix = fixes.get((p["chart_id"], p["page_n"], p["page_n1"]))
        if fix and p["doc_breaks"] != fix["doc_breaks"]:
            p["doc_breaks"] = fix["doc_breaks"]
            p["doc_continues"] = str(1 - int(fix["doc_breaks"]))
            changed += 1
    return changed


def text_scores(path: Optional[Path]) -> dict[str, float]:
    """pair_id -> p_break_text from the text model's scores_break.csv (out-of-fold for train / val,
    the final model for test: never a score the text model learned that pair from)."""
    if not path:
        return {}
    return {r["pair_id"]: float(r["p_break_text"]) for r in _read_csv(path)}


def build(labels: Path, text: Path, types_file: Optional[Path],
          scores_file: Optional[Path] = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pairs = _read_csv(labels / "document_pairs.csv")
    corrected = apply_corrections(pairs, labels)
    scores = text_scores(scores_file)
    wanted = {p["image_n"] for p in pairs} | {p["image_n1"] for p in pairs}
    pages: dict[str, dict[str, Any]] = {}
    with text.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                r = json.loads(line)
                if r.get("image") in wanted:
                    pages[r["image"]] = {"text": r.get("text") or "", "specialty": r.get("specialty") or ""}
    missing = wanted - set(pages)
    predicted = page_types(types_file)

    by_chart: dict[str, list[dict[str, str]]] = defaultdict(list)
    for p in pairs:
        by_chart[p["chart_id"]].append(p)
    rows: list[dict[str, Any]] = []
    for chart, chart_pairs in by_chart.items():
        chart_pairs.sort(key=lambda p: int(p["page_n"]))
        # One chain per run of consecutive meeting points (page n1 of one = page n of the next).
        chain: list[dict[str, str]] = []
        for p in chart_pairs + [None]:
            if p is not None and (not chain or chain[-1]["image_n1"] == p["image_n"]):
                chain.append(p)
                continue
            if chain:
                images = [chain[0]["image_n"]] + [c["image_n1"] for c in chain]
                recs = []
                for k, image in enumerate(images):
                    pair = chain[k] if k < len(chain) else chain[-1]
                    side = "n" if k < len(chain) else "n1"
                    own = predicted.get(image) or {"L1": pair[f"L1_{side}"], "L2": pair[f"L2_{side}"]}
                    recs.append({**pages.get(image, {"text": ""}), **own, "visual": None})
                gaps = {k: int(c.get("gap") or 0) for k, c in enumerate(chain)}
                for c, clue in zip(chain, chart_rows(recs, gaps)):
                    rows.append({**{k: c.get(k, "") for k in ID_COLUMNS}, **{k: clue.get(k) for k in FEATURES},
                                 **{k: clue.get(k) for k in REPORT_ONLY},
                                 **({TEXT_CLUE: scores.get(c["pair_id"], math.nan)} if scores else {})})
            chain = [p] if p is not None else []
    return rows, {"pairs_in": len(pairs), "rows_out": len(rows), "pages_without_text": len(missing),
                  "reviewer_corrections_applied": corrected,
                  "text_scores": len(scores),
                  "page_types": "predicted" if predicted else "reviewed"}


def report(rows: list[dict[str, Any]], info: dict[str, Any]) -> dict[str, Any]:
    fit = [r for r in rows if r["split"] in ("train", "val")]
    base = sum(int(r["doc_breaks"]) for r in fit) / max(1, len(fit))

    def rate(name: str, test=lambda v: v == 1):
        hit = [r for r in fit if r.get(name) is not None and not (isinstance(r[name], float) and math.isnan(r[name]))
               and test(r[name])]
        return {"points": len(hit), "break_rate": round(sum(int(r["doc_breaks"]) for r in hit) / len(hit), 3) if hit else None}

    review = [{"chart_id": r["chart_id"], "page_n": r["page_n"], "page_n1": r["page_n1"], "why": why}
              for r in rows for why, bad in (
                  ("page n+1 opens with Additional Documentation / an addendum, labelled a break",
                   r.get("addl_doc_head_b") == 1 and int(r["doc_breaks"]) == 1),
                  ("page n's only signatures are embedded-result ones, labelled a break",
                   (r.get("sig_embedded_result_a") or 0) > 0 and r.get("sig_tail_a") == 0
                   and r.get("sig_before_next_section") == 0 and int(r["doc_breaks"]) == 1))
              if bad]
    return {
        **info,
        "split": dict(Counter(r["split"] for r in rows)),
        "breaks": {f"{s}:{b}": n for (s, b), n in Counter((r["split"], r["doc_breaks"]) for r in rows).items()},
        "base_break_rate_train_val": round(base, 3),
        "signature_types_train_val": {
            "old single flag (sig_any_tail_a)": rate("sig_any_tail_a"),
            "end of note (sig_tail_a)": rate("sig_tail_a"),
            "signature before the next section": rate("sig_before_next_section"),
            "n+1 opens Additional Documentation": rate("addl_doc_head_b"),
            "embedded result signature": rate("sig_embedded_result_a", lambda v: v > 0),
        },
        "visual_train_val": {"visual_b": rate("visual_b"), "visual_a": rate("visual_a")},
        "for_review": review,
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--text", type=Path, required=True)
    ap.add_argument("--page-types", type=Path)
    ap.add_argument("--text-scores", type=Path, help="the text model's scores_break.csv: adds p_break_text")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    rows, info = build(args.labels, args.text, args.page_types, args.text_scores)
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "clue_table.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ID_COLUMNS + list(FEATURES) + REPORT_ONLY
                                + ([TEXT_CLUE] if info["text_scores"] else []))
        writer.writeheader()
        writer.writerows(rows)
    rep = report(rows, info)
    (args.out / "build_report.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in rep.items() if k != "for_review"}, indent=1))
    print(f"for review: {len(rep['for_review'])} meeting points (chart and pages in build_report.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
