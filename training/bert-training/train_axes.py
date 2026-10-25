#!/usr/bin/env python3
"""Train the page-axes model: one encoder, five heads (L1, L2, L3, specialty,
codeability) over the v29.2 taxonomy.

Input is ``training_data.jsonl`` (one page per line: ``text`` plus the labels
``L1``, ``L2``, ``L3``, ``specialty``, ``codeability``, and ``chart_id``), a copy
of ``Training/processed/training_data.jsonl``. Every name comes from
``taxonomy.json`` beside this file (v29.2, from the 2026-10-25 Claude Code
pack); nothing here knows a page type. Output, by default ``output/page-axes``,
always holds the best epoch by validation macro F1 of the selected heads.

    python train_axes.py --check              # two steps and a time estimate; saves nothing
    python train_axes.py                      # a run: 20 epochs
    python train_axes.py --with-mock          # + data/mock_pages.jsonl, training split only
    python train_axes.py --base-model /models/distilbert-base-uncased   # vendored weights

How the label space is built (all thresholds are options):

* L1 — taxonomy L1s with fewer than ``--min-l1-pages`` pages fold into one
  ``OTHER`` class, which routes the page to the keyword canon.
* L2 — classes are (L1, L2) pairs, because L2 names repeat across L1s. The
  logits are masked to the pairs under the row's L1 (the gold L1 in training,
  the predicted one at inference). No L2 loss for the section L1 (its L2 is
  the content section, owned by section tagging), for folded L1s, for a blank
  L2, or for a pair with fewer than ``--min-l2-pages`` pages.
* L3 — classes are (L1, L2, L3) triples, only under L2s that have named L3s in
  the taxonomy; masked by the L2 the same way. A blank L3 there is a missing
  label and is left out of the loss. Rare L3s fold into the fallback (Others)
  under the same parent.
* specialty — values with at least ``--min-specialty-pages`` pages; rarer ones
  are left out of the loss.
* codeability — the four values from the taxonomy rules.

Synthetic pages (``--with-mock``, or ``synthetic: true`` in the data) are always
in the training split, never validation or test, so every score is a real-page
score. The summary prints the Lab Report <-> Progress Note confusion, both ways.

Page text is never printed: only counts, class names, shapes and metrics.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = HERE / "data" / "training_data.jsonl"
DEFAULT_OUTPUT = HERE / "output" / "page-axes"
DEFAULT_MOCK = HERE / "data" / "mock_pages.jsonl"
TAXONOMY = HERE / "taxonomy.json"

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

BASE_MODEL = "distilbert-base-uncased"
MAX_LENGTH = 512
HEAD_FRACTION = 0.85  # of the token budget kept from the start; the rest from the end
BATCH_SIZE = 8
LOG_EVERY = 10  # training steps between running-loss lines; 0 turns them off
LEARNING_RATE = 3e-5
WARMUP_FRACTION = 0.1  # of all steps, then linear decay
EPOCHS = 20
SEED = 42
DROPOUT = 0.1
VAL_FRACTION = 0.15
TEST_FRACTION = 0.15
MIN_CHARS = 20
# An L1 is trained when it has more than 10 labelled pages (at least 11);
# below that it joins OTHER. L2, L3 and specialty need 3: a rarer L3 joins its
# L2's Others, and a rarer L2 or specialty is left out of that output's loss.
MIN_L1_PAGES = 11
MIN_L2_PAGES = 3
MIN_L3_PAGES = 3
MIN_SPECIALTY_PAGES = 3
OTHER_L1 = "OTHER"
UNKNOWN_SPECIALTY = "Unknown"  # "not determined", not "no specialty"
ROUTE_MODEL = 0.50
ROUTE_MARGIN = 0.10
ROUTE_CANON = 0.25
SELECT_HEADS = ("L1", "L2", "L3")
# The pair the client asks about: genuine results pages must not read as a note.
LAB_L1 = "Lab Report"
NOTE_L1 = "Progress Note"

HEADS = ("L1", "L2", "L3", "specialty", "codeability")
IGNORE = -100
MASKED = -1e4  # not -inf: a fully masked row stays finite under mixed precision
SEP = " > "
TOP_PATHS = 5  # whole paths kept per page (``paths``)
PATHS_PER_L1 = 3  # of which at most this many under one L1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=DEFAULT_DATA, help="training_data.jsonl (default: data/)")
    p.add_argument("--with-mock", nargs="?", type=Path, const=DEFAULT_MOCK, default=None, metavar="JSONL",
                   help="add synthetic pages (default data/mock_pages.jsonl), training split only")
    p.add_argument("--specialty-unknown", choices=("train", "ignore"), default="train",
                   help="'Unknown' specialty (not determined): a class of its own, or left out of the loss")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="model folder (best epoch)")
    p.add_argument("--taxonomy", type=Path, default=TAXONOMY)
    p.add_argument("--base-model", default=BASE_MODEL,
                   help="Hugging Face name or a local folder (use a folder where Hugging Face is blocked)")
    p.add_argument("--check", action="store_true", help="two training steps and a time estimate; saves nothing")
    p.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    p.add_argument("--epochs", type=int, default=EPOCHS)
    p.add_argument("--max-length", type=int, default=MAX_LENGTH)
    p.add_argument("--head-fraction", type=float, default=HEAD_FRACTION)
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--log-every", type=int, default=LOG_EVERY, help="steps between loss lines (0: per epoch only)")
    p.add_argument("--lr", type=float, default=LEARNING_RATE)
    p.add_argument("--dropout", type=float, default=DROPOUT)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--val-fraction", type=float, default=VAL_FRACTION)
    p.add_argument("--test-fraction", type=float, default=TEST_FRACTION, help="0 disables the test split")
    p.add_argument("--min-chars", type=int, default=MIN_CHARS)
    p.add_argument("--min-l1-pages", type=int, default=MIN_L1_PAGES)
    p.add_argument("--min-l2-pages", type=int, default=MIN_L2_PAGES)
    p.add_argument("--min-l3-pages", type=int, default=MIN_L3_PAGES)
    p.add_argument("--min-specialty-pages", type=int, default=MIN_SPECIALTY_PAGES)
    p.add_argument("--other-l1", default=OTHER_L1, help="name of the folded L1 class")
    p.add_argument("--route-model", type=float, default=ROUTE_MODEL)
    p.add_argument("--route-canon", type=float, default=ROUTE_CANON)
    p.add_argument("--select-heads", default=",".join(SELECT_HEADS),
                   help="heads whose mean validation macro F1 picks the best epoch")
    return p.parse_args(argv)


# --- Taxonomy and records ---------------------------------------------------------


def load_taxonomy(path: Path = TAXONOMY) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"No taxonomy at {path} (copy taxonomy.json beside train_axes.py)")
    return json.loads(path.read_text(encoding="utf-8"))


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def validate(records: list[dict], taxonomy: dict[str, Any]) -> list[str]:
    """Problems as 'what (count): names' lines. Empty means every label is in the taxonomy."""
    l1s = {x["name"] for x in taxonomy["L1"]}
    l2s = {a: {x["name"] for x in v} for a, v in taxonomy["L2"].items()}
    l3s = {parent: {x["name"] for x in v} for parent, v in taxonomy["L3"].items()}
    specialties = {x["name"] for x in taxonomy["specialty"]}
    codeability = set(taxonomy["rules"]["codeability_values"])
    bad: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        a, b, c = r["L1"], r["L2"], r["L3"]
        if a not in l1s:
            bad["L1 not in taxonomy"][a or "<empty>"] += 1
        elif b and b not in l2s.get(a, set()):
            bad["L2 not under its L1"][f"{a}{SEP}{b}"] += 1
        elif c and c not in l3s.get(f"{a}{SEP}{b}", set()):
            bad["L3 not under its L2"][f"{a}{SEP}{b}{SEP}{c}"] += 1
        if r["specialty"] and r["specialty"] not in specialties:
            bad["specialty not in taxonomy"][r["specialty"]] += 1
        if r["codeability"] not in codeability:
            bad["codeability not a codeability value"][r["codeability"] or "<empty>"] += 1
    return [f"{what} ({sum(c.values())}): " + ", ".join(f"{n!r} ({k})" for n, k in sorted(c.items()))
            for what, c in bad.items()]


def load_records(path: Path, taxonomy: dict[str, Any], min_chars: int) -> tuple[list[dict], int]:
    """Usable pages, and how many were left out for having almost no text."""
    if not path.is_file():
        raise SystemExit(f"No training file at {path}")
    records, short = [], 0
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path.name} line {number} is not JSON ({exc.msg})") from None
            text = str(row.get("text") or "")
            if len("".join(text.split())) < min_chars:
                short += 1
                continue
            records.append({
                "image": str(row.get("image") or f"line-{number}"),
                "text": text,
                "chart_id": _clean(row.get("chart_id")),
                "synthetic": bool(row.get("synthetic")),
                **{k: _clean(row.get(k)) for k in ("L1", "L2", "L3", "specialty", "codeability")},
            })
    problems = validate(records, taxonomy)
    if problems:
        raise SystemExit("Labels not in the taxonomy — fix them in the annotation tool or rebuild "
                         "the taxonomy:\n  " + "\n  ".join(problems))
    return records, short


# --- Label space --------------------------------------------------------------------


def build_label_space(records: list[dict], taxonomy: dict[str, Any], *, min_l1: int = MIN_L1_PAGES,
                      min_l2: int = MIN_L2_PAGES, min_l3: int = MIN_L3_PAGES,
                      min_specialty: int = MIN_SPECIALTY_PAGES, other_l1: str = OTHER_L1,
                      specialty_unknown: str = "train") -> dict[str, Any]:
    """Every class list and mask, as plain JSON. Saved beside the model, so
    inference needs neither the taxonomy nor the data."""
    rules = taxonomy["rules"]
    section, fallback = rules["section_L1"], rules["fallback"]
    l1_order = [x["name"] for x in taxonomy["L1"]]
    l1_count = Counter(r["L1"] for r in records)
    kept = [a for a in l1_order if l1_count[a] >= min_l1]
    fold = {a: other_l1 for a in l1_order if a not in kept}
    l1_classes = kept + ([other_l1] if any(l1_count[a] for a in fold) else [])
    l1_with_l2 = [a for a in l1_order if taxonomy["L2"].get(a)]

    pair_count = Counter(f"{r['L1']}{SEP}{r['L2']}" for r in records
                         if r["L1"] in kept and r["L1"] != section and r["L1"] in l1_with_l2 and r["L2"])
    pair_order = [f"{a}{SEP}{b['name']}" for a in l1_order for b in taxonomy["L2"].get(a, [])]
    l2_classes = [p for p in pair_order if pair_count[p] >= min_l2]
    l2_by_l1 = {a: [p for p in l2_classes if p.split(SEP)[0] == a] for a in l1_classes}

    l3_parents = list(taxonomy["L3"])
    l3_count = Counter(f"{r['L1']}{SEP}{r['L2']}{SEP}{r['L3']}" for r in records
                       if f"{r['L1']}{SEP}{r['L2']}" in l2_classes
                       and f"{r['L1']}{SEP}{r['L2']}" in l3_parents and r["L3"])
    l3_fold = {t: t.rsplit(SEP, 1)[0] + SEP + fallback for t, n in l3_count.items()
               if n < min_l3 and not t.endswith(SEP + fallback)}
    folded_count = Counter()
    for t, n in l3_count.items():
        folded_count[l3_fold.get(t, t)] += n
    l3_order = [f"{parent}{SEP}{c['name']}" for parent in l3_parents for c in taxonomy["L3"][parent]]
    l3_classes = [t for t in l3_order if folded_count[t] > 0]
    l3_by_l2 = {p: [t for t in l3_classes if t.rsplit(SEP, 1)[0] == p] for p in l2_classes if p in l3_parents}

    spec_count = Counter(r["specialty"] for r in records if r["specialty"])
    specialty = [x["name"] for x in taxonomy["specialty"] if spec_count[x["name"]] >= min_specialty
                 and not (specialty_unknown == "ignore" and x["name"] == UNKNOWN_SPECIALTY)]

    return {
        "taxonomy_version": taxonomy.get("version", ""),
        "section_L1": section,
        "fallback": fallback,
        "other_L1": other_l1,
        "L1": l1_classes,
        "L1_fold": fold,
        "L1_with_L2": l1_with_l2,
        "L2": l2_classes,
        "L2_by_L1": l2_by_l1,
        "L3": l3_classes,
        "L3_fold": l3_fold,
        "L3_parents": l3_parents,
        "L3_by_L2": l3_by_l2,
        "specialty": specialty,
        "codeability": list(rules["codeability_values"]),
    }


def encode_targets(record: dict, space: dict[str, Any]) -> tuple[dict[str, int], dict[str, str]]:
    """Class index per head (IGNORE when the row does not train that head), and
    why a head was left out."""
    index = {h: {name: i for i, name in enumerate(space[h])} for h in HEADS}
    targets, why = {}, {}
    l1 = space["L1_fold"].get(record["L1"], record["L1"])
    targets["L1"] = index["L1"].get(l1, IGNORE)
    pair = f"{record['L1']}{SEP}{record['L2']}"
    if record["L1"] == space["section_L1"]:
        why["L2"] = "section L1 (L2 is the content section)"
    elif l1 == space["other_L1"]:
        why["L2"] = "L1 folded into " + space["other_L1"]
    elif record["L1"] not in space["L1_with_L2"]:
        why["L2"] = "L1 has no L2"
    elif not record["L2"]:
        why["L2"] = "blank L2"
    elif pair not in index["L2"]:
        why["L2"] = "rare L2"
    targets["L2"] = IGNORE if "L2" in why else index["L2"][pair]

    if pair not in space["L3_parents"]:
        why["L3"] = "L2 has no named L3"
    elif targets["L2"] == IGNORE:
        why["L3"] = "no L2 target"
    elif not record["L3"]:
        why["L3"] = "blank L3 under a parent with named L3s"
    else:
        triple = f"{pair}{SEP}{record['L3']}"
        triple = space["L3_fold"].get(triple, triple)
        if triple not in index["L3"]:
            why["L3"] = "rare L3"
    targets["L3"] = IGNORE if "L3" in why else index["L3"][triple]

    targets["specialty"] = index["specialty"].get(record["specialty"], IGNORE)
    if targets["specialty"] == IGNORE:
        why["specialty"] = ("Unknown specialty" if record["specialty"] == UNKNOWN_SPECIALTY
                            else "rare specialty" if record["specialty"] else "blank specialty")
    targets["codeability"] = index["codeability"].get(record["codeability"], IGNORE)
    return targets, why


def mask_rows(space: dict[str, Any]) -> tuple[list[list[bool]], list[list[bool]]]:
    """Allowed L2 per L1 class, allowed L3 per L2 class."""
    l2 = [[p in space["L2_by_L1"].get(a, []) for p in space["L2"]] for a in space["L1"]]
    l3 = [[t in space["L3_by_L2"].get(p, []) for t in space["L3"]] for p in space["L2"]]
    return l2, l3


# --- Paths, metrics and routing --------------------------------------------------------


def path_nodes(l1: str, l2: str | None, l3: str | None, space: dict[str, Any]) -> list[str]:
    """Path prefixes the model is accountable for. The section L1 and the folded
    L1 stop at L1 (their L2 is not the model's); L3 only under an L3 parent."""
    nodes = [l1]
    if not l1 or l1 in (space["section_L1"], space["other_L1"]) or l1 not in space["L1_with_L2"] or not l2:
        return nodes
    nodes.append(f"{l1}{SEP}{l2}")
    if nodes[-1] in space["L3_parents"] and l3:
        nodes.append(f"{nodes[-1]}{SEP}{l3}")
    return nodes


def gold_nodes(record: dict, space: dict[str, Any]) -> list[str]:
    l1 = space["L1_fold"].get(record["L1"], record["L1"])
    triple = f"{record['L1']}{SEP}{record['L2']}{SEP}{record['L3']}"
    l3 = space["L3_fold"].get(triple, triple).rsplit(SEP, 1)[-1] if record["L3"] else ""
    return path_nodes(l1, record["L2"], l3, space)


def hierarchical_pr(gold: list[list[str]], pred: list[list[str]]) -> dict[str, float]:
    hit = sum(len(set(g) & set(p)) for g, p in zip(gold, pred))
    n_pred, n_gold = sum(len(p) for p in pred), sum(len(g) for g in gold)
    precision = hit / n_pred if n_pred else 0.0
    recall = hit / n_gold if n_gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1,
            "full_path_accuracy": sum(g == p for g, p in zip(gold, pred)) / len(gold) if gold else 0.0}


