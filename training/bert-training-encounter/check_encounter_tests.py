#!/usr/bin/env python3
"""Score encounter-type predictions on encounter_test_pages.jsonl.

    python check_encounter_tests.py --pages encounter_test_pages.jsonl --pred predictions.csv

predictions.csv: header id,encounter_type (one row per test id). ENC_22 (client default rule)
is printed but not scored. Exit code 1 if any scored page fails.
"""
import argparse, csv, json
from collections import Counter

ap = argparse.ArgumentParser()
ap.add_argument("--pages", default="encounter_test_pages.jsonl")
ap.add_argument("--pred", required=True)
a = ap.parse_args()
pages = [json.loads(l) for l in open(a.pages, encoding="utf-8") if l.strip()]
pred = {r["id"]: (r.get("encounter_type") or "").strip() for r in csv.DictReader(open(a.pred, encoding="utf-8"))}
ok = Counter(); tot = Counter(); fails = 0
for p in pages:
    want, got = p["expected_encounter_type"], pred.get(p["id"], "<missing>")
    scored = p["expected_basis"] != "default"
    if scored:
        tot[want] += 1; ok[want] += want == got; fails += want != got
    flag = "PASS" if want == got else ("FAIL" if scored else "info")
    print(f"{p['id']}  {flag:4}  want={want:<24} got={got:<24} {p['checks'][:70]}")
print("\nPer-class recall:")
for c in sorted(tot): print(f"  {c:<26} {ok[c]}/{tot[c]}")
print(f"\n{sum(ok.values())}/{sum(tot.values())} scored pages correct")
raise SystemExit(1 if fails else 0)
