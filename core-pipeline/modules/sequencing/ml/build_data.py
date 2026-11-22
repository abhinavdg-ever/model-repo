"""Sequencing clue tables (Brief 2 Q0 / Q2, 2026-11-22): the order clues ``clues.py`` computes --
the same code the pipeline runs -- for every labelled page pair, written for
``training/sequencing/clue-model`` to train on. Labels are never edited here.

    python -m modules.sequencing.ml.build_data --pairs <order_pairs.csv or local_order_pairs.csv> \\
        --text "<Training>/processed/training_data.jsonl" --out <training/sequencing/data/...csv>

Keeps every id and label column of the input (``follows``, ``split``, positions, DOS block) and
replaces the clue columns with the recomputed ones; reports how many stored clues differ.
No text is written.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Optional

from modules.sequencing.ml.clues import opage, order_feats

CLUES = ['pg_both', 'pg_next', 'pg_delta', 'pg_backward', 'pg_b_is1', 'pg_a_islast', 'fax_both', 'fax_next',
         'fax_delta', 'ni_next', 'ni_delta', 'sec_delta', 'sec_a_last', 'sec_b_first', 'a_ends_open',
         'b_starts_lower', 'open_join', 'a_sig_tail', 'b_sig_tail', 'a_cont_tail', 'b_title_head', 'a_title_head',
         'b_enc_head', 'a_enc_head', 'header_jacc', 'tail_head_jacc', 'a_chars', 'b_chars']


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", type=Path, required=True)
    ap.add_argument("--text", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    with a.pairs.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    wanted = {r["image_a"] for r in rows} | {r["image_b"] for r in rows}
    records = {}
    with a.text.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                r = json.loads(line)
                if r.get("image") in wanted:
                    records[r["image"]] = {"text": r.get("text") or ""}
    cache: dict[str, dict] = {}

    def page(image: str) -> dict:
        if image not in cache:
            cache[image] = opage(records.get(image, {"text": ""}))
        return cache[image]

    keep = [c for c in rows[0] if c not in CLUES]
    differ = {c: 0 for c in CLUES}
    out = []
    for r in rows:
        f = order_feats(records.get(r["image_a"], {}), records.get(r["image_b"], {}), page(r["image_a"]), page(r["image_b"]))
        for c in CLUES:
            if c in r and r[c] != "" and round(float(r[c]), 3) != round(float(f[c]), 3):
                differ[c] += 1
        out.append({**{k: r[k] for k in keep}, **{c: f[c] for c in CLUES}})
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=keep + CLUES)
        w.writeheader()
        w.writerows(out)
    print(json.dumps({"pairs": len(out), "pages_without_text": len(wanted - set(records)),
                      "stored_clues_differing": {k: v for k, v in differ.items() if v}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