def class_scores(truth: list[int], preds: list[int], k: int) -> dict[str, Any]:
    """Per-class precision / recall / F1 / support. Macro F1 is over the classes
    that occur in the truth or the predictions, so a class absent from a small
    split does not count as a zero."""
    tp, fp, fn = Counter(), Counter(), Counter()
    for t, p in zip(truth, preds):
        if t == p:
            tp[t] += 1
        else:
            fp[p] += 1
            fn[t] += 1
    precision, recall, f1, support = [], [], [], []
    for c in range(k):
        pr = tp[c] / (tp[c] + fp[c]) if tp[c] + fp[c] else 0.0
        rc = tp[c] / (tp[c] + fn[c]) if tp[c] + fn[c] else 0.0
        precision.append(pr)
        recall.append(rc)
        f1.append(2 * pr * rc / (pr + rc) if pr + rc else 0.0)
        support.append(tp[c] + fn[c])
    seen = [c for c in range(k) if support[c] or fp[c]]
    confusion = [[0] * k for _ in range(k)]
    for t, p in zip(truth, preds):
        confusion[t][p] += 1
    return {
        "macro_f1": sum(f1[c] for c in seen) / len(seen) if seen else 0.0,
        "accuracy": sum(t == p for t, p in zip(truth, preds)) / len(truth) if truth else 0.0,
        "n": len(truth), "precision": precision, "recall": recall, "f1": f1, "support": support,
        "confusion": confusion,
    }


