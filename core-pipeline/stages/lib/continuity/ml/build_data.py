"""Document-continuity training data (2026-11-02, user): does the DOCUMENT carry on from page n
to page n+1, for every document type?

The earlier encounter data (Training/boundary/build_encounter_data.py) labelled only visit notes
and marked every other page "no encounter". Here every reviewed document counts: a lab report,
consent, clinical summary or medication list can run on over pages just as a note does. Pages of
the L1s in one group of ``keyword-canon/continuity_groups.json`` may share a document (a Clinical
Summary print runs on into its Problem List and Medication pages); each page's group and the clue
``same_group`` come from that file.

Labels come from the reviewed documents (``pages_bmes.csv``: one doc_id per document, B/M/E/S
tags, ``unsure_before``):

* ``doc_continues`` = 1 when pages n and n+1 are in the same reviewed document;
* ``doc_breaks`` = 1 - doc_continues (the model's target: a break after page n);
* ``unsure`` = the reviewer was unsure of the boundary before page n+1 (half weight).

    python -m stages.lib.continuity.ml.build_data \\
        --boundary "/Volumes/Extreme SSD/Advantmed/Training/boundary" \\
        --training-data "/Volumes/Extreme SSD/Advantmed/Training/processed/training_data.jsonl"

Reads only; writes ``--out`` (default training/bert-training-boundary/data, git-ignored):
pages_document.csv, document_pairs.csv (labels + clues), document_pairs.jsonl (labels + the
end of page n and the start of page n+1, for the text model), document_report.txt.
The clues are continuity/ml/features.py's, the same code the pipeline will run.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
from pathlib import Path

from stages.lib.canon_store import CANON_DIR
from stages.lib.continuity.ml.features import lines, page_feats, pair_feats

HERE = Path(__file__).resolve()
DEFAULT_OUT = HERE.parents[5] / "training" / "bert-training-boundary" / "data"
GROUPS_FILE = CANON_DIR / "continuity_groups.json"


def load_groups(path: Path = GROUPS_FILE) -> dict[str, str]:
    """L1 -> its group; an L1 not listed is its own group."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {l1: group for group, l1s in data["groups"].items() for l1 in l1s}


def group_of(l1: str, groups: dict[str, str]) -> str:
    return groups.get(l1) or (l1 or "unknown")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--boundary", type=Path, required=True, help="folder with pages_bmes.csv")
    ap.add_argument("--training-data", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)

    groups = load_groups()
    records = {}
    with a.training_data.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                r = json.loads(line)
                records[r["image"]] = r
    pages = list(csv.DictReader((a.boundary / "pages_bmes.csv").open(encoding="utf-8")))
    by_chart: dict[str, list[dict]] = collections.defaultdict(list)
    for p in pages:
        by_chart[p["chart_id"]].append(p)

    prow, pairs, jrows = [], [], []
    for chart, ps in by_chart.items():
        ps.sort(key=lambda p: int(p["page_index"]))
        feats = {p["image"]: page_feats(records[p["image"]]) for p in ps}
        for p in ps:
            prow.append({"chart_id": chart, "page_index": int(p["page_index"]), "image": p["image"], "doc_id": p["doc_id"],
                         "tag": p["tag"], "L1": p["L1"], "L2": p["L2"], "group": group_of(p["L1"], groups),
                         "unsure_before": p["unsure_before"], "split": p["split"]})
        for x, y in zip(ps, ps[1:]):
            ra, rb = records[x["image"]], records[y["image"]]
            same = int(x["doc_id"] == y["doc_id"])
            gx, gy = group_of(x["L1"], groups), group_of(y["L1"], groups)
            row = {"pair_id": f"{chart}|{x['page_index']}|{y['page_index']}", "chart_id": chart,
                   "page_n": int(x["page_index"]), "page_n1": int(y["page_index"]),
                   "gap": int(int(y["page_index"]) != int(x["page_index"]) + 1),
                   "image_n": x["image"], "image_n1": y["image"],
                   "doc_continues": same, "doc_breaks": 1 - same,
                   "L1_n": x["L1"], "L2_n": x["L2"], "L1_n1": y["L1"], "L2_n1": y["L2"],
                   "group_n": gx, "group_n1": gy, "same_group": int(gx == gy),
                   "unsure": int(y["unsure_before"] == "1"), "split": x["split"],
                   **pair_feats(ra, rb, feats[x["image"]], feats[y["image"]])}
            pairs.append(row)
            jrows.append({k: row[k] for k in ("pair_id", "doc_continues", "doc_breaks", "group_n", "group_n1",
                                              "unsure", "split")}
                         | {"tail_n": "\n".join(lines(ra.get("text")))[-900:],
                            "head_n1": "\n".join(lines(rb.get("text")))[:900]})

    a.out.mkdir(parents=True, exist_ok=True)

    def write(name, rows):
        with (a.out / name).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    write("pages_document.csv", prow)
    write("document_pairs.csv", pairs)
    with (a.out / "document_pairs.jsonl").open("w", encoding="utf-8") as fh:
        for j in jrows:
            fh.write(json.dumps(j) + "\n")

    docs: dict[str, list[dict]] = collections.defaultdict(list)
    for p in prow:
        docs[p["doc_id"]].append(p)
    multi = collections.Counter(ps[0]["group"] for ps in docs.values() if len(ps) > 1)
    within = collections.Counter(r["group_n"] for r in pairs if r["doc_continues"])
    cross_group_continue = sum(1 for r in pairs if r["doc_continues"] and not r["same_group"])
    same_group_break = sum(1 for r in pairs if r["doc_breaks"] and r["same_group"])
    rep = [
        f"pages {len(prow)}; documents {len(docs)} (multi-page {sum(1 for d in docs.values() if len(d) > 1)})",
        f"meeting points {len(pairs)}: continues {sum(r['doc_continues'] for r in pairs)}, breaks {sum(r['doc_breaks'] for r in pairs)}, unsure {sum(r['unsure'] for r in pairs)}",
        f"multi-page documents by first page's group: {dict(multi.most_common())}",
        f"continues by group of page n: {dict(within.most_common())}",
        f"continues across groups (a page inside a note, e.g. its lab results): {cross_group_continue}",
        f"breaks between two pages of one group (two documents of a kind in a row): {same_group_break}",
    ]
    for sp in ("train", "val", "test"):
        s = [r for r in pairs if r["split"] == sp]
        rep.append(f"  {sp}: meeting points {len(s)}, breaks {sum(r['doc_breaks'] for r in s)}")
    (a.out / "document_report.txt").write_text("\n".join(rep) + "\n", encoding="utf-8")
    print("\n".join(rep))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
