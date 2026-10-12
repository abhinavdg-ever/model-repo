#!/usr/bin/env python3
"""Check taxonomy.json, a keyword canon and image_labels.csv agree.

    python check_taxonomy.py <folder with image_labels.csv>
    python check_taxonomy.py <folder> --canon ../../core-pipeline/stages/lib/keyword-canon/page_keyword_canon.json
    python check_taxonomy.py <folder> --expect 51 191 60

Prints counts and file names only, never page text. Exits 1 when a check fails.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from taxonomy import TAXONOMY_PATH, Taxonomy

HERE = Path(__file__).resolve().parent
DEFAULT_CANON = HERE.parents[1] / "core-pipeline" / "stages" / "lib" / "keyword-canon" / "page_keyword_canon.json"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", type=Path, help="folder holding image_labels.csv")
    p.add_argument("--taxonomy", type=Path, default=TAXONOMY_PATH)
    p.add_argument("--canon", type=Path, default=DEFAULT_CANON, help="keyword canon JSON")
    p.add_argument("--expect", type=int, nargs=3, metavar=("PAGE_TYPES", "SUB_TYPES", "MODEL_TYPES"),
                   default=None, help="fail unless the taxonomy has exactly these counts")
    args = p.parse_args(argv)

    failed = False
    tax = Taxonomy(args.taxonomy)
    c = tax.counts
    print(f"taxonomy.json {tax.version}: {c['page_types']} page types, {c['sub_types']} sub-types, "
          f"{c['model_types']} model types")
    if args.expect and [c["page_types"], c["sub_types"], c["model_types"]] != args.expect:
        print(f"  FAIL: expected {args.expect[0]} / {args.expect[1]} / {args.expect[2]}")
        failed = True

    if args.canon.is_file():
        canon = json.loads(args.canon.read_text(encoding="utf-8"))
        names = [s["page_subtype"] for s in canon.get("sub_types", [])]
        all_subs = {s for subs in tax.sub_types.values() for s in subs}
        unknown = sorted(n for n in names if n not in all_subs)
        wrong_type = sorted(
            s["page_subtype"] for s in canon.get("sub_types", [])
            if s["page_subtype"] in all_subs and not tax.subtype_belongs(s.get("page_type", ""), s["page_subtype"])
        )
        print(f"keyword canon {args.canon.name}: {len(names)} sub-types; "
              f"{len(unknown)} not in taxonomy, {len(wrong_type)} under the wrong page type")
        for name in unknown + wrong_type:
            print(f"  {name}")
        failed = failed or bool(unknown or wrong_type)
    else:
        print(f"keyword canon not found at {args.canon} (skipped)")

    labels = args.folder / "image_labels.csv"
    with labels.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    tagged = [r for r in rows if (r.get("model_type") or "").strip()]
    bad_pair = [r for r in tagged if not tax.is_valid_pair(r.get("page_type", ""), r.get("page_subtype", ""))]
    bad_rule = [r for r in tagged
                if r.get("model_type") != tax.rule_model_type(r.get("page_type", ""), r.get("page_subtype", ""))]
    print(f"{labels}: {len(rows)} rows, {len(tagged)} tagged; {len(bad_pair)} invalid pair(s), "
          f"{len(bad_rule)} breaking the model type rule")
    for r in bad_pair + bad_rule:
        print(f"  {r.get('image')}: {r.get('page_type')} / {r.get('page_subtype')} -> {r.get('model_type')}")
    failed = failed or bool(bad_pair or bad_rule)

    print("OK" if not failed else "FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