def route(top: list[tuple[str, float]], other_l1: str, model_at: float = ROUTE_MODEL,
          canon_at: float = ROUTE_CANON, margin: float = ROUTE_MARGIN) -> str:
    """Where a page goes: 'model' (trust the prediction), 'canon' (let the
    keyword canon decide), or 'review'. ``top`` is the L1 ranking, best first."""
    name, best = top[0]
    second = top[1][1] if len(top) > 1 else 0.0
    if name != other_l1 and best >= model_at and best - second >= margin:
        return "model"
    if name == other_l1 or best >= canon_at:
        return "canon"
    return "review"


# --- Split -------------------------------------------------------------------------------


def chart_group(chart_id: Any) -> str | None:
    """The leading number of a chart id. ``53688890`` and ``53688890_240126_2000``
    are one chart."""
    text = str(chart_id or "").strip()
    if not text:
        return None
    match = re.match(r"\d+", text)
    return match.group(0) if match else text


def assign_groups(records: list[dict]) -> list[str]:
    """One split group per record. A chart id groups its pages, which share
    letterhead, banner and footer. A page without one stands alone."""
    groups = []
    for r in records:
        chart = chart_group(r.get("chart_id"))
        groups.append(f"chart:{chart}" if chart else f"page:{r['image']}")
    return groups


def _pick_groups(by_group: dict[str, Counter], pool: list[str], target: dict[str, float]) -> set[str]:
    have = Counter()
    chosen = set()
    for g in pool:
        counts = by_group[g]
        gain = sum(min(n, max(0.0, target[c] - have[c])) for c, n in counts.items())
        overshoot = sum(max(0.0, have[c] + n - target[c]) for c, n in counts.items())
        if gain > 0 and gain >= overshoot:
            chosen.add(g)
            have.update(counts)
    return chosen


def group_split(labels: list[str], groups: list[str], val_fraction: float, test_fraction: float,
                seed: int) -> list[str]:
    """'train' / 'val' / 'test' per record. A group never sits on two sides;
    classes are balanced as far as whole groups allow, test first, then val;
    every class keeps at least one training page."""
    by_group: dict[str, Counter] = {}
    for label, g in zip(labels, groups):
        by_group.setdefault(g, Counter())[label] += 1
    totals = Counter(labels)
    order = list(by_group)
    random.Random(seed).shuffle(order)
    order.sort(key=lambda g: -sum(by_group[g].values()))  # big groups are the hard ones to place
    test = _pick_groups(by_group, order, {c: test_fraction * n for c, n in totals.items()}) \
        if test_fraction > 0 else set()
    rest = [g for g in order if g not in test]
    val = _pick_groups(by_group, rest, {c: val_fraction * n for c, n in totals.items()}) \
        if val_fraction > 0 else set()
    for c in totals:
        if all(g in test or g in val for label, g in zip(labels, groups) if label == c):
            back = {g for label, g in zip(labels, groups) if label == c}
            test -= back
            val -= back
    return ["test" if g in test else "val" if g in val else "train" for g in groups]


