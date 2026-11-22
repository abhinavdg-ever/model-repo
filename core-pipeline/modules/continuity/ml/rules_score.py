"""Today's rules engine on the labelled continuity charts (Brief 3 section 2.5: "also score the
rules engine on the same test charts"), so the clue model and the rules are compared on the same
meeting points.

    python -m modules.continuity.ml.rules_score --table ../training/continuity/data/clue_table.csv \\
        --text "<Training>/processed/training_data.jsonl" [--page-types page_types.csv] --out rules_decisions.csv

The engine's inputs are approximated from the page text, as the training pages have no pipeline run
behind them: lines without OCR positions, the date of service from the clue code's date reader
(not the DOS stage), signatures from the text (no key / value extraction), printed page numbers from
the text. Writes pair_id -> rules_break (1 / 0). Ids only, never text.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Optional

from modules.continuity.engine import PageInput, assign, text_lines
from modules.continuity.ml import clues
from modules.continuity.ml.build_data import page_types
from modules.continuity.signature import has_signature_section


def page_input(i: int, image: str, text: str, l1: str, l2: str) -> PageInput:
    lines = text_lines(text)
    dos = clues.dos_dates(text)
    _first, last = clues.page_marks(text)
    tail = "\n".join(clues.tail(text))
    return PageInput(page_id=i, page_name=image, page_number=i, text=text, lines=lines, page_type=l1 or "",
                     page_subtype=l2 or "", names=tuple(x for x in (l2, l1) if x), dos_from=dos[0] if dos else "",
                     dos_trust=5 if dos else None, signed=has_signature_section(text),
                     signed_in_tail=has_signature_section(tail), printed=last)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", type=Path, required=True)
    ap.add_argument("--text", type=Path, required=True)
    ap.add_argument("--page-types", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    with a.table.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    wanted = {r["image_n"] for r in rows} | {r["image_n1"] for r in rows}
    texts = {}
    with a.text.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                r = json.loads(line)
                if r.get("image") in wanted:
                    texts[r["image"]] = r.get("text") or ""
    predicted = page_types(a.page_types)
    by_chart = defaultdict(list)
    for r in rows:
        by_chart[r["chart_id"]].append(r)
    out = []
    for chart, chart_rows in by_chart.items():
        chart_rows.sort(key=lambda r: int(r["page_n"]))
        chain: list[dict] = []
        for r in chart_rows + [None]:
            if r is not None and (not chain or chain[-1]["image_n1"] == r["image_n"]):
                chain.append(r)
                continue
            if chain:
                images = [chain[0]["image_n"]] + [c["image_n1"] for c in chain]
                pages = []
                for k, image in enumerate(images):
                    side = "n" if k < len(chain) else "n1"
                    src = chain[min(k, len(chain) - 1)]
                    t = predicted.get(image) or {"L1": src.get(f"L1_{side}", ""), "L2": ""}
                    pages.append(page_input(k, image, texts.get(image, ""), t.get("L1", ""), t.get("L2", "")))
                seq = [row["document_seq"] for row in assign(pages)]
                for k, c in enumerate(chain):
                    out.append({"pair_id": c["pair_id"], "rules_break": int(seq[k + 1] != seq[k])})
            chain = [r] if r is not None else []
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("w", newline="", encoding="utf-8") as handle:
        w = csv.DictWriter(handle, fieldnames=["pair_id", "rules_break"])
        w.writeheader()
        w.writerows(out)
    print(json.dumps({"points": len(out), "breaks": sum(o["rules_break"] for o in out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
