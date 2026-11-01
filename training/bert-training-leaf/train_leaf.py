#!/usr/bin/env python3
"""Train the page-leaf model: one encoder, one **leaf** head (a class per whole path
L1 > L2 > L3 of the taxonomy) and a specialty head. Replaces the page-axes cascade
(L1, then L2 under it, then L3) of ``training/bert-training`` (2026-11-01, user).

Input is ``training_data.jsonl`` (one page per line: ``text``, ``L1``, ``L2``, ``L3``,
``specialty``, ``chart_id``), a copy of ``Training/processed/training_data.jsonl``. Names
come from ``taxonomy.json`` beside this file (v29.3). Output, by default
``output/page-leaf``, holds the best epoch by validation leaf macro F1; copy it to
``core-pipeline/models/page-family/page-leaf/`` on each host.

    python train_leaf.py --check          # two steps and a time estimate; saves nothing
    python train_leaf.py                  # a run (20 epochs)

The leaves (all thresholds are options):

* A page's leaf is its whole path. A path that stops above the taxonomy's leaves (an L1
  with L2s but no L2 given, an L2 with L3s but no L3 given) takes that level's
  ``Others`` when the taxonomy has one, else stays the shorter path.
* Old names are renamed first (``RENAMES``: v29.2 -> v29.3).
* A leaf with fewer than ``--min-leaf-pages`` pages (default 3: the workbook's "Too few")
  folds into its parent's ``Others`` (or the parent itself) until every leaf has enough.
  ``label_space.json -> folded`` lists every fold; the pipeline's keyword canon still
  finds those leaves.
* Specialty: values with at least ``--min-specialty-pages`` pages; Unknown is a class.

Small classes: whole charts go to train / val / test spreading every leaf and specialty;
a leaf seen in fewer than ``--min-eval-groups`` charts stays in train. Training pages of
rare leaves repeat (repeat-factor sampling, ``--oversample``), and the loss weights are
the square root of inverse frequency.

At inference (``decode``) the model gives every leaf a probability; ``top_leaves`` keeps
the best ``TOP_K`` (5) above ``MIN_PROB``, at most ``PER_L1`` (3) under one L1, and keeps
any leaf tied with the last one kept. The pipeline's copy is
``core-pipeline/stages/lib/page_classify/leaf_model.py`` (tests/test_leaf.py fails when
the two drift).

Page text is never printed: only counts, class names, shapes and metrics.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
DEFAULT_DATA = HERE / "data" / "training_data.jsonl"
DEFAULT_OUTPUT = HERE / "output" / "page-leaf"
TAXONOMY = HERE / "taxonomy.json"

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

BASE_MODEL = "emilyalsentzer/Bio_ClinicalBERT"
MAX_LENGTH = 512
HEAD_FRACTION = 0.85  # of the token budget kept from the start; the rest from the end
BATCH_SIZE = 8
LOG_EVERY = 10
LEARNING_RATE = 3e-5
WARMUP_FRACTION = 0.1
EPOCHS = 20
SEED = 42
DROPOUT = 0.1
VAL_FRACTION = 0.15
TEST_FRACTION = 0.15
MIN_CHARS = 20
MIN_LEAF_PAGES = 3
MIN_SPECIALTY_PAGES = 3
MIN_EVAL_GROUPS = 3
OVERSAMPLE_PAGES = 30
MAX_REPEAT = 4.0
SPECIALTY_LOSS = 0.5  # the specialty head's share of the loss
UNKNOWN_SPECIALTY = "Unknown"
OTHERS = "Others"
IGNORE = -100
SEP = " > "
HEADS = ("leaf", "specialty")
TOP_K = 5  # leaves kept per page
PER_L1 = 3  # of which at most this many under one L1
MIN_PROB = 0.01  # a leaf below this is never kept
# v29.2 -> v29.3 (training/taxonomy/import_taxonomy_xlsx.py --rename).
RENAMES = {
    "Consent and Authorization > HIPAA and Privacy": "Consent and Authorization > HIPAA Notice & Consent",
    "Problem List > Diagnosis List/Index": "Problem List > Diagnosis List/Coding Summary",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=DEFAULT_DATA)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--taxonomy", type=Path, default=TAXONOMY)
    p.add_argument("--base-model", default=BASE_MODEL)
    p.add_argument("--check", action="store_true", help="two training steps and a time estimate; saves nothing")
    p.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    p.add_argument("--epochs", type=int, default=EPOCHS)
    p.add_argument("--max-length", type=int, default=MAX_LENGTH)
    p.add_argument("--head-fraction", type=float, default=HEAD_FRACTION)
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--log-every", type=int, default=LOG_EVERY)
    p.add_argument("--lr", type=float, default=LEARNING_RATE)
    p.add_argument("--dropout", type=float, default=DROPOUT)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--val-fraction", type=float, default=VAL_FRACTION)
    p.add_argument("--test-fraction", type=float, default=TEST_FRACTION)
    p.add_argument("--min-chars", type=int, default=MIN_CHARS)
    p.add_argument("--min-leaf-pages", type=int, default=MIN_LEAF_PAGES)
    p.add_argument("--min-specialty-pages", type=int, default=MIN_SPECIALTY_PAGES)
    p.add_argument("--min-eval-groups", type=int, default=MIN_EVAL_GROUPS)
    p.add_argument("--oversample", type=int, default=OVERSAMPLE_PAGES)
    p.add_argument("--max-repeat", type=float, default=MAX_REPEAT)
    p.add_argument("--specialty-loss", type=float, default=SPECIALTY_LOSS)
    return p.parse_args(argv)


# --- Taxonomy and labels -------------------------------------------------------------------------


def load_taxonomy(path: Path = TAXONOMY) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit(f"No taxonomy at {path} (copy taxonomy.json beside train_leaf.py)")
    return json.loads(path.read_text(encoding="utf-8"))


def taxonomy_leaves(taxonomy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every leaf path of the taxonomy -> {L1, L2, L3, codeability}: an L1 without L2s,
    an L2 without L3s, every L3."""
    out: dict[str, dict[str, Any]] = {}
    l2s = {l1: [x for x in items] for l1, items in taxonomy["L2"].items()}
    l3s = {parent: [x for x in items] for parent, items in taxonomy["L3"].items()}
    for node in taxonomy["L1"]:
        l1 = node["name"]
        if not l2s.get(l1):
            out[l1] = {"L1": l1, "L2": "", "L3": "", "codeability": ""}
            continue
        for item in l2s[l1]:
            parent = f"{l1}{SEP}{item['name']}"
            if not l3s.get(parent):
                out[parent] = {"L1": l1, "L2": item["name"], "L3": "", "codeability": item.get("codeability", "")}
                continue
            for leaf in l3s[parent]:
                out[f"{parent}{SEP}{leaf['name']}"] = {"L1": l1, "L2": item["name"], "L3": leaf["name"],
                                                       "codeability": leaf.get("codeability", "")}
    return out


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def renamed(path: str) -> str:
    for old, new in RENAMES.items():
        if path == old or path.startswith(old + SEP):
            return new + path[len(old):]
    return path


