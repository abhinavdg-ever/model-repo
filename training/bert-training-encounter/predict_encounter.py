#!/usr/bin/env python3
"""Try the encounter-type model on a page's text: the class, its confidence and
every class's probability.

    python predict_encounter.py --text "TELEHEALTH VISIT ..."
    python predict_encounter.py --file page.txt
    python predict_encounter.py --jsonl pages.jsonl --out predictions.csv   # many pages; no text in the CSV
    python predict_encounter.py --model output/encounter

In a notebook:

    from predict_encounter import EncounterClassifier
    clf = EncounterClassifier("output/encounter")
    clf.show(text)

This is the page's own vote. The document's encounter type is the vote over its
pages (first page twice; Not Available only when every page says so), and the
client's default (outpatient with no modality stated -> Evisit) is a rule after
that, not something the model learned.

Needs train_encounter.py beside it.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

import train_encounter as enc

COLUMNS = ["image", "encounter_type", "confidence", *[f"p_{c}" for c in enc.CLASSES]]


class EncounterClassifier:
    def __init__(self, model_dir: Path | str = enc.DEFAULT_OUTPUT, device: Optional[str] = None):
        import torch
        from transformers.utils import logging as hf_logging

        hf_logging.set_verbosity_error()
        model_dir = Path(model_dir)
        if not (model_dir / "label_space.json").is_file():
            raise SystemExit(f"No trained encounter model at {model_dir} — run train_encounter.py first")
        self.torch = torch
        self.device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
        self.model, self.tokenizer, self.space = enc.load_model(model_dir, self.device)
        self.max_length = int(self.space.get("max_length", enc.MAX_LENGTH))
        self.head_fraction = float(self.space.get("head_fraction", enc.HEAD_FRACTION))

    def predict_many(self, texts: list[str]) -> list[dict]:
        torch = self.torch
        ids = enc.encode_texts(self.tokenizer, [t or "" for t in texts], self.max_length, self.head_fraction)
        pad = self.tokenizer.pad_token_id or 0
        width = max(len(x) for x in ids)
        input_ids = torch.tensor([x + [pad] * (width - len(x)) for x in ids], device=self.device)
        mask = torch.tensor([[1] * len(x) + [0] * (width - len(x)) for x in ids], device=self.device)
        with torch.no_grad():
            probs = torch.softmax(self.model(input_ids, mask), -1).tolist()
        out = []
        for row in probs:
            k = max(range(len(row)), key=row.__getitem__)
            out.append({"encounter_type": enc.CLASSES[k], "confidence": row[k],
                        "probabilities": dict(zip(enc.CLASSES, row))})
        return out

    def predict(self, text: str) -> dict:
        return self.predict_many([text])[0]

    def show(self, text: str) -> dict:
        r = self.predict(text)
        print(f"{len((text or '').split())} words in. Encounter type: {r['encounter_type']} ({r['confidence']:.1%})")
        for name, p in sorted(r["probabilities"].items(), key=lambda kv: -kv[1]):
            print(f"  {name:<24} {p:.1%}")
        return r


def predict_jsonl(clf: EncounterClassifier, source: Path, out: Path, batch: int = 16) -> int:
    pages = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    lines = []
    for start in range(0, len(pages), batch):
        chunk = pages[start:start + batch]
        for page, r in zip(chunk, clf.predict_many([str(p.get("text") or "") for p in chunk])):
            lines.append([str(page.get("image") or ""), r["encounter_type"], round(r["confidence"], 4),
                          *[round(r["probabilities"][c], 4) for c in enc.CLASSES]])
    enc.write_csv(out, COLUMNS, lines)
    return len(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", type=Path, default=enc.DEFAULT_OUTPUT, help="trained encounter folder")
    parser.add_argument("--text", help="the page text")
    parser.add_argument("--file", type=Path, help="a text file with the page text")
    parser.add_argument("--jsonl", type=Path, help="pages to predict (image, text per line)")
    parser.add_argument("--out", type=Path, default=Path("predictions.csv"), help="CSV for --jsonl")
    parser.add_argument("--device", choices=("cpu", "cuda"), default=None)
    args = parser.parse_args(argv)

    clf = EncounterClassifier(args.model, args.device)
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
