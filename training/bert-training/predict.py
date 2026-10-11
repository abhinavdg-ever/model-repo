#!/usr/bin/env python3
"""Try the trained model on some text: the top model types with probabilities.

    python predict.py --text "DISCHARGE SUMMARY ..."        # text on the command line
    python predict.py --file page.txt                       # text from a file
    python predict.py                                       # paste text, then Ctrl+D (Ctrl+Z Enter on Windows)
    python predict.py --model output/page-family --top 5

In a notebook:

    from predict import Classifier
    clf = Classifier("output/page-family")
    clf.show(text)

This is BERT alone. The pipeline also runs the keyword model and the
decision ladder (core-pipeline/stages/lib/page_classify), so its final answer
can differ — especially for model types BERT was not trained on.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = HERE / "output" / "page-family"
TAXONOMY = HERE / "taxonomy.json"
MAX_LENGTH = 512


class Classifier:
    def __init__(self, model_dir: Path | str = DEFAULT_MODEL, device: Optional[str] = None):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        model_dir = Path(model_dir)
        if not (model_dir / "config.json").is_file():
            raise SystemExit(f"No trained model at {model_dir} — run train.py first")
        self.torch = torch
        self.device = torch.device(device or ("cuda" if torch.cuda.is_available() else "cpu"))
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_dir).to(self.device).eval()
        self.labels = {int(k): v for k, v in self.model.config.id2label.items()}
        taxonomy = json.loads(TAXONOMY.read_text(encoding="utf-8"))
        self.info = {m["model_type"]: m for m in taxonomy["model_types"]}
        self.untrained = [m for m in self.info if m not in set(self.labels.values())]

    def predict(self, text: str, top: int = 5) -> list[dict]:
        """[{model_type, probability, page_type, codability}], best first."""
        torch = self.torch
        inputs = self.tokenizer(text or "", truncation=True, max_length=MAX_LENGTH, return_tensors="pt")
        inputs = {k: v.to(self.device) for k, v in inputs.items()}
        with torch.no_grad():
            probabilities = torch.softmax(self.model(**inputs).logits[0], dim=-1).cpu()
        best = torch.topk(probabilities, k=min(top, len(self.labels)))
        out = []
        for p, i in zip(best.values.tolist(), best.indices.tolist()):
            model_type = self.labels[int(i)]
            info = self.info.get(model_type, {})
            out.append({
                "model_type": model_type,
                "probability": round(float(p), 4),
                "page_type": info.get("page_type", ""),
                "codability": info.get("codability", ""),
            })
        return out

    def show(self, text: str, top: int = 5) -> list[dict]:
        """Print the top predictions as a small table; returns them too."""
        rows = self.predict(text, top)
        words = len((text or "").split())
        print(f"{words} words in; BERT top {len(rows)} (the pipeline's keyword ladder is not applied here):")
        print(f"  {'probability':>11}  {'model type':<34} {'page type':<28} codability")
        for r in rows:
            print(f"  {r['probability']:>11.1%}  {r['model_type']:<34} {r['page_type']:<28} {r['codability']}")
        if rows:
            top = rows[0]["probability"]
            lead = top - (rows[1]["probability"] if len(rows) > 1 else 0.0)
            # The pipeline's ladder (page_arbitration.json): BERT wins outright at
            # 50%, or at 25% with a 10-point lead over its second choice.
            if top >= 0.50 or (top >= 0.25 and lead >= 0.10):
                verdict = "BERT would win in the pipeline"
            elif top >= 0.25:
                verdict = "BERT would win unless the keyword model has a clear title — flagged for review"
            elif top >= 0.10:
                verdict = "keyword model if clear, else the two models' top 3 are compared — flagged for review"
            else:
                verdict = "Unknown in the pipeline, unless the keyword model has a clear title"
            print(f"  lead over second: {lead:.1%} — {verdict}")
        return rows


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="trained model folder")
    parser.add_argument("--text", help="the page text")
    parser.add_argument("--file", type=Path, help="a text file with the page text")
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--device", choices=("cpu", "cuda"), default=None)
    args = parser.parse_args(argv)

    if args.text is not None:
        text = args.text
    elif args.file is not None:
        text = args.file.read_text(encoding="utf-8")
    else:
        print("Paste the page text, then Ctrl+D (Windows: Ctrl+Z, Enter):", file=sys.stderr)
        text = sys.stdin.read()
    Classifier(args.model, args.device).show(text, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