def leaf_of(l1: str, l2: str, l3: str, leaves: dict[str, Any]) -> str | None:
    """The leaf a labelled path names (None when the taxonomy has no such path). A path
    stopping above the leaves takes that level's Others, else stays as it is."""
    path = renamed(SEP.join(x for x in (l1, l2, l3) if x))
    if path in leaves:
        return path
    if f"{path}{SEP}{OTHERS}" in leaves:
        return f"{path}{SEP}{OTHERS}"
    if any(k.startswith(path + SEP) for k in leaves):
        return path  # an inner node with no Others: kept as its own class
    return None


def parent_of(path: str) -> str:
    return path.rsplit(SEP, 1)[0] if SEP in path else ""


def fold_rare(counts: Counter, leaves: dict[str, Any], min_pages: int) -> dict[str, str]:
    """leaf -> the class it trains as. A leaf under ``min_pages`` pages folds into its
    parent's Others (or the parent) until every class has enough or reaches the L1."""
    target = {leaf: leaf for leaf in counts}
    while True:
        sizes = Counter()
        for leaf, n in counts.items():
            sizes[target[leaf]] += n
        moved = False
        for leaf in list(target):
            cls = target[leaf]
            if sizes[cls] >= min_pages or SEP not in cls:
                continue
            parent = parent_of(cls)
            others = f"{parent}{SEP}{OTHERS}"
            new = others if others in leaves and others != cls else parent
            if new != cls:
                target[leaf] = new
                moved = True
        if not moved:
            return target


