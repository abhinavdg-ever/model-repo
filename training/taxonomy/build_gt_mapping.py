#!/usr/bin/env python3
"""Build the ground-truth page-type canon: the client's GT labels mapped to our
v29.2 paths, from the mapping workbook (sheet "GT label to ours").

    python training/taxonomy/build_gt_mapping.py GT_mapping_v29_2.xlsx
    python training/taxonomy/build_gt_mapping.py --check     # exit 1 when the CSV is stale

Writes

* ``review-ui/frontend/src/gtPageTypeCanon.json`` — the canon review-ui scores with
  (``pageTypeMark`` in ``groundTruth.ts``)
* ``docs/gt_page_type_mapping.csv`` — the same mapping, one row per GT label and
  target, with the target split into L1 / L2 / L3

Every target must be an L1 or a prefix of a path in ``taxonomy.json``. The canon is
the source once written: edit it, then run the script with no workbook to rewrite the CSV.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TAXONOMY = REPO / "training" / "taxonomy" / "taxonomy.json"
CANON = REPO / "review-ui" / "frontend" / "src" / "gtPageTypeCanon.json"
CSV_OUT = REPO / "docs" / "gt_page_type_mapping.csv"
SEP = " > "

STATUS = {"Mapped": "mapped", "Mapped (ambiguous GT name)": "ambiguous", "Exclude from accuracy": "exclude"}

COMMENT = (
    "Client ground-truth page-type labels -> our v29.2 paths (L1 > L2 > L3). Key = the GT label "
    "lower-cased with everything but letters and digits removed. Our path is L1 > L2 > L3 (Others "
    "dropped). FULL (1): a 'full' target is a prefix of our path. PARTIAL (0.5): a 'partial' target "
    "is a prefix of our path, or our L1 is the L1 of a 'full' target (our path stops above the GT "
    "level, a sibling L3, or another L2 under the same L1). MISS (0) otherwise. 'exclude' labels "
    "(cover / index / fax / blank pages, NA, Misc ...) are not page types and are not scored. A page "
    "scores the better of Extracted and Final. Built by training/taxonomy/build_gt_mapping.py from "
    "GT_mapping_v29_2.xlsx; docs/gt_page_type_mapping.csv is the same mapping as a table."
)


def key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", label.lower())


def targets(cell: object) -> list[str]:
    lines = [SEP.join(part.strip() for part in line.split(">")) for line in str(cell or "").splitlines()]
    return [line for line in lines if line.strip() and line.strip().lower() != "others"]


def from_workbook(path: Path) -> dict:
    import openpyxl

    sheet = openpyxl.load_workbook(path, read_only=True, data_only=True)["GT label to ours"]
    rows = list(sheet.iter_rows(values_only=True))
    labels: dict[str, dict] = {}
    for gt, _, standard, status, full, partial, note in (row[:7] for row in rows[1:]):
        if not gt or not str(gt).strip():
            continue
        entry = {"label": " ".join(str(gt).split()), "standard": str(standard or "").strip(),
                 "status": STATUS[str(status).strip()], "full": targets(full), "partial": targets(partial)}
        if note:
            entry["note"] = str(note).strip()
        if entry["status"] == "exclude":
            entry["full"], entry["partial"] = [], []
        previous = labels.get(key(entry["label"]))
        if previous and (previous["full"], previous["partial"]) != (entry["full"], entry["partial"]):
            raise SystemExit(f"{entry['label']!r} and {previous['label']!r} fold to one key but map differently")
        labels.setdefault(key(entry["label"]), entry)
    return {"comment": COMMENT, "version": "v29.2", "labels": dict(sorted(labels.items()))}


def check_targets(canon: dict) -> list[str]:
    taxonomy = json.loads(TAXONOMY.read_text(encoding="utf-8"))
    known = {item["name"] for item in taxonomy["L1"]}
    for path in taxonomy["paths"]:
        levels = path.split(SEP)
        known.update(SEP.join(levels[: n + 1]) for n in range(len(levels)))
    return [f"{entry['label']}: {target}" for entry in canon["labels"].values()
            for target in entry["full"] + entry["partial"] if target not in known]


def to_csv(canon: dict) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["gt_label", "standard_name", "status", "match", "l1", "l2", "l3", "note"])
    for entry in sorted(canon["labels"].values(), key=lambda e: (e["standard"].lower(), e["label"].lower())):
        rows = [("full", t) for t in entry["full"]] + [("partial", t) for t in entry["partial"]]
        for match, target in rows or [("not scored", "")]:
            levels = (target.split(SEP) + ["", "", ""])[:3] if target else ["", "", ""]
            writer.writerow([entry["label"], entry["standard"], entry["status"], match, *levels, entry.get("note", "")])
    return out.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workbook", nargs="?", type=Path, help="GT_mapping_v29_2.xlsx (omit to reuse the canon)")
    parser.add_argument("--check", action="store_true", help="exit 1 when the CSV does not match the canon")
    args = parser.parse_args()

    canon = from_workbook(args.workbook) if args.workbook else json.loads(CANON.read_text(encoding="utf-8"))
    unknown = check_targets(canon)
    if unknown:
        print("targets not in taxonomy.json:\n  " + "\n  ".join(unknown), file=sys.stderr)
        return 1
    table = to_csv(canon)
    if args.check:
        stale = not CSV_OUT.is_file() or CSV_OUT.read_text(encoding="utf-8") != table
        print("docs/gt_page_type_mapping.csv is stale" if stale else "up to date")
        return int(stale)
    if args.workbook:
        CANON.write_text(json.dumps(canon, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    CSV_OUT.write_text(table, encoding="utf-8")
    counts = {s: sum(e["status"] == s for e in canon["labels"].values()) for s in STATUS.values()}
    print(f"{len(canon['labels'])} GT labels {counts}; wrote {CSV_OUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