def split_real_pages(records: list[dict], labels: list[str], groups: list[str], val_fraction: float,
                     test_fraction: float, seed: int) -> list[str]:
    """``group_split`` over the real pages; a synthetic page is always 'train'."""
    real = [i for i, r in enumerate(records) if not r.get("synthetic")]
    sides = ["train"] * len(records)
    for i, side in zip(real, group_split([labels[i] for i in real], [groups[i] for i in real],
                                         val_fraction, test_fraction, seed)):
        sides[i] = side
    return sides


def lab_note_confusion(confusion: list[list[int]], l1_classes: list[str]) -> dict[str, Any] | None:
    """Lab Report pages called Progress Note and the reverse, from an L1 confusion
    matrix (rows = truth). None when either class is not trained."""
    if LAB_L1 not in l1_classes or NOTE_L1 not in l1_classes:
        return None
    lab, note = l1_classes.index(LAB_L1), l1_classes.index(NOTE_L1)
    return {"lab_pages": sum(confusion[lab]), "lab_as_note": confusion[lab][note],
            "note_pages": sum(confusion[note]), "note_as_lab": confusion[note][lab]}


def lab_note_line(c: dict[str, Any] | None) -> str:
    if not c:
        return f"{LAB_L1} <-> {NOTE_L1}: not scored (a class is not trained)"
    pct = lambda n, d: f"{100 * n / d:.1f}%" if d else "—"  # noqa: E731
    return (f"{LAB_L1} -> {NOTE_L1}: {c['lab_as_note']} of {c['lab_pages']} ({pct(c['lab_as_note'], c['lab_pages'])}); "
            f"{NOTE_L1} -> {LAB_L1}: {c['note_as_lab']} of {c['note_pages']} ({pct(c['note_as_lab'], c['note_pages'])})")


# --- Tokens --------------------------------------------------------------------------------


def head_tail(ids: list[int], max_length: int, head_fraction: float, cls_id: int | None,
              sep_id: int | None) -> list[int]:
    """Content token ids cut to fit ``max_length`` with the special tokens: the
    first ``head_fraction`` of the budget from the start of the page (letterhead,
    title) and the rest from the end (signature, attestation)."""
    specials = [t for t in (cls_id, sep_id) if t is not None]
    budget = max_length - len(specials)
    if len(ids) > budget:
        head = int(math.floor(head_fraction * budget))
        tail = budget - head
        ids = ids[:head] + (ids[-tail:] if tail else [])
    return ([cls_id] if cls_id is not None else []) + list(ids) + ([sep_id] if sep_id is not None else [])


def encode_texts(tokenizer, texts: list[str], max_length: int, head_fraction: float) -> list[list[int]]:
    cls_id = tokenizer.cls_token_id if tokenizer.cls_token_id is not None else tokenizer.bos_token_id
    sep_id = tokenizer.sep_token_id if tokenizer.sep_token_id is not None else tokenizer.eos_token_id
    raw = tokenizer(texts, add_special_tokens=False, truncation=False, verbose=False)["input_ids"]
    return [head_tail(ids, max_length, head_fraction, cls_id, sep_id) for ids in raw]


# --- Model ------------------------------------------------------------------------------------


