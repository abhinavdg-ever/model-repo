"""The lab-inside-a-note test set (Brief 5 section 2, 2026-11-22): measure before changing anything.

From the reviewed charts (train + val only):

* **embedded**: Lab Report pages whose reviewed document is opened by a Progress Note, and pages
  labelled Progress Note > Lab and Imaging Results -- the Final answer should be Progress Note >
  Lab and Imaging Results;
* **stand-alone**: as many Lab Report pages in documents opened by a Lab Report (seeded sample) --
  the Final answer should stay Lab Report.

Each chart that holds a test page runs the pipeline's own steps offline: the Extracted page type
(``leaf.predict`` -> the stage's row), continuity's documents (the rules engine, inputs from the text
as ``continuity/ml/rules_score.py`` builds them), and the Final page type
(``arbitration.decide_final``, as ``imaging_final`` calls it). Every wrong page gets its cause
(Page Classification 101, "How to tell which cause it is"):

* ``4`` the lab page opens a pipeline document that runs on;
* ``3/5`` continuity broke before it (a different document from the page before);
* ``6`` same document as the note page before, but the Final family is not Progress Note;
* ``7`` it stood alone (``continuation_rule = no_change``);
* ``other`` Final family right, subtype wrong.

    python -m modules.page_classify.lab_in_note_eval --pages <pages_document.csv> --text <training_data.jsonl> \\
        --out <Training>/eval/page_classify/lab_in_note_report.csv

Writes ids, labels and causes only; never text.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

PN, LAB, LIR = "Progress Note", "Lab Report", "Lab and Imaging Results"
SEED = 42


def select(pages: list[dict[str, str]]) -> dict[str, str]:
    """image -> embedded | standalone, from the reviewed documents (train + val)."""
    docs: dict[str, list[dict[str, str]]] = defaultdict(list)
    for p in pages:
        docs[p["doc_id"]].append(p)
    embedded, standalone = [], []
    for members in docs.values():
        members.sort(key=lambda p: int(p["page_index"]))
        if members[0]["split"] not in ("train", "val"):
            continue
        opener = members[0]["L1"]
        for k, p in enumerate(members):
            if p["L1"] == PN and p["L2"] == LIR:
                embedded.append(p["image"])
            elif p["L1"] == LAB and opener == PN and k > 0:
                embedded.append(p["image"])
            elif p["L1"] == LAB and opener == LAB:
                standalone.append(p["image"])
    rng = random.Random(SEED)
    standalone = rng.sample(standalone, min(len(standalone), len(embedded)))
    return {**{i: "embedded" for i in embedded}, **{i: "standalone" for i in standalone}}


def run_chart(chart_pages: list[dict[str, str]], texts: dict[str, str]) -> dict[str, dict[str, Any]]:
    """image -> Extracted, document and Final for one chart's pages (in order)."""
    from modules.continuity.engine import assign
    from modules.continuity.ml.rules_score import page_input
    from modules.page_classify import leaf
    from modules.page_classify.arbitration import decide_final, note_sections
    from modules.page_classify.stage import _page_row
    from modules.imaging_final.stage import _final_page

    kinds, inputs = [], []
    for k, p in enumerate(chart_pages):
        text = texts.get(p["image"], "")
        result = leaf.predict(text)
        row = _page_row(result)
        kinds.append(row)
        inputs.append(page_input(k, p["image"], text, result.l1 if result else "", (result.l2 or "") if result else ""))
    links = assign(inputs)
    finals_in: list[Any] = []
    for k, (p, kind, link) in enumerate(zip(chart_pages, kinds, links)):
        finals_in.append(_final_page(k, kind, link, text=texts.get(p["image"], "")))
    by_doc: dict[Any, list[Any]] = defaultdict(list)
    for fp in finals_in:
        by_doc[fp.document_seq].append(fp)
    out = {}
    for k, (p, fp) in enumerate(zip(chart_pages, finals_in)):
        doc = by_doc.get(fp.document_seq) or [fp]
        final = decide_final(fp, doc if fp.document_seq is not None else None)
        out[p["image"]] = {"k": k, "extracted": f"{fp.page_type} / {fp.page_subtype}", "document_seq": fp.document_seq,
                           "seq": fp.seq, "doc_size": len(doc), "final_type": final.page_type,
                           "final_subtype": final.page_subtype, "source": final.source,
                           "continuation_rule": final.continuation_rule}
    return out


def _family(page_type: Optional[str]) -> str:
    return (page_type or "").split(" (")[0]


def cause(res: dict[str, Any], prev: Optional[dict[str, Any]]) -> str:
    if res["seq"] == 1 and res["doc_size"] > 1:
        return "4"
    if prev is not None and prev["document_seq"] != res["document_seq"]:
        return "3/5"
    if _family(res["final_type"]) != PN:
        return "7" if res["continuation_rule"] == "no_change" else "6"
    return "other"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pages", type=Path, required=True)
    ap.add_argument("--text", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    with a.pages.open(encoding="utf-8") as handle:
        pages = list(csv.DictReader(handle))
    chosen = select(pages)
    charts = {p["chart_id"] for p in pages if p["image"] in chosen}
    wanted = {p["image"] for p in pages if p["chart_id"] in charts}
    texts = {}
    with a.text.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                r = json.loads(line)
                if r.get("image") in wanted:
                    texts[r["image"]] = r.get("text") or ""
    rows = []
    for chart in sorted(charts):
        chart_pages = sorted((p for p in pages if p["chart_id"] == chart), key=lambda p: int(p["page_index"]))
        results = run_chart(chart_pages, texts)
        ordered = [results[p["image"]] for p in chart_pages]
        for p in chart_pages:
            if p["image"] not in chosen:
                continue
            res = results[p["image"]]
            group = chosen[p["image"]]
            fam, sub = _family(res["final_type"]), res["final_subtype"]
            ok = (fam == PN and sub == LIR) if group == "embedded" else fam == LAB
            prev = ordered[res["k"] - 1] if res["k"] > 0 else None
            rows.append({"chart_id": chart, "image": p["image"], "group": group,
                         "reviewed": f"{p['L1']} / {p['L2']}", "extracted": res["extracted"],
                         "final": f"{res['final_type']} / {sub}", "continuation_rule": res["continuation_rule"],
                         "document_seq": res["document_seq"], "seq": res["seq"], "right": int(ok),
                         "cause": "" if ok or group == "standalone" else cause(res, prev)})
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    emb = [r for r in rows if r["group"] == "embedded"]
    sta = [r for r in rows if r["group"] == "standalone"]
    print(json.dumps({
        "charts": len(charts), "embedded": len(emb), "standalone": len(sta),
        "embedded_fixed": sum(r["right"] for r in emb), "embedded_fixed_pct": round(100 * sum(r["right"] for r in emb) / max(1, len(emb)), 1),
        "standalone_pulled_into_note": sum(1 - r["right"] for r in sta),
        "standalone_pulled_pct": round(100 * sum(1 - r["right"] for r in sta) / max(1, len(sta)), 1),
        "wrong_embedded_by_cause": dict(Counter(r["cause"] for r in emb if not r["right"])),
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