def load_records(path: Path, leaves: dict[str, Any], min_chars: int) -> tuple[list[dict], Counter]:
    """Usable pages with their leaf, and the pages left out (why -> count)."""
    if not path.is_file():
        raise SystemExit(f"No training file at {path}")
    records, skipped = [], Counter()
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            text = str(row.get("text") or "")
            if len("".join(text.split())) < min_chars:
                skipped["almost no text"] += 1
                continue
            l1, l2, l3 = (_clean(row.get(k)) for k in ("L1", "L2", "L3"))
            leaf = leaf_of(l1, l2, l3, leaves)
            if leaf is None:
                skipped[f"not in the taxonomy: {renamed(SEP.join(x for x in (l1, l2, l3) if x))}"] += 1
                continue
            records.append({"image": str(row.get("image") or f"line-{number}"), "text": text,
                            "chart_id": _clean(row.get("chart_id")), "synthetic": bool(row.get("synthetic")),
                            "leaf": leaf, "specialty": _clean(row.get("specialty"))})
    return records, skipped


def build_label_space(records: list[dict], taxonomy: dict[str, Any], leaves: dict[str, Any], *,
                      min_leaf: int, min_specialty: int) -> dict[str, Any]:
    counts = Counter(r["leaf"] for r in records)
    target = fold_rare(counts, leaves, min_leaf)
    classes = sorted(set(target.values()))
    paths = {}
    for cls in classes:
        info = leaves.get(cls)
        if info is None:  # an inner node kept as a class
            parts = cls.split(SEP) + ["", ""]
            info = {"L1": parts[0], "L2": parts[1], "L3": parts[2], "codeability": ""}
        paths[cls] = info
    spec_counts = Counter(r["specialty"] or UNKNOWN_SPECIALTY for r in records)
    specialties = sorted(s for s, n in spec_counts.items() if n >= min_specialty)
    return {
        "kind": "leaf",
        "taxonomy_version": taxonomy.get("version"),
        "leaf": classes,
        "paths": paths,
        "folded": {leaf: cls for leaf, cls in sorted(target.items()) if leaf != cls},
        "specialty": specialties,
        "train_pages": {"leaf": {c: sum(n for leaf, n in counts.items() if target[leaf] == c) for c in classes}},
    }


def encode_targets(record: dict, space: dict[str, Any]) -> dict[str, int]:
    cls = space["folded"].get(record["leaf"], record["leaf"])
    specialty = record["specialty"] or UNKNOWN_SPECIALTY
    return {"leaf": space["leaf"].index(cls),
            "specialty": space["specialty"].index(specialty) if specialty in space["specialty"] else IGNORE}


# --- Splits ----------------------------------------------------------------------------------------


def chart_group(chart_id: Any) -> str | None:
    """The leading number of a chart id: ``53688890`` and ``53688890_240126_2000`` are one chart."""
    text = str(chart_id or "").strip()
    if not text:
        return None
    match = re.match(r"\d+", text)
    return match.group(0) if match else text


def assign_groups(records: list[dict]) -> list[str]:
    return [f"chart:{chart_group(r.get('chart_id'))}" if chart_group(r.get("chart_id")) else f"page:{r['image']}"
            for r in records]


