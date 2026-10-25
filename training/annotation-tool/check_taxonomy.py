#!/usr/bin/env python3
"""Check image_labels.csv against taxonomy.json (v29.2).

    python check_taxonomy.py <folder with image_labels.csv>
    python check_taxonomy.py <folder> --taxonomy other/taxonomy.json --label-sets other/label_sets.json

Prints the label space (L1 count, L2 count per L1, L3 count per L2 that has
L3s), whether label_sets.json agrees with it, the old names the name map would
still rename, and the rows that are not valid. Counts, reasons, label names
and file names only, never page text. Exits 1 when a labelled row is not
valid, label_sets.json disagrees, or an old name is still stored.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from store import NAME_MAP_PATH, Store
from taxonomy import TAXONOMY_PATH, Taxonomy, join_path


def print_sources(store: Store) -> None:
    """Which files the tool opened, and whether label_sets.json agrees with the taxonomy."""
    tax = store.taxonomy
    print(f"taxonomy {tax.version}: {len(tax.l1)} L1, {tax.counts.get('L2')} L2, {tax.counts.get('L3')} L3, "
          f"{len(tax.specialty)} specialties")
    if store.label_sets is not None:
        problems = tax.label_sets_problems(store.label_sets)
        print(f"{store.label_sets_path}: " + ("agrees with the taxonomy" if not problems
                                               else f"{len(problems)} difference(s)"))
        for problem in problems:
            print(f"  {problem}")
    print(f"images: {store.root}" + (f" and {store.images_dir} (by stem)" if store.images_dir else ""))
    enc = store.encounters
    if enc.enabled:
        print(f"encounter: {enc.tags_path or '-'} · {enc.review_path or '-'} · classes: {', '.join(enc.classes)}")
        print("  " + ", ".join(f"{k or '<none>'} {v}" for k, v in sorted(enc.counts().items(), key=lambda kv: -kv[1])))
    else:
        print("encounter: no encounter_tags.jsonl / encounter_review.csv found")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("folder", type=Path, help="folder holding image_labels.csv")
    p.add_argument("--taxonomy", type=Path, default=TAXONOMY_PATH)
    p.add_argument("--label-sets", type=Path, help="default: label_sets.json in the folder, if any")
    p.add_argument("--images", type=Path, help="default: ../ocr-jpeg beside the folder, when it exists")
    p.add_argument("--name-map", type=Path, default=NAME_MAP_PATH)
    p.add_argument("--list", type=int, default=20, help="file names to print per reason")
    args = p.parse_args(argv)

    tax = Taxonomy(args.taxonomy)
    # Opening changes nothing on disk.
    store = Store(args.folder, tax, images=args.images, label_sets=args.label_sets, name_map=args.name_map)
    print_sources(store)
    print("L2 per L1: " + ", ".join(f"{a} {len(tax.l2_of(a))}" for a in tax.l1))
    for a in tax.l1:
        for b in tax.l2_of(a):
            if tax.l3_of(a, b):
                print(f"L3 under {join_path(a, b)}: {len(tax.l3_of(a, b))}")

    c = store.counts()
    print(f"{store.labels_path}: {c['rows']} rows, {c['rows'] - c['untagged']} labelled, "
          f"{c['claude_reviewed']} claude-reviewed, {c['missing_files']} without an image")
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

    migration = store.migrate(apply=False)
    print(f"old names (name map): {len(migration.changes)} to rename, "
          f"{len(migration.decisions)} needing a decision")
    if migration.changes or migration.decisions:
        print("  run: python app.py <folder> --migrate --dry-run")

    sets_ok = store.label_sets is None or not tax.label_sets_problems(store.label_sets)
    ok = not store.invalid and sets_ok and not migration.changes and not migration.decisions
    print("OK" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
