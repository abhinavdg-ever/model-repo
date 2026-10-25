#!/usr/bin/env python3
"""Score the trained page-axes model on the synthetic test pages.

    cd training/bert-training
    python check_test_pages.py --model output/page-axes --pages test_pages.jsonl

Needs predict_axes.py / train_axes.py beside it. Prints PASS/FAIL per page and head and
a summary. Progress Note L2 is not scored (section tagging owns it). Page text is synthetic,
so printing ids and labels is safe.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from predict_axes import AxesClassifier

# Codeability is not learned (2026-10-25): the pipeline reads it from the Final type.
HEADS = ("L1", "L2", "L3", "specialty")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", type=Path, default=Path("output/page-axes"))
    ap.add_argument("--pages", type=Path, default=Path("test_pages.jsonl"))
    args = ap.parse_args()
    clf = AxesClassifier(args.model)
    pages = [json.loads(l) for l in args.pages.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = clf.predict_many([p["text"] for p in pages])
    passed = total = 0
    lab_as_pn = 0
    per = {h: [0, 0] for h in HEADS}
    for p, r in zip(pages, rows):
        e = p["expected"]
        cells = []
        for h in HEADS:
            want, got = e.get(h) or "", r.get(h) or ""
            if h == "L2" and e["L1"] == clf.space["section_L1"]:
                continue
            if h == "L3" and not want:
                continue
            if h == "specialty" and want == "Unknown" and not p.get("spec_trap"):
                continue
            ok = want == got
            total += 1; passed += ok
            per[h][0] += ok; per[h][1] += 1
            cells.append(f"{h}:{'PASS' if ok else 'FAIL'}" + ("" if ok else f" (want {want!r}, got {got!r})"))
        if e["L1"] == "Lab Report" and r.get("L1") == "Progress Note":
            lab_as_pn += 1
        print(f"{p['image']}  route={r['route']}  " + "  ".join(cells))
    print("\nPer head: " + "  ".join(f"{h} {a}/{b}" for h, (a, b) in per.items() if b))
    print(f"{passed}/{total} head checks passed.  Lab Report pages predicted as Progress Note: {lab_as_pn}")
    return 0 if lab_as_pn == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
