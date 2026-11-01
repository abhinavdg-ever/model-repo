#!/usr/bin/env python3
"""Score a trained page-leaf model on the synthetic test pages.

    cd training/bert-training-leaf
    python check_test_pages.py --model output/page-leaf --pages test_pages.jsonl

Needs train_leaf.py beside it. Per page: the top leaf against the expected one (PASS /
FAIL), whether the expected leaf is among the kept leaves (the top 5 the pipeline's keyword
canon chooses from), and the specialty when the page expects one. Exits 1 if a Lab Report
page comes back as Progress Note. Page text is synthetic, so printing ids and labels is safe.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from train_leaf import RENAMES, decode, encode_texts, load_model


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path, default=Path("output/page-leaf"))
    ap.add_argument("--pages", type=Path, default=Path("test_pages.jsonl"))
    args = ap.parse_args()
    import torch

    model, tokenizer, space = load_model(args.model)
    folded = space.get("folded") or {}
    pages = [json.loads(l) for l in args.pages.read_text(encoding="utf-8").splitlines() if l.strip()]
    top1 = kept = spec_ok = spec_n = lab_as_note = 0
    for page in pages:
        ids = encode_texts(tokenizer, [page["text"]], int(space["max_length"]), float(space["head_fraction"]))[0]
        with torch.no_grad():
            logits = model(torch.tensor([ids]), torch.ones((1, len(ids)), dtype=torch.long))
        row = decode(logits, space)[0]
        want = RENAMES.get(page["expected"]["leaf"], page["expected"]["leaf"])
        want = folded.get(want, want)  # a folded leaf trained as its parent's Others
        leaves = [leaf["leaf"] for leaf in row["leaves"]]
        got = leaves[0] if leaves else ""
        ok, inside = got == want, want in leaves
        top1 += ok
        kept += inside
        cells = [f"leaf:{'PASS' if ok else 'FAIL'}" + ("" if ok else f" (want {want!r}, got {got!r})"),
                 f"top5:{'yes' if inside else 'no'}"]
        spec = page["expected"].get("specialty") or ""
        if spec and spec != "Unknown" and row.get("specialty"):
            spec_n += 1
            spec_ok += row["specialty"] == spec
            cells.append(f"specialty:{'PASS' if row['specialty'] == spec else 'FAIL'}"
                         + ("" if row["specialty"] == spec else f" (want {spec!r}, got {row['specialty']!r})"))
        if want.startswith("Lab Report") and got.startswith("Progress Note"):
            lab_as_note += 1
        print(f"{page['image']}  " + "  ".join(cells))
    n = len(pages)
    print(f"\nTop leaf {top1}/{n}  in kept leaves {kept}/{n}  specialty {spec_ok}/{spec_n}  "
          f"Lab Report pages predicted as Progress Note: {lab_as_note}")
    return 0 if lab_as_note == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