def build_model(encoder_source: str, space: dict[str, Any], dropout: float = DROPOUT):
    """Encoder plus one linear head per axis on the first token's hidden state."""
    import torch
    from torch import nn
    from transformers import AutoModel

    class PageAxesModel(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.encoder = AutoModel.from_pretrained(encoder_source)
            config = self.encoder.config
            hidden = getattr(config, "hidden_size", None) or getattr(config, "dim")
            self.dropout = nn.Dropout(dropout)
            self.heads = nn.ModuleDict({h: nn.Linear(hidden, max(1, len(space[h]))) for h in HEADS})
            l2, l3 = mask_rows(space)
            self.register_buffer("l2_mask", _bool_matrix(torch, l2, len(space["L1"]), len(space["L2"])),
                                 persistent=False)
            self.register_buffer("l3_mask", _bool_matrix(torch, l3, len(space["L2"]), len(space["L3"])),
                                 persistent=False)

        def forward(self, input_ids, attention_mask):
            hidden = self.encoder(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state[:, 0]
            hidden = self.dropout(hidden)
            return {h: self.heads[h](hidden).float() for h in HEADS}

    return PageAxesModel()


def _bool_matrix(torch, rows: list[list[bool]], n: int, m: int):
    matrix = torch.zeros((n, m), dtype=torch.bool)
    for i, row in enumerate(rows):
        if m:
            matrix[i] = torch.tensor(row, dtype=torch.bool)
    return matrix


def apply_mask(logits, allowed):
    """Logits outside ``allowed`` (same shape, bool) pushed to MASKED."""
    return logits.masked_fill(~allowed, MASKED)


def masked_l2(logits, model, l1_index):
    if logits["L2"].shape[-1] != model.l2_mask.shape[-1] or model.l2_mask.numel() == 0:
        return logits["L2"]
    return apply_mask(logits["L2"], model.l2_mask[l1_index.clamp(min=0)])


def masked_l3(logits, model, l2_index):
    if logits["L3"].shape[-1] != model.l3_mask.shape[-1] or model.l3_mask.numel() == 0:
        return logits["L3"]
    return apply_mask(logits["L3"], model.l3_mask[l2_index.clamp(min=0)])


def compute_loss(logits, targets, model, loss_fns):
    """Sum of the five cross-entropies. L2 is masked by the gold L1 and L3 by
    the gold L2 (teacher forcing). A head with no target in the batch adds 0."""
    import torch

    heads = {**logits, "L2": masked_l2(logits, model, targets["L1"])}
    if model.l3_mask.shape[0]:
        heads["L3"] = masked_l3(logits, model, targets["L2"])
    total = torch.zeros((), device=targets["L1"].device)
    for h in HEADS:
        if (targets[h] != IGNORE).any():
            total = total + loss_fns[h](heads[h], targets[h])
    return total


def _best_under_every_l1(logits, model, space: dict[str, Any], rows: int) -> list[dict[str, Any]]:
    """Per row, the best L2 (and the best L3 under it) the model gives under each
    L1 that has L2s, so the Final step can re-place a page whose document has
    another L1 without re-running the model."""
    import torch

    out: list[dict[str, Any]] = [{} for _ in range(rows)]
    for j, l1 in enumerate(space["L1"]):
        if not space["L2_by_L1"].get(l1):
            continue
        index = torch.full((rows,), j, dtype=torch.long)
        l2_prob = torch.softmax(masked_l2(logits, model, index), -1)
        l2_idx = l2_prob.argmax(-1)
        l3_prob = torch.softmax(masked_l3(logits, model, l2_idx), -1) if space["L3"] else None
        for i in range(rows):
            pair = space["L2"][int(l2_idx[i])]
            best = {"L2": pair.split(SEP, 1)[1], "L2_confidence": float(l2_prob[i, l2_idx[i]]),
                    "L3": None, "L3_confidence": None}
            if l3_prob is not None and space["L3_by_L2"].get(pair):
                k = int(l3_prob[i].argmax())
                best["L3"], best["L3_confidence"] = space["L3"][k].rsplit(SEP, 1)[1], float(l3_prob[i, k])
            out[i][l1] = best
    return out


def decode(logits, model, space: dict[str, Any], top_k: int = 3) -> list[dict[str, Any]]:
    """Per-row predictions: L1 ranking, then L2 masked by the predicted L1 and
    L3 by the predicted L2."""
    import torch

    l1_prob = torch.softmax(logits["L1"], -1)
    l1_idx = l1_prob.argmax(-1)
    l2_prob = torch.softmax(masked_l2(logits, model, l1_idx), -1) if space["L2"] else None
    l2_idx = l2_prob.argmax(-1) if l2_prob is not None else None
    l3_prob = torch.softmax(masked_l3(logits, model, l2_idx), -1) if space["L3"] and l2_idx is not None else None
    spec_prob = torch.softmax(logits["specialty"], -1) if space["specialty"] else None
    code_prob = torch.softmax(logits["codeability"], -1)
    by_l1 = _best_under_every_l1(logits, model, space, l1_prob.shape[0]) if l2_prob is not None else []
    l2_given, l3_given = path_probabilities(logits, model, space)
    out = []
    for i in range(l1_prob.shape[0]):
        values, indices = torch.topk(l1_prob[i], k=min(top_k, len(space["L1"])))
        top = [(space["L1"][j], float(v)) for v, j in zip(values.tolist(), indices.tolist())]
        l1 = top[0][0]
        row = {"L1": l1, "L1_confidence": top[0][1], "L1_top": top,
               "L2": None, "L2_confidence": None, "L3": None, "L3_confidence": None}
        if l2_prob is not None and space["L2_by_L1"].get(l1):
            pair = space["L2"][int(l2_idx[i])]
            row["L2"], row["L2_confidence"] = pair.split(SEP, 1)[1], float(l2_prob[i, l2_idx[i]])
            if l3_prob is not None and space["L3_by_L2"].get(pair):
                j = int(l3_prob[i].argmax())
                row["L3"], row["L3_confidence"] = space["L3"][j].rsplit(SEP, 1)[1], float(l3_prob[i, j])
        row["by_L1"] = by_l1[i] if by_l1 else {}
        if spec_prob is not None:
            values, indices = torch.topk(spec_prob[i], k=min(top_k, len(space["specialty"])))
            row["specialty_top"] = [(space["specialty"][j], float(v))
                                    for v, j in zip(values.tolist(), indices.tolist())]
            row["specialty"], row["specialty_confidence"] = row["specialty_top"][0]
        else:
            row["specialty"], row["specialty_confidence"], row["specialty_top"] = None, None, []
        j = int(code_prob[i].argmax())
        row["codeability"], row["codeability_confidence"] = space["codeability"][j], float(code_prob[i, j])
        row["path"] = " > ".join(x for x in (row["L1"], row["L2"], row["L3"]) if x)
        row["paths"] = top_paths(l1_prob[i].tolist(), None if l2_given is None else l2_given[i].tolist(),
                                 None if l3_given is None else l3_given[i].tolist(), space)
        out.append(row)
    return out


def path_probabilities(logits, model, space: dict[str, Any]):
    """(L2 under every L1, L3 under every L2) as probabilities: rows × L1 × L2 and
    rows × L2 × L3, each row masked to its parent's children. None for a level the
    model does not have."""
    import torch

    l2 = l3 = None
    if space["L2"] and model.l2_mask.numel() and logits["L2"].shape[-1] == model.l2_mask.shape[-1]:
        l2 = torch.softmax(logits["L2"].unsqueeze(1).masked_fill(~model.l2_mask.unsqueeze(0), MASKED), -1)
    if space["L3"] and model.l3_mask.numel() and logits["L3"].shape[-1] == model.l3_mask.shape[-1]:
        l3 = torch.softmax(logits["L3"].unsqueeze(1).masked_fill(~model.l3_mask.unsqueeze(0), MASKED), -1)
    return l2, l3


def top_paths(l1_prob: list[float], l2_given: list[list[float]] | None, l3_given: list[list[float]] | None,
              space: dict[str, Any], k: int = TOP_PATHS, per_l1: int = PATHS_PER_L1) -> list[dict[str, Any]]:
    """The k best whole paths L1 > L2 > L3 over every path the taxonomy allows, at
    most ``per_l1`` under one L1, best first. Score = p(L1) · p(L2 | L1) · p(L3 | L2);
    a level the parent does not have counts as 1. Paths may share an L1 (Lab Report >
    Pathology Report and Lab Report > Lab Result). ``l2_given[a][p]`` is
    p(L2 pair p | L1 a); ``l3_given[p][t]`` is p(L3 t | L2 pair p)."""
    l2_index = {pair: i for i, pair in enumerate(space["L2"])}
    l3_index = {name: i for i, name in enumerate(space["L3"])}
    paths = []

    def add(l1, p1, l2=None, p2=None, l3=None, p3=None):
        score = p1 * (1.0 if p2 is None else p2) * (1.0 if p3 is None else p3)
        paths.append({"path": " > ".join(x for x in (l1, l2, l3) if x), "L1": l1, "L2": l2, "L3": l3,
                      "L1_confidence": p1, "L2_confidence": p2, "L3_confidence": p3, "score": score})

    for a, l1 in enumerate(space["L1"]):
        p1 = float(l1_prob[a])
        pairs = space["L2_by_L1"].get(l1) if l2_given is not None else None
        if not pairs:
            add(l1, p1)
            continue
        for pair in pairs:
            p = l2_index[pair]
            p2 = float(l2_given[a][p])
            l3s = space["L3_by_L2"].get(pair) if l3_given is not None else None
            if not l3s:
                add(l1, p1, pair.split(SEP, 1)[1], p2)
                continue
            for name in l3s:
                add(l1, p1, pair.split(SEP, 1)[1], p2, name.rsplit(SEP, 1)[1], float(l3_given[p][l3_index[name]]))
    kept, per = [], {}
    for found in sorted(paths, key=lambda x: -x["score"]):
        if per.get(found["L1"], 0) < per_l1:
            per[found["L1"]] = per.get(found["L1"], 0) + 1
            kept.append(found)
    return kept[:k]


def save_model(output: Path, model, tokenizer, space: dict[str, Any], settings: dict[str, Any]) -> None:
    import torch

    output.mkdir(parents=True, exist_ok=True)
    model.encoder.save_pretrained(output / "encoder")
    tokenizer.save_pretrained(output / "encoder")
    tmp = output / "heads.pt.tmp"
    torch.save(model.heads.state_dict(), tmp)
    tmp.replace(output / "heads.pt")
    write_json(output / "label_space.json", {**space, **settings})


def load_model(folder: Path, device=None):
    """(model, tokenizer, label space) from a saved page-axes folder."""
    import torch
    from transformers import AutoTokenizer

    space = json.loads((folder / "label_space.json").read_text(encoding="utf-8"))
    model = build_model(str(folder / "encoder"), space, space.get("dropout", DROPOUT))
    model.heads.load_state_dict(torch.load(folder / "heads.pt", map_location="cpu", weights_only=True))
    model.to(device or "cpu").eval()
    return model, AutoTokenizer.from_pretrained(folder / "encoder"), space


# --- Writing ------------------------------------------------------------------------------------


def write_csv(path: Path, header: list[str], rows: Iterable[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    tmp.replace(path)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


# --- Torch helpers ------------------------------------------------------------------------------


def pick_device(choice: str):
    import torch

    if choice == "cuda" and not torch.cuda.is_available():
        raise SystemExit("--device cuda was asked for, but PyTorch sees no CUDA GPU")
    if choice == "cpu":
        return torch.device("cpu")
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def seed_everything(seed: int) -> None:
    import numpy as np
    import torch

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def make_loader(ids: list[list[int]], targets: list[dict[str, int]], pad_id: int, batch_size: int,
                shuffle: bool, seed: int):
    import torch
    from torch.utils.data import DataLoader

    items = list(range(len(ids)))

    def collate(batch):
        width = max(len(ids[i]) for i in batch)
        return {
            "index": torch.tensor(batch),
            "input_ids": torch.tensor([ids[i] + [pad_id] * (width - len(ids[i])) for i in batch]),
            "attention_mask": torch.tensor([[1] * len(ids[i]) + [0] * (width - len(ids[i])) for i in batch]),
            **{h: torch.tensor([targets[i][h] for i in batch]) for h in HEADS},
        }

    return DataLoader(items, batch_size=batch_size, shuffle=shuffle, collate_fn=collate, num_workers=0,
                      generator=torch.Generator().manual_seed(seed))


def class_weights(targets: list[dict[str, int]], head: str, k: int):
    import torch

    counts = Counter(t[head] for t in targets if t[head] != IGNORE)
    n = sum(counts.values())
    return torch.tensor([n / (max(1, k) * max(1, counts[c])) for c in range(max(1, k))], dtype=torch.float)


def run_eval(model, loader, space, device) -> tuple[list[dict], dict[str, list[int]]]:
    """Chained predictions per row, plus each head's argmax given the gold
    parent (L2 masked by the gold L1, L3 by the gold L2) for per-head scores."""
    import torch

    model.eval()
    rows, teacher = [], defaultdict(list)
    with torch.no_grad():
        for batch in loader:
            batch = {k: v.to(device) for k, v in batch.items()}
            logits = model(batch["input_ids"], batch["attention_mask"])
            for row, index in zip(decode(logits, model, space), batch["index"].tolist()):
                row["index"] = index
                rows.append(row)
            forced = {**logits, "L2": masked_l2(logits, model, batch["L1"])}
            if model.l3_mask.shape[0]:
                forced["L3"] = masked_l3(logits, model, batch["L2"])
            for h in HEADS:
                teacher[h].extend(forced[h].argmax(-1).tolist())
    return rows, teacher


def head_scores(targets: list[dict[str, int]], teacher: dict[str, list[int]], space) -> dict[str, Any]:
    out = {}
    for h in HEADS:
        pairs = [(t[h], p) for t, p in zip(targets, teacher[h]) if t[h] != IGNORE]
        out[h] = class_scores([a for a, _ in pairs], [b for _, b in pairs], len(space[h]))
    return out


# --- Main ------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    import torch
    from transformers import AutoTokenizer, get_linear_schedule_with_warmup
    from transformers.utils import logging as hf_logging

    hf_logging.set_verbosity_error()
    output = args.output.resolve()
    device = pick_device(args.device)
    import transformers

    environment = {"python": platform.python_version(), "torch": torch.__version__,
                   "transformers": transformers.__version__, "device": str(device)}
    if device.type == "cuda":
        environment["gpu"] = torch.cuda.get_device_name(device)
    print("Environment: " + ", ".join(f"{k} {v}" for k, v in environment.items()), flush=True)

    taxonomy = load_taxonomy(args.taxonomy)
    records, short = load_records(args.data, taxonomy, args.min_chars)
    if args.with_mock is not None:
        mock, mock_short = load_records(args.with_mock, taxonomy, args.min_chars)
        for r in mock:
            r["synthetic"] = True
        records += mock
        short += mock_short
    synthetic = sum(1 for r in records if r["synthetic"])
    print(f"Pages: {len(records)} usable ({synthetic} synthetic, training split only), {short} left out "
          f"with fewer than {args.min_chars} characters")
    print(f"Taxonomy {taxonomy.get('version', '?')}: {len(taxonomy['L1'])} L1 in the taxonomy")
    space = build_label_space(records, taxonomy, min_l1=args.min_l1_pages, min_l2=args.min_l2_pages,
                              min_l3=args.min_l3_pages, min_specialty=args.min_specialty_pages,
                              other_l1=args.other_l1, specialty_unknown=args.specialty_unknown)
    encoded = [encode_targets(r, space) for r in records]
    targets = [t for t, _ in encoded]
    excluded = Counter((h, why) for _, reasons in encoded for h, why in reasons.items())
    folded = sorted(a for a in space["L1_fold"] if any(r["L1"] == a for r in records))
    print(f"Heads: L1 {len(space['L1'])} classes ({len(folded)} folded into {args.other_l1}: "
          f"{', '.join(folded) or 'none'}), L2 {len(space['L2'])}, L3 {len(space['L3'])}, "
          f"specialty {len(space['specialty'])}, codeability {len(space['codeability'])}")
    for h in HEADS:
        trained = sum(1 for t in targets if t[h] != IGNORE)
        reasons = "; ".join(f"{why}: {n}" for (head, why), n in sorted(excluded.items()) if head == h)
        print(f"  {h:<12} {trained} pages train this head" + (f"  (left out — {reasons})" if reasons else ""))

    groups = assign_groups(records)
    l1_labels = [space["L1"][t["L1"]] for t in targets]
    sides = split_real_pages(records, l1_labels, groups, args.val_fraction, args.test_fraction, args.seed)
    split = {s: [i for i, side in enumerate(sides) if side == s] for s in ("train", "val", "test")}
    print("Split by group: " + ", ".join(
        f"{s} {len(ix)} pages / {len({groups[i] for i in ix})} groups" for s, ix in split.items()))
    print(f"  groups: {sum(1 for g in set(groups) if g.startswith('chart:'))} charts, "
          f"{sum(1 for g in set(groups) if g.startswith('page:'))} stand-alone pages")
    if not split["val"]:
        raise SystemExit("No validation pages: lower --min-l1-pages or raise --val-fraction")

    seed_everything(args.seed)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    ids = encode_texts(tokenizer, [r["text"] for r in records], args.max_length, args.head_fraction)
    model = build_model(args.base_model, space, args.dropout).to(device)
    pad = tokenizer.pad_token_id or 0

    def loader(side: str, shuffle: bool):
        return make_loader([ids[i] for i in split[side]], [targets[i] for i in split[side]], pad,
                           args.batch_size, shuffle, args.seed)

    train_loader = loader("train", True)
    val_loader = loader("val", False)
    train_targets = [targets[i] for i in split["train"]]
    loss_fns = {h: torch.nn.CrossEntropyLoss(weight=class_weights(train_targets, h, len(space[h])).to(device),
                                             ignore_index=IGNORE) for h in HEADS}
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    total_steps = max(1, len(train_loader) * args.epochs)
    scheduler = get_linear_schedule_with_warmup(optimizer, max(1, int(WARMUP_FRACTION * total_steps)),
                                                total_steps)
    use_amp = device.type == "cuda"
    scaler = torch.amp.GradScaler("cuda") if use_amp else None

    def train_step(batch) -> float:
        model.train()
        batch = {k: v.to(device) for k, v in batch.items()}
        optimizer.zero_grad(set_to_none=True)
        with torch.autocast(device_type="cuda", enabled=use_amp):
            logits = model(batch["input_ids"], batch["attention_mask"])
        loss = compute_loss(logits, batch, model, loss_fns)
        if scaler:
            scaler.scale(loss).backward()
            scale = scaler.get_scale()
            scaler.step(optimizer)
            scaler.update()
            stepped = scaler.get_scale() >= scale  # the scaler skips a step while it calibrates
        else:
            loss.backward()
            optimizer.step()
            stepped = True
        if stepped:
            scheduler.step()
        return float(loss.item())

    def val_loss() -> float:
        model.eval()
        losses = []
        with torch.no_grad():
            for batch in val_loader:
                batch = {k: v.to(device) for k, v in batch.items()}
                losses.append(float(compute_loss(model(batch["input_ids"], batch["attention_mask"]),
                                                 batch, model, loss_fns).item()))
        return sum(losses) / max(1, len(losses))

    if args.check:
        batches = iter(train_loader)
        train_step(next(batches))  # warm-up, not timed
        start = time.perf_counter()
        train_step(next(batches, None) or next(iter(train_loader)))
        if device.type == "cuda":
            torch.cuda.synchronize()
        per_step = time.perf_counter() - start
        minutes = per_step * (len(train_loader) + len(val_loader) / 3) / 60
        print(f"Check passed: 2 training steps on {device}, batch size {args.batch_size}, max length "
              f"{args.max_length}. About {per_step:.2f} s per step, {len(train_loader)} steps per epoch: "
              f"roughly {minutes:.1f} minutes per epoch. Nothing saved.")
        return 0

    select = [h.strip() for h in args.select_heads.split(",") if h.strip()]
    unknown = [h for h in select if h not in HEADS]
    if unknown:
        raise SystemExit(f"--select-heads: unknown head(s) {unknown}; choose from {list(HEADS)}")
    settings = {k: getattr(args, k) for k in ("base_model", "max_length", "head_fraction", "dropout",
                                              "route_model", "route_canon", "specialty_unknown")}
    settings["route_margin"] = ROUTE_MARGIN
    # Training pages per class: the pipeline asserts a class only when it was
    # trained on enough pages (page_arbitration.json → axes).
    settings["train_pages"] = {
        h: {space[h][c]: n for c, n in sorted(Counter(t[h] for t in train_targets if t[h] != IGNORE).items())}
        for h in HEADS
    }
    config = {
        "data": str(args.data.resolve()), "output": str(output), "environment": environment,
        "settings": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
        "pages": {"usable": len(records), "left_out_short": short,
                  **{s: len(ix) for s, ix in split.items()}},
        "excluded_from_head": {f"{h}: {why}": n for (h, why), n in sorted(excluded.items())},
    }
    write_csv(output / "split.csv", ["image", "group", "split"],
              [[r["image"], g, s] for r, g, s in zip(records, groups, sides)])

    log, best, best_epoch = [], -1.0, 0
    for epoch in range(1, args.epochs + 1):
        started = time.perf_counter()
        losses = []
        for step, batch in enumerate(train_loader, 1):
            losses.append(train_step(batch))
            if args.log_every > 0 and (step % args.log_every == 0 or step == len(train_loader)):
                recent = losses[-args.log_every:]
                print(f"  epoch {epoch} step {step}/{len(train_loader)}: loss {sum(recent) / len(recent):.4f} "
                      f"(last {len(recent)}), lr {scheduler.get_last_lr()[0]:.2e}, "
                      f"{time.perf_counter() - started:.0f}s", flush=True)
        validation_loss = val_loss()
        _, teacher = run_eval(model, val_loader, space, device)
        scores = head_scores([targets[i] for i in split["val"]], teacher, space)
        selected = sum(scores[h]["macro_f1"] for h in select) / len(select)
        row = {"epoch": epoch, "train_loss": round(sum(losses) / max(1, len(losses)), 4),
               "val_loss": round(validation_loss, 4),
               **{f"val_{h}_macro_f1": round(scores[h]["macro_f1"], 4) for h in HEADS},
               "val_selected": round(selected, 4), "seconds": round(time.perf_counter() - started, 1)}
        log.append(row)
        improved = selected > best
        print(f"Epoch {epoch}: loss {row['train_loss']}, val loss {row['val_loss']}, val macro F1 " + ", ".join(
            f"{h} {scores[h]['macro_f1']:.3f}" for h in HEADS) + f"; selected {selected:.3f}"
            + ("  (best so far, saved)" if improved else ""), flush=True)
        if improved:
            best, best_epoch = selected, epoch
            save_model(output, model, tokenizer, space, settings)
        write_csv(output / "training_log.csv", list(row), [list(r.values()) for r in log])
        write_json(output / "run_config.json", {**config, "epochs_run": epoch, "best_epoch": best_epoch,
                                                "best_val_selected_macro_f1": best})

    # Score the best epoch, loaded back from disk (which also proves the folder works).
    saved, _, saved_space = load_model(output, device)
    report = evaluate_saved(saved, saved_space, records, targets, groups, sides, ids, pad, args, device)
    write_outputs(output, report, saved_space, records, targets, split)
    summary = report["summary"]
    summary.update({"best_epoch": best_epoch, "selected_heads": select})
    write_json(output / "metrics_summary.json", summary)
    side = summary["scored_on"]
    print(f"Best epoch {best_epoch}, scored on {side} ({summary['pages']} pages):")
    for h in HEADS:
        print(f"  {h:<12} macro F1 {summary['macro_f1'][h]:.3f}  ({summary['head_pages'][h]} pages)")
    print(f"  full-path accuracy {summary['full_path_accuracy']:.3f}, hierarchical P {summary['hierarchical_precision']:.3f}"
          f" R {summary['hierarchical_recall']:.3f}  ({summary['path_note']})")
    print(f"  routes: {summary['routes']}; codeability model≠rule on {summary['codeability_disagreements']} pages")
    print("  per-class recall (L1): " + ", ".join(
        f"{name} {report['scores']['L1']['recall'][c]:.2f} ({report['scores']['L1']['support'][c]})"
        for c, name in enumerate(saved_space["L1"])))
    print(f"  {lab_note_line(summary['lab_note_confusion'])}  (on {side})")
    print(f"Saved: {output}")
    return 0


def evaluate_saved(model, space, records, targets, groups, sides, ids, pad, args, device) -> dict[str, Any]:
    """Predictions for val and test, and the summary on test (else val)."""
    per_side, teacher_by_side = {}, {}
    for side in ("val", "test"):
        ix = [i for i, s in enumerate(sides) if s == side]
        if not ix:
            continue
        loader = make_loader([ids[i] for i in ix], [targets[i] for i in ix], pad, args.batch_size, False, args.seed)
        rows, teacher = run_eval(model, loader, space, device)
        for row in rows:
            i = ix[row.pop("index")]
            row.update({"record": i, "split": side, "group": groups[i],
                        "route": route(row["L1_top"], space["other_L1"], args.route_model, args.route_canon)})
        per_side[side] = rows
        teacher_by_side[side] = (ix, teacher)
    side = "test" if "test" in per_side else "val"
    ix, teacher = teacher_by_side[side]
    scores = head_scores([targets[i] for i in ix], teacher, space)
    rows = per_side[side]
    gold = [gold_nodes(records[r["record"]], space) for r in rows]
    pred = [path_nodes(r["L1"], r["L2"], r["L3"], space) for r in rows]
    hier = hierarchical_pr(gold, pred)
    summary = {
        "scored_on": side, "pages": len(rows),
        "macro_f1": {h: round(scores[h]["macro_f1"], 4) for h in HEADS},
        "accuracy": {h: round(scores[h]["accuracy"], 4) for h in HEADS},
        "head_pages": {h: scores[h]["n"] for h in HEADS},
        "full_path_accuracy": round(hier["full_path_accuracy"], 4),
        "hierarchical_precision": round(hier["precision"], 4),
        "hierarchical_recall": round(hier["recall"], 4),
        "hierarchical_f1": round(hier["f1"], 4),
        "path_note": f"{space['section_L1']} and {space['other_L1']} are scored on L1 only (their L2 is not "
                     "the model's); L3 only under L2s with named L3s. Per-head L2/L3 scores assume the "
                     "correct parent; the path scores use the predicted chain.",
        "routes": dict(Counter(r["route"] for r in rows)),
        "codeability_disagreements": sum(1 for r in rows
                                         if r["codeability"] != records[r["record"]]["codeability"]),
        "lab_note_confusion": lab_note_confusion(scores["L1"]["confusion"], space["L1"]),
    }
    return {"summary": summary, "scores": scores, "rows": per_side, "gold": gold, "pred": pred,
            "side": side, "teacher": teacher, "ix": ix}


def write_outputs(output: Path, report, space, records, targets, split) -> None:
    scores = report["scores"]
    train_counts = {h: Counter(targets[i][h] for i in split["train"]) for h in HEADS}
    write_csv(output / "category_wise_metrics.csv",
              ["head", "class", "precision", "recall", "f1", "support", "train_pages"],
              [[h, name, round(scores[h]["precision"][c], 4), round(scores[h]["recall"][c], 4),
                round(scores[h]["f1"][c], 4), scores[h]["support"][c], train_counts[h][c]]
               for h in HEADS for c, name in enumerate(space[h])])
    write_csv(output / "confusion" / "L1.csv", ["true \\ predicted", *space["L1"]],
              [[name, *row] for name, row in zip(space["L1"], scores["L1"]["confusion"])])
    # Within each L1: L2 truth against L2 predicted under the correct L1.
    ix, teacher = report["ix"], report["teacher"]
    for l1, pairs in space["L2_by_L1"].items():
        if not pairs:
            continue
        position = {space["L2"].index(p): n for n, p in enumerate(pairs)}
        names = [p.split(SEP, 1)[1] for p in pairs]
        matrix = [[0] * len(pairs) for _ in pairs]
        for i, p in zip(ix, teacher["L2"]):
            t = targets[i]["L2"]
            if t in position and p in position:
                matrix[position[t]][position[p]] += 1
        write_csv(output / "confusion" / f"L2_{slug(l1)}.csv", ["true \\ predicted", *names],
                  [[n, *row] for n, row in zip(names, matrix)])

    header = ["image", "split", "group", "route", "L1_confidence", "L1_top3", "path_correct"]
    for h in HEADS:
        header += [f"gold_{h}", f"pred_{h}"]
    header += ["L2_confidence", "L3_confidence", "specialty_confidence", "codeability_confidence", "specialty_top3"]
    errors = []
    for side, rows in report["rows"].items():
        lines = []
        for r in rows:
            rec = records[r["record"]]
            g = gold_nodes(rec, space)
            p = path_nodes(r["L1"], r["L2"], r["L3"], space)
            correct = g == p
            gold_l1 = space["L1_fold"].get(rec["L1"], rec["L1"])
            line = [rec["image"], side, r["group"], r["route"], round(r["L1_confidence"], 4),
                    "; ".join(f"{n} {v:.2f}" for n, v in r["L1_top"]), "y" if correct else "n",
                    gold_l1, r["L1"], rec["L2"], r["L2"] or "", rec["L3"], r["L3"] or "",
                    rec["specialty"], r["specialty"] or "", rec["codeability"], r["codeability"],
                    _round(r["L2_confidence"]), _round(r["L3_confidence"]), _round(r["specialty_confidence"]),
                    _round(r["codeability_confidence"]),
                    "; ".join(f"{n} {v:.2f}" for n, v in r["specialty_top"])]
            lines.append(line)
            wrong_spec = targets[r["record"]]["specialty"] != IGNORE and rec["specialty"] != r["specialty"]
            if not correct or wrong_spec or rec["codeability"] != r["codeability"]:
                errors.append(line)
        write_csv(output / f"predictions_{side}.csv", header, lines)
    write_csv(output / "errors.csv", header, errors)


def _round(value):
    return "" if value is None else round(value, 4)


if __name__ == "__main__":
    sys.exit(main())
