"""Score the page classifier (leaf model + keyword canon, ``leaf.predict``) on the page-leaf
model's val / test pages, and compare two runs page by page (Brief 4, 2026-11-22).

    python -m modules.page_classify.leaf_eval run --split <leaf_split.csv> --data <training_data.jsonl> \\
        --side val --out <outside the repo>/before_val.csv
    python -m modules.page_classify.leaf_eval compare <before.csv> <after.csv>

``leaf_split.csv`` comes from ``training/bert-training-leaf/write_split.py`` (the model's own
split, by chart, fixed seed). Writes and prints ids, leaves, counts and scores only -- never
page text. The pages carry no OCR-detected headings, so header-only matches score a little
lower than on real charts.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

SEP = " > "
NOTE, LAB = "Progress Note", "Lab Report"
COLUMNS = ["image", "chart_id", "truth", "leaf", "how", "deciding", "priority_rule", "confidence"]


def _l1(path: str) -> str:
    return (path or "").split(SEP)[0]


def run(split: Path, data: Path, side: str, out: Path, limit: Optional[int] = None,
        guard: Optional[float] = None) -> dict[str, Any]:
    from modules.page_classify import leaf, leaf_canon

    if guard is not None:  # try another Progress Note guard threshold (tuning on val only)
        leaf_canon.canon().priority["progress_note_model_guard"]["min_model_prob"] = guard

    wanted: dict[str, dict[str, str]] = {}
    with split.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row["side"] == side:
                wanted[row["image"]] = row
    rows: list[dict[str, Any]] = []
    started = time.time()
    with data.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            page = json.loads(line)
            ref = wanted.get(str(page.get("image") or ""))
            if ref is None:
                continue
            result = leaf.predict(str(page.get("text") or ""))
            info = (result.leaf or {}) if result else {}
            rows.append({"image": ref["image"], "chart_id": ref["chart_id"], "truth": ref["leaf"],
                         "leaf": info.get("leaf") or "", "how": info.get("how") or "",
                         "deciding": info.get("deciding") or "",
                         "priority_rule": (info.get("priority_rule") or {}).get("rule") or "",
                         "confidence": info.get("confidence")})
            if limit and len(rows) >= limit:
                break
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    summary = score(rows)
    summary["seconds"] = round(time.time() - started, 1)
    return summary


def predict(images_csv: Path, data: Path, out: Path) -> dict[str, Any]:
    """The pipeline's page type for every image in ``images_csv`` (a column ``image``): leaf,
    L1, L2, specialty and the L1s of the model's top 5 leaves (continuity's page-type clues,
    Brief 3 section 2.2). Ids and labels only."""
    from modules.page_classify import leaf

    with images_csv.open(encoding="utf-8") as handle:
        wanted = {row["image"] for row in csv.DictReader(handle)}
    started, rows = time.time(), []
    with data.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            page = json.loads(line)
            if page.get("image") not in wanted:
                continue
            r = leaf.predict(str(page.get("text") or ""))
            info = (r.leaf or {}) if r else {}
            top = [_l1(t["path"]) for t in info.get("model_top") or []]
            rows.append({"image": page["image"], "leaf": info.get("leaf") or "", "l1": r.l1 if r else "",
                         "l2": (r.l2 or "") if r else "", "specialty": (r.specialty or "") if r else "",
                         "top5_l1": "|".join(dict.fromkeys(top))})
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["image", "leaf", "l1", "l2", "specialty", "top5_l1"])
        writer.writeheader()
        writer.writerows(rows)
    return {"pages": len(rows), "of": len(wanted), "seconds": round(time.time() - started, 1)}


def score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows) or 1
    notes = [r for r in rows if _l1(r["truth"]) == NOTE]
    return {
        "pages": len(rows),
        "l1_accuracy": round(sum(_l1(r["leaf"]) == _l1(r["truth"]) for r in rows) / n, 4),
        "leaf_accuracy": round(sum(r["leaf"] == r["truth"] for r in rows) / n, 4),
        "note_pages": len(notes),
        "note_called_lab_report": sum(_l1(r["leaf"]) == LAB for r in notes),
        "lab_called_note": sum(_l1(r["leaf"]) == NOTE for r in rows if _l1(r["truth"]) == LAB),
        "notes_kept": sum(_l1(r["leaf"]) == NOTE for r in notes),
        "others_called_note": sum(_l1(r["leaf"]) == NOTE for r in rows if _l1(r["truth"]) != NOTE),
        "how": dict(Counter(r["how"] for r in rows)),
        "priority_fired": dict(Counter(r.get("priority_rule") or "none" for r in rows)),
    }


def per_l1(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for l1 in sorted({_l1(r["truth"]) for r in rows}):
        mine = [r for r in rows if _l1(r["truth"]) == l1]
        out[l1] = {"pages": len(mine), "l1_ok": sum(_l1(r["leaf"]) == l1 for r in mine),
                   "leaf_ok": sum(r["leaf"] == r["truth"] for r in mine)}
    return out


def _read(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def compare(before: Path, after: Path) -> dict[str, Any]:
    a = {r["image"]: r for r in _read(before)}
    b = {r["image"]: r for r in _read(after)}
    changed = []
    for image in sorted(set(a) & set(b)):
        if a[image]["leaf"] != b[image]["leaf"]:
            truth = a[image]["truth"]
            changed.append({"image": image, "truth": truth, "before": a[image]["leaf"], "after": b[image]["leaf"],
                            "verdict": "fixed" if b[image]["leaf"] == truth else
                            "broken" if a[image]["leaf"] == truth else "still wrong",
                            "how_before": a[image]["how"], "how_after": b[image]["how"],
                            "deciding_after": b[image]["deciding"], "priority_rule": b[image].get("priority_rule", ""),
                            "rule_right": b[image]["leaf"].split(SEP)[0] == truth.split(SEP)[0]})
    return {"before": score(list(a.values())), "after": score(list(b.values())),
            "changed": len(changed), "verdicts": dict(Counter(c["verdict"] for c in changed)), "pages": changed}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--split", type=Path, required=True)
    r.add_argument("--data", type=Path, required=True)
    r.add_argument("--side", choices=("val", "test"), default="val")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--limit", type=int)
    r.add_argument("--guard", type=float, help="Progress Note guard threshold to try (val only)")
    pr = sub.add_parser("predict", help="page types for a list of images (continuity clues)")
    pr.add_argument("--images", type=Path, required=True, help="CSV with an image column")
    pr.add_argument("--data", type=Path, required=True)
    pr.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("compare")
    c.add_argument("before", type=Path)
    c.add_argument("after", type=Path)
    c.add_argument("--pages", action="store_true", help="list every changed page (ids and leaves only)")
    args = ap.parse_args(argv)
    if args.cmd == "run":
        print(json.dumps(run(args.split, args.data, args.side, args.out, args.limit, args.guard), indent=1))
        print(json.dumps(per_l1(_read(args.out)), indent=1))
        return 0
    if args.cmd == "predict":
        print(json.dumps(predict(args.images, args.data, args.out)))
        return 0
    result = compare(args.before, args.after)
    pages = result.pop("pages")
    print(json.dumps(result, indent=1))
    if args.pages:
        for page in pages:
            print(json.dumps(page))
    return 0


if __name__ == "__main__":
    sys.exit(main())