def label_split(labels: list[list[str]], groups: list[str], val_fraction: float, test_fraction: float,
                seed: int, min_eval_groups: int = MIN_EVAL_GROUPS) -> list[str]:
    """'train' / 'val' / 'test' per record, whole groups only, spreading every label.
    Groups with the rarest labels are placed first, each on the side that most lacks its
    labels; a group with a label seen in fewer than ``min_eval_groups`` groups trains."""
    by_group: dict[str, Counter] = {}
    for labs, g in zip(labels, groups):
        by_group.setdefault(g, Counter()).update(labs)
    totals = Counter(lab for labs in labels for lab in labs)
    n_groups = Counter(lab for counts in by_group.values() for lab in counts)
    fraction = {"train": 1.0 - val_fraction - test_fraction, "val": val_fraction, "test": test_fraction}
    sides = [s for s in ("train", "test", "val") if fraction[s] > 0]
    have = {s: Counter() for s in sides}
    order = list(by_group)
    random.Random(seed).shuffle(order)
    order.sort(key=lambda g: (min((totals[lab] for lab in by_group[g]), default=0), -sum(by_group[g].values())))
    side_of: dict[str, str] = {}
    for g in order:
        counts = by_group[g]
        if not counts or any(n_groups[lab] < min_eval_groups for lab in counts):
            choice = "train"
        else:
            def need(side: str) -> float:
                return sum((fraction[side] * totals[lab] - have[side][lab] - 0.5 * n) / totals[lab]
                           for lab, n in counts.items())
            choice = max(sides, key=lambda side: (need(side), side == "train"))
        side_of[g] = choice
        have[choice].update(counts)
    return [side_of[g] for g in groups]


def page_labels(target: dict[str, int], space: dict[str, Any]) -> list[str]:
    return [f"{h}:{space[h][target[h]]}" for h in HEADS if target[h] != IGNORE]


def repeat_factors(targets: list[dict[str, int]], space: dict[str, Any], threshold: int,
                   max_repeat: float) -> list[float]:
    """Per training page: max over its classes of sqrt(threshold / pages of that class),
    at least 1, at most ``max_repeat``."""
    if threshold <= 0:
        return [1.0] * len(targets)
    labs = [page_labels(t, space) for t in targets]
    count = Counter(lab for ls in labs for lab in ls)
    return [min(max_repeat, max([1.0] + [math.sqrt(threshold / count[lab]) for lab in ls])) for ls in labs]


# --- The model (copied to core-pipeline/stages/lib/page_classify/leaf_model.py) ------------------


def head_tail(ids: list[int], max_length: int, head_fraction: float, cls_id: int | None,
              sep_id: int | None) -> list[int]:
    """Content token ids cut to fit ``max_length`` with the special tokens: the first
    ``head_fraction`` of the budget from the start of the page (letterhead, title) and the
    rest from the end (signature, attestation)."""
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


