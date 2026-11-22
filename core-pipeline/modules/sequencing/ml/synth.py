"""Shuffle test cases (Brief 2 Q1, 2026-11-22). The prebuilt cases are in
``Training/segmentation_training_data/sequencing/local_synth_cases.jsonl`` (none, swap_neighbours,
move_one, reverse_run, two_mistakes; val and test blocks only). This adds ``split_by_break``: a swap that
puts an end-of-note page (signed near the end of its text) before the page that continues the note, so
continuity's first pass breaks after it and the stray page lands in the next encounter (Brief 2 Step 5).
Every case stays inside one DOS block; seeds are fixed.
"""
from __future__ import annotations

import random
from typing import Iterable

from modules.continuity.ml.clues import signature_kinds

SEED = 42


def split_by_break(cases: Iterable[dict], texts: dict[str, str], seed: int = SEED) -> list[dict]:
    """One split_by_break case per in-order block (from its ``none`` case) that has an end-of-note page
    with a page before it in the block."""
    rng = random.Random(seed)
    out = []
    for case in cases:
        if case.get("shuffle_type") != "none":
            continue
        true = list(case["true_order"])
        ends = [i for i in range(1, len(true)) if signature_kinds(texts.get(true[i], ""))["sig_end_tail"]]
        if not ends:
            continue
        i = rng.choice(ends)
        scan = true[:i - 1] + [true[i], true[i - 1]] + true[i + 1:]
        out.append({**case, "case_id": case["case_id"].replace("|none", "|split_by_break"),
                    "shuffle_type": "split_by_break", "scan_order": scan, "true_order": true, "seed": seed})
    return out
