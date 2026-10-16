#!/usr/bin/env python3
"""Check image_labels.csv against taxonomy_v24.json.

    python check_taxonomy.py <folder with image_labels.csv>
    python check_taxonomy.py <folder> --taxonomy other/taxonomy_v24.json

Prints counts, reasons and file names only, never page text. Exits 1 when a
labelled row is not valid under the taxonomy.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from store import Store
from taxonomy import TAXONOMY_PATH, Taxonomy


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", type=Path, help="folder holding image_labels.csv")
    p.add_argument("--taxonomy", type=Path, default=TAXONOMY_PATH)
    p.add_argument("--list", type=int, default=20, help="file names to print per reason")
    args = p.parse_args(argv)

    tax = Taxonomy(args.taxonomy)
    store = Store(args.folder, tax)  # opening changes nothing on disk
    c = store.counts()
    print(f"taxonomy {tax.version}: {len(tax.l1)} L1, {tax.counts.get('L2')} L2, {tax.counts.get('L3')} L3, "
          f"{len(tax.specialty)} specialties")
    print(f"{store.labels_path}: {c['rows']} rows, {c['rows'] - c['untagged']} labelled")
    print(f"  not valid: {c['invalid']} · disagreements open: {c['disagreements']} · "
          f"needs_review: {c['needs_review']} · specialty {tax.specialty_other!r}: {c['specialty_other']}")
    by_reason: dict[str, list[str]] = {}
    for index, reason in store.invalid.items():
        by_reason.setdefault(reason, []).append(store.rows[index].get("image") or "")
    for reason, images in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(images):>4}  {reason}")
        for image in images[: args.list]:
            print(f"          {image}")
    specialties = Counter(r.get("specialty") or "" for r in store.rows if store.tagged(r))
    print("specialty: " + ", ".join(f"{k or '<blank>'} {v}" for k, v in specialties.most_common()))
    print("OK" if not store.invalid else "FAILED")
    return 1 if store.invalid else 0


if __name__ == "__main__":
    sys.exit(main())