def build_model(encoder_source: str, space: dict[str, Any], dropout: float = DROPOUT):
    import torch
    from torch import nn
    from transformers import AutoModel

    class PageLeafModel(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.encoder = AutoModel.from_pretrained(encoder_source)
            hidden = self.encoder.config.hidden_size
            self.dropout = nn.Dropout(dropout)
            self.heads = nn.ModuleDict({
                head: nn.Linear(hidden, len(space[head])) for head in HEADS if space.get(head)
            })

        def forward(self, input_ids, attention_mask):
            out = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
            pooled = self.dropout(out.last_hidden_state[:, 0])
            return {head: layer(pooled) for head, layer in self.heads.items()}

    torch.manual_seed(0)
    return PageLeafModel()


def top_leaves(probs: list[float], space: dict[str, Any], k: int = TOP_K, per_l1: int = PER_L1,
               min_prob: float = MIN_PROB) -> list[dict[str, Any]]:
    """The best leaves, best first: at most ``k`` above ``min_prob`` and at most ``per_l1``
    under one L1, plus any leaf tied with the last one kept."""
    order = sorted(range(len(probs)), key=lambda i: -probs[i])
    kept: list[dict[str, Any]] = []
    per: Counter = Counter()
    for i in order:
        p = float(probs[i])
        if p < min_prob:
            break
        name = space["leaf"][i]
        info = space["paths"][name]
        if len(kept) >= k and not (kept and round(p, 4) == round(kept[-1]["score"], 4)):
            break
        if per[info["L1"]] >= per_l1:
            continue
        per[info["L1"]] += 1
        kept.append({"leaf": name, "L1": info["L1"], "L2": info["L2"] or None, "L3": info["L3"] or None,
                     "score": round(p, 4), "codeability": info.get("codeability") or None})
    return kept


def decode(logits, space: dict[str, Any], k: int = TOP_K, per_l1: int = PER_L1,
           min_prob: float = MIN_PROB) -> list[dict[str, Any]]:
    """Per page: ``leaves`` (top_leaves), ``leaf_probs`` (every leaf), the specialty."""
    import torch

    leaf_prob = torch.softmax(logits["leaf"].float(), dim=-1)
    spec = torch.softmax(logits["specialty"].float(), dim=-1) if "specialty" in logits else None
    rows = []
    for i in range(leaf_prob.shape[0]):
        probs = leaf_prob[i].tolist()
        row: dict[str, Any] = {"leaves": top_leaves(probs, space, k, per_l1, min_prob),
                               "leaf_probs": {name: round(float(p), 4) for name, p in zip(space["leaf"], probs)}}
        if spec is not None:
            values = spec[i].tolist()
            order = sorted(range(len(values)), key=lambda j: -values[j])[:3]
            row["specialty"] = space["specialty"][order[0]]
            row["specialty_confidence"] = round(float(values[order[0]]), 4)
            row["specialty_top"] = [(space["specialty"][j], round(float(values[j]), 4)) for j in order]
        rows.append(row)
    return rows


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
    """(model, tokenizer, label space) from a saved page-leaf folder."""
    import torch
    from transformers import AutoTokenizer

    space = json.loads((folder / "label_space.json").read_text(encoding="utf-8"))
    model = build_model(str(folder / "encoder"), space, space.get("dropout", DROPOUT))
    model.heads.load_state_dict(torch.load(folder / "heads.pt", map_location="cpu", weights_only=True))
    model.to(device or "cpu").eval()
    return model, AutoTokenizer.from_pretrained(folder / "encoder"), space


# --- Writing and scores ----------------------------------------------------------------------------


def write_csv(path: Path, header: list[str], rows: Iterable[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def macro_f1(truth: list[int], preds: list[int], k: int) -> tuple[float, list[tuple[int, int, int, float]]]:
    """Macro F1 over the classes present in the truth, and (support, tp, predicted, f1) per class."""
    tp, pred_n, true_n = Counter(), Counter(preds), Counter(truth)
    for t, p in zip(truth, preds):
        if t == p:
            tp[t] += 1
    per, f1s = [], []
    for c in range(k):
        prec = tp[c] / pred_n[c] if pred_n[c] else 0.0
        rec = tp[c] / true_n[c] if true_n[c] else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        per.append((true_n[c], tp[c], pred_n[c], f1))
        if true_n[c]:
            f1s.append(f1)
    return (sum(f1s) / len(f1s) if f1s else 0.0), per


# --- Training ----------------------------------------------------------------------------------------


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


def batches(ids: list[list[int]], targets: list[dict[str, int]], order: list[int], pad_id: int, size: int):
    import torch

    for start in range(0, len(order), size):
        chunk = order[start:start + size]
        width = max(len(ids[i]) for i in chunk)
        input_ids = torch.full((len(chunk), width), pad_id, dtype=torch.long)
        mask = torch.zeros((len(chunk), width), dtype=torch.long)
        for row, i in enumerate(chunk):
            input_ids[row, :len(ids[i])] = torch.tensor(ids[i])
            mask[row, :len(ids[i])] = 1
        yield input_ids, mask, {h: torch.tensor([targets[i][h] for i in chunk]) for h in HEADS}, chunk


def sampled_order(n: int, repeat: list[float], rng: random.Random) -> list[int]:
    order = []
    for i in range(n):
        whole = int(repeat[i])
        order += [i] * whole + ([i] if rng.random() < repeat[i] - whole else [])
    rng.shuffle(order)
    return order


def class_weights(targets: list[dict[str, int]], head: str, k: int, order: list[int]):
    """sqrt(inverse frequency) over the pages as sampled; 1.0 for an unseen class."""
    import torch

    count = Counter(targets[i][head] for i in order if targets[i][head] != IGNORE)
    total = sum(count.values()) or 1
    return torch.tensor([math.sqrt(total / (k * count[c])) if count[c] else 1.0 for c in range(k)],
                        dtype=torch.float)


def evaluate(model, ids, targets, idx, pad_id, batch_size, device, space) -> dict[str, Any]:
    import torch

    preds, top_hits, l1_hits, rows = [], 0, 0, []
    model.eval()
    with torch.no_grad():
        for input_ids, mask, _t, chunk in batches(ids, targets, idx, pad_id, batch_size):
            logits = model(input_ids.to(device), mask.to(device))
            for i, row in zip(chunk, decode(logits, space)):
                best = max(row["leaf_probs"], key=row["leaf_probs"].get)
                truth = space["leaf"][targets[i]["leaf"]]
                preds.append(space["leaf"].index(best))
                top_hits += any(leaf["leaf"] == truth for leaf in row["leaves"])
                l1_hits += space["paths"][best]["L1"] == space["paths"][truth]["L1"]
                rows.append((truth, best))
    truth_idx = [targets[i]["leaf"] for i in idx]
    f1, per = macro_f1(truth_idx, preds, len(space["leaf"]))
    n = len(idx) or 1
    return {"pages": len(idx), "leaf_macro_f1": round(f1, 4),
            "leaf_accuracy": round(sum(t == p for t, p in zip(truth_idx, preds)) / n, 4),
            "top5_recall": round(top_hits / n, 4), "l1_accuracy": round(l1_hits / n, 4), "per_leaf": per}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    import torch
    from transformers import AutoTokenizer, get_linear_schedule_with_warmup

    taxonomy = load_taxonomy(args.taxonomy)
    leaves = taxonomy_leaves(taxonomy)
    records, skipped = load_records(args.data, leaves, args.min_chars)
    for why, n in sorted(skipped.items()):
        print(f"  left out {n}: {why}")
    space = build_label_space(records, taxonomy, leaves, min_leaf=args.min_leaf_pages,
                              min_specialty=args.min_specialty_pages)
    print(f"{len(records)} pages, {len(space['leaf'])} leaf classes "
          f"({len(leaves)} taxonomy leaves, {len(space['folded'])} folded), {len(space['specialty'])} specialties")
    targets = [encode_targets(r, space) for r in records]
    groups = assign_groups(records)
    real = [i for i, r in enumerate(records) if not r["synthetic"]]
    sides = ["train"] * len(records)
    for i, side in zip(real, label_split([page_labels(targets[i], space) for i in real], [groups[i] for i in real],
                                         args.val_fraction, args.test_fraction, args.seed, args.min_eval_groups)):
        sides[i] = side
    idx = {s: [i for i, side in enumerate(sides) if side == s] for s in ("train", "val", "test")}
    print("split: " + ", ".join(f"{s} {len(v)}" for s, v in idx.items()))

    seed_everything(args.seed)
    device = pick_device(args.device)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    ids = encode_texts(tokenizer, [r["text"] for r in records], args.max_length, args.head_fraction)
    pad_id = tokenizer.pad_token_id or 0
    model = build_model(args.base_model, space, args.dropout).to(device)
    repeat = repeat_factors([targets[i] for i in idx["train"]], space, args.oversample, args.max_repeat)
    rng = random.Random(args.seed)
    sample = sampled_order(len(idx["train"]), repeat, rng)
    order = [idx["train"][j] for j in sample]
    loss_fns = {
        "leaf": torch.nn.CrossEntropyLoss(weight=class_weights(targets, "leaf", len(space["leaf"]), order).to(device)),
        "specialty": torch.nn.CrossEntropyLoss(ignore_index=IGNORE),
    }
    steps_per_epoch = math.ceil(len(order) / args.batch_size)
    total = steps_per_epoch * (1 if args.check else args.epochs)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    scheduler = get_linear_schedule_with_warmup(optimizer, max(1, int(WARMUP_FRACTION * total)), total)
    settings = {"base_model": args.base_model, "max_length": args.max_length, "head_fraction": args.head_fraction,
                "dropout": args.dropout, "top_k": TOP_K, "per_l1": PER_L1, "min_prob": MIN_PROB}

    def step(input_ids, mask, tgt) -> float:
        model.train()
        optimizer.zero_grad(set_to_none=True)
        logits = model(input_ids.to(device), mask.to(device))
        loss = loss_fns["leaf"](logits["leaf"], tgt["leaf"].to(device))
        if "specialty" in logits and (tgt["specialty"] != IGNORE).any():
            loss = loss + args.specialty_loss * loss_fns["specialty"](logits["specialty"], tgt["specialty"].to(device))
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        scheduler.step()
        return float(loss.item())

    if args.check:
        started = time.time()
        for n, (input_ids, mask, tgt, _c) in enumerate(batches(ids, targets, order, pad_id, args.batch_size)):
            step(input_ids, mask, tgt)
            if n == 1:
                break
        per = (time.time() - started) / 2
        print(f"check: 2 steps on {device}, ~{per:.2f}s a step, ~{per * steps_per_epoch * args.epochs / 60:.0f} min "
              f"for {args.epochs} epochs; nothing saved")
        return 0

    best, best_epoch = -1.0, 0
    for epoch in range(1, args.epochs + 1):
        sample = sampled_order(len(idx["train"]), repeat, rng)
        order = [idx["train"][j] for j in sample]
        recent: list[float] = []
        for n, (input_ids, mask, tgt, _c) in enumerate(batches(ids, targets, order, pad_id, args.batch_size), 1):
            recent.append(step(input_ids, mask, tgt))
            if args.log_every and n % args.log_every == 0:
                print(f"  epoch {epoch} step {n}/{steps_per_epoch} loss {sum(recent) / len(recent):.4f}", flush=True)
                recent = []
        scores = evaluate(model, ids, targets, idx["val"] or idx["train"], pad_id, args.batch_size, device, space)
        improved = scores["leaf_macro_f1"] > best
        print(f"epoch {epoch}: val leaf macro F1 {scores['leaf_macro_f1']:.4f}, accuracy {scores['leaf_accuracy']:.4f}, "
              f"top-5 recall {scores['top5_recall']:.4f}, L1 {scores['l1_accuracy']:.4f}"
              + ("  (best so far, saved)" if improved else ""), flush=True)
        if improved:
            best, best_epoch = scores["leaf_macro_f1"], epoch
            save_model(args.output, model, tokenizer, space, settings)

    model, _tok, space = load_model(args.output, device)
    side = "test" if idx["test"] else "val"
    scores = evaluate(model, ids, targets, idx[side], pad_id, args.batch_size, device, space)
    write_csv(args.output / "per_leaf.csv", ["leaf", "train_pages", "support", "correct", "predicted", "f1"],
              [[name, space["train_pages"]["leaf"].get(name, 0), *row[:3], round(row[3], 4)]
               for name, row in zip(space["leaf"], scores.pop("per_leaf"))])
    write_json(args.output / "metrics_summary.json", {"scored_on": side, "best_epoch": best_epoch, **scores})
    print(f"best epoch {best_epoch}; {side}: " + ", ".join(f"{k} {v}" for k, v in scores.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
