#!/usr/bin/env python3
"""Try the page-axes model: L1 (with its top 3), L2, L3, specialty and
codeability for a page's text, with a confidence each and a route.

    python predict_axes.py --text "OPERATIVE REPORT ..."
    python predict_axes.py --file page.txt
    python predict_axes.py --jsonl pages.jsonl --out predictions.csv   # many pages; no text in the CSV
    python predict_axes.py --model output/page-axes

In a notebook:

    from predict_axes import AxesClassifier
    clf = AxesClassifier("output/page-axes")
    clf.show(text)

L2 is chosen only among the L2s of the predicted L1, and L3 among the L3s of
the predicted L2. Under the section L1 (Progress Note) there is no L2 here:
section tagging owns it. ``route`` says what to do with the answer: ``model``
(trust it), ``canon`` (let the keyword canon decide), ``review``.

Needs train_axes.py beside it; the model folder alone carries every label.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Optional

import train_axes as axes

DEFAULT_MODEL = axes.DEFAULT_OUTPUT
COLUMNS = ["image", "path", "paths", "route", "L1", "L1_confidence", "L1_top3", "L2", "L2_confidence", "L3",
           "L3_confidence", "specialty", "specialty_confidence", "codeability", "codeability_confidence"]


class AxesClassifier:
    def __init__(self, model_dir: Path | str = DEFAULT_MODEL, device: Optional[str] = None):
        import torch
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
        model_dir = Path(model_dir)
        if not (model_dir / "label_space.json").is_file():
            raise SystemExit(f"No trained page-axes model at {model_dir} — run train_axes.py first")
        self.torch = torch
        self.device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
        self.model, self.tokenizer, self.space = axes.load_model(model_dir, self.device)
        self.max_length = int(self.space.get("max_length", axes.MAX_LENGTH))
        self.head_fraction = float(self.space.get("head_fraction", axes.HEAD_FRACTION))

    def predict_many(self, texts: list[str]) -> list[dict]:
        torch = self.torch
        ids = axes.encode_texts(self.tokenizer, [t or "" for t in texts], self.max_length, self.head_fraction)
        pad = self.tokenizer.pad_token_id or 0
        width = max(len(x) for x in ids)
        input_ids = torch.tensor([x + [pad] * (width - len(x)) for x in ids], device=self.device)
        mask = torch.tensor([[1] * len(x) + [0] * (width - len(x)) for x in ids], device=self.device)
        with torch.no_grad():
            rows = axes.decode(self.model(input_ids, mask), self.model, self.space)
        for row in rows:
            row["route"] = axes.route(row["L1_top"], self.space["other_L1"],
                                      self.space.get("route_model", axes.ROUTE_MODEL),
                                      self.space.get("route_canon", axes.ROUTE_CANON),
                                      self.space.get("route_margin", axes.ROUTE_MARGIN))
        return rows

    def predict(self, text: str) -> dict:
        """{L1, L1_confidence, L1_top, L2, L3, specialty, codeability (+ confidences), path, route}."""
        return self.predict_many([text])[0]

    def show(self, text: str) -> dict:
        r = self.predict(text)
        print(f"{len((text or '').split())} words in. Route: {r['route']}")
        print("  L1 top 3: " + ", ".join(f"{n} {p:.1%}" for n, p in r["L1_top"]))
        for head in ("L2", "L3", "specialty", "codeability"):
            value, confidence = r[head], r[f"{head}_confidence"]
            print(f"  {head:<12} {value or '—'}" + (f"  ({confidence:.1%})" if confidence is not None else ""))
        if r["L1"] == self.space["section_L1"]:
            print(f"  ({r['L1']}: L2 is the content section, decided by section tagging, not this model)")
        elif r["L1"] == self.space["other_L1"]:
            print(f"  ({r['L1']}: a page type with too few training pages — the keyword canon decides)")
        print(f"  path: {r['path']}")
        print("  best paths: " + "; ".join(f"{x['path']} {x['score']:.1%}" for x in r["paths"]))
        return r


def _cells(image: str, r: dict) -> list:
    rounded = lambda v: "" if v is None else round(v, 4)  # noqa: E731
    return [image, r["path"], "; ".join(f"{x['path']} {x['score']:.2f}" for x in r["paths"]), r["route"], r["L1"], rounded(r["L1_confidence"]),
            "; ".join(f"{n} {p:.2f}" for n, p in r["L1_top"]), r["L2"] or "", rounded(r["L2_confidence"]),
            r["L3"] or "", rounded(r["L3_confidence"]), r["specialty"] or "", rounded(r["specialty_confidence"]),
            r["codeability"], rounded(r["codeability_confidence"])]


def predict_jsonl(clf: AxesClassifier, source: Path, out: Path, batch: int = 16) -> int:
    pages = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    lines = []
    for start in range(0, len(pages), batch):
        chunk = pages[start:start + batch]
        for page, r in zip(chunk, clf.predict_many([str(p.get("text") or "") for p in chunk])):
            lines.append(_cells(str(page.get("image") or ""), r))
    axes.write_csv(out, COLUMNS, lines)
    return len(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="trained page-axes folder")
    parser.add_argument("--text", help="the page text")
    parser.add_argument("--file", type=Path, help="a text file with the page text")
    parser.add_argument("--jsonl", type=Path, help="pages to predict (image, text per line)")
    parser.add_argument("--out", type=Path, default=Path("predictions.csv"), help="CSV for --jsonl")
    parser.add_argument("--device", choices=("cpu", "cuda"), default=None)
    args = parser.parse_args(argv)

    clf = AxesClassifier(args.model, args.device)
    if args.jsonl is not None:
        n = predict_jsonl(clf, args.jsonl, args.out)
        print(f"{n} pages predicted → {args.out}")
        return 0
    if args.text is not None:
        text = args.text
    elif args.file is not None:
        text = args.file.read_text(encoding="utf-8")
    else:
        print("Paste the page text, then Ctrl+D (Windows: Ctrl+Z, Enter):", file=sys.stderr)
        text = sys.stdin.read()
    clf.show(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
