#!/usr/bin/env python3
"""Run a trained page-leaf model on page text files and print each page's best leaves.

    python predict_leaf.py output/page-leaf page1.txt page2.txt

Prints the file name, then its kept leaves (top 5, at most 3 per L1) with their scores and
the specialty. Never prints page text.
"""
from __future__ import annotations

import sys
from pathlib import Path

from train_leaf import decode, encode_texts, load_model


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    folder, files = Path(argv[0]), [Path(a) for a in argv[1:]]
    import torch

    model, tokenizer, space = load_model(folder)
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        ids = encode_texts(tokenizer, [text], int(space["max_length"]), float(space["head_fraction"]))[0]
        with torch.no_grad():
            logits = model(torch.tensor([ids]), torch.ones((1, len(ids)), dtype=torch.long))
        row = decode(logits, space)[0]
        print(path.name)
        for leaf in row["leaves"]:
            print(f"  {leaf['score']:.4f}  {leaf['leaf']}  ({leaf['codeability'] or '-'})")
        if row.get("specialty"):
            print(f"  specialty {row['specialty']} ({row['specialty_confidence']:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
