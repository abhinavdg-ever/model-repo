#!/usr/bin/env python3
"""Train the encounter-type model: one encoder, one head, one page at a time.

Input is ``encounter_tags.jsonl`` (a copy of ``Training/encounter/encounter_tags.jsonl``:
one page per line, ``text``, ``encounter_type``, ``encounter_on_page``, ``basis``,
``train``, ``visit``, ``chart_id``, ``page_index``). Output, by default
``output/encounter``, always holds the best epoch by validation macro F1.

What it learns (``--target``):

* ``page`` (default, 2026-10-25): ``encounter_on_page`` -- the setting only when the
  page itself shows it (a field, a statement, vitals with an exam, the page type),
  else **No evidence on page**, a class of its own. Six classes: Inpatient, Outpatient
  (F2F), Outpatient (Evisit), Outpatient (Home Visit), Not Available, No evidence on
  page. The visit is applied to the whole document afterwards, as the pipeline does;
  a page that says nothing then abstains instead of guessing. Synthetic pages, which
  have no page-level column, use ``encounter_type``.
* ``visit``: ``encounter_type`` -- the setting of the visit the page belongs to (the
  first run's target; five classes, No evidence on page never used).

    python train_encounter.py --check          # two steps and a time estimate; saves nothing
    python train_encounter.py                  # a run: 20 epochs
    python train_encounter.py --with-mock      # + data/mock_pages.jsonl, training split only

What is learned, and what is not:

* ``train: false`` rows (basis ``default``: outpatient with no modality stated, the
  client's Evisit default) are never learned. They are predicted and reported on
  their own; the default stays an explicit rule after the model.
* ``basis`` says where a label came from. ``inherited`` labels were carried from
  another page of the visit, so the page itself may say nothing about the
  encounter: they train at ``--inherited-weight`` (0.5). ``stated`` / ``field`` /
  ``inferred`` / ``prior`` / ``hand-labelled`` train at 1.
* The loss is class-weighted, so Home Visit (51 pages) and Evisit (120) count.

The split is by chart (the leading number of ``chart_id``: ``53688890`` and
``53688890_240126_2000`` are one chart); loose pages stand alone; synthetic pages
are training only. Besides page scores, the summary scores each visit the way the
pipeline decides a document: every page votes, the first page twice, and Not
Available wins only when every vote is Not Available.

Page text is never printed: only counts, class names and metrics.
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
DEFAULT_DATA = HERE / "data" / "encounter_tags.jsonl"
DEFAULT_MOCK = HERE / "data" / "mock_pages.jsonl"
DEFAULT_OUTPUT = HERE / "output" / "encounter"

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

LABEL_VERSION = "encounter-v1"
CLASSES = ["Inpatient", "Outpatient (F2F)", "Outpatient (Evisit)", "Outpatient (Home Visit)", "Not Available",
           "No evidence on page"]
NOT_AVAILABLE = "Not Available"
NO_EVIDENCE = "No evidence on page"
# The five classes of a model trained on the visit target before 2026-10-25; it still loads.
VISIT_CLASSES = CLASSES[:5]
SMALL_CLASSES = ("Outpatient (Home Visit)", "Outpatient (Evisit)")
BASIS_WEIGHT = {"inherited": 0.5}  # every other basis trains at 1

BASE_MODEL = "distilbert-base-uncased"
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
IGNORE = -100


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=DEFAULT_DATA, help="encounter_tags.jsonl (default: data/)")
    p.add_argument("--with-mock", nargs="?", type=Path, const=DEFAULT_MOCK, default=None, metavar="JSONL",
                   help="add synthetic pages (default data/mock_pages.jsonl), training split only")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="model folder (best epoch)")
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
    p.add_argument("--target", choices=("page", "visit"), default="page",
                   help="page: encounter_on_page (No evidence on page is a class); visit: encounter_type")
    p.add_argument("--inherited-weight", type=float, default=BASIS_WEIGHT["inherited"],
                   help="loss weight of a label inherited from another page of the visit (0 leaves them out)")
    return p.parse_args(argv)


# --- Records -----------------------------------------------------------------------


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "0", "no", "n", "")


def load_records(path: Path, min_chars: int, synthetic: bool = False,
                 target: str = "visit") -> tuple[list[dict], int]:
    """Usable pages and how many were left out for having almost no text. ``label``
    is the target (``encounter_on_page`` for "page", where the row has it; else
    ``encounter_type``); ``visit_label`` is always ``encounter_type``. A label outside
    CLASSES stops the run with the names and counts."""
    if not path.is_file():
        raise SystemExit(f"No training file at {path}")
    records, short, bad = [], 0, Counter()
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path.name} line {number} is not JSON ({exc.msg})") from None
            visit_label = _clean(row.get("encounter_type"))
            label = _clean(row.get("encounter_on_page")) if target == "page" else ""
            label = label or visit_label
            if label not in CLASSES or visit_label not in CLASSES:
                bad[label or "<empty>"] += 1
                continue
            text = str(row.get("text") or "")
            if len("".join(text.split())) < min_chars:
                short += 1
                continue
            is_synthetic = synthetic or bool(row.get("synthetic"))
            records.append({
                "image": str(row.get("image") or f"line-{number}"),
                "text": text,
                "label": label,
                "visit_label": visit_label,
                "basis": _clean(row.get("basis")) or ("mock" if is_synthetic else ""),
                "train": _truthy(row.get("train", True)),
                "visit": _clean(row.get("visit")),
                "chart_id": _clean(row.get("chart_id")),
                "page_index": _page_index(row.get("page_index")),
                "synthetic": is_synthetic,
            })
    if bad:
        raise SystemExit(f"{path.name}: encounter types not in {CLASSES}: "
                         + ", ".join(f"{n!r} ({k})" for n, k in sorted(bad.items())))
    return records, short


def _page_index(value: Any) -> int | None:
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else None


def loss_weight(record: dict, inherited_weight: float) -> float:
    """0 for a row that must not be learned; else the basis weight."""
    if not record["train"]:
        return 0.0
    return inherited_weight if record["basis"] == "inherited" else 1.0


# --- Split -------------------------------------------------------------------------


def chart_group(chart_id: Any) -> str | None:
    """The leading number of a chart id. ``53688890`` and ``53688890_240126_2000``
    are one chart."""
    text = str(chart_id or "").strip()
    if not text:
        return None
    match = re.match(r"\d+", text)
    return match.group(0) if match else text


def assign_groups(records: list[dict]) -> list[str]:
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


def group_split(records: list[dict], groups: list[str], val_fraction: float, test_fraction: float,
                seed: int) -> list[str]:
    """'train' / 'val' / 'test' per record. A group never sits on two sides; classes
    are balanced as far as whole groups allow; every class keeps a training page;
    a synthetic page is always 'train'."""
    real = [i for i, r in enumerate(records) if not r["synthetic"]]
    by_group: dict[str, Counter] = {}
    for i in real:
        by_group.setdefault(groups[i], Counter())[records[i]["label"]] += 1
    totals = Counter(records[i]["label"] for i in real)
    order = list(by_group)
    random.Random(seed).shuffle(order)
    order.sort(key=lambda g: -sum(by_group[g].values()))
    test = _pick_groups(by_group, order, {c: test_fraction * n for c, n in totals.items()}) \
        if test_fraction > 0 else set()
    rest = [g for g in order if g not in test]
    val = _pick_groups(by_group, rest, {c: val_fraction * n for c, n in totals.items()}) \
        if val_fraction > 0 else set()
    for c in totals:
        held = {groups[i] for i in real if records[i]["label"] == c}
        if held <= test | val:
            test -= held
            val -= held
    return ["train" if records[i]["synthetic"] else "test" if groups[i] in test
            else "val" if groups[i] in val else "train" for i in range(len(records))]


# --- Scores ------------------------------------------------------------------------


def class_scores(truth: list[int], preds: list[int], k: int = len(CLASSES)) -> dict[str, Any]:
    """Per-class precision / recall / F1 / support and the confusion matrix (rows =
    truth). Macro F1 is over the classes present in the truth or the predictions."""
    tp, fp, fn = Counter(), Counter(), Counter()
    confusion = [[0] * k for _ in range(k)]
    for t, p in zip(truth, preds):
        confusion[t][p] += 1
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
    return {
        "macro_f1": sum(f1[c] for c in seen) / len(seen) if seen else 0.0,
        "accuracy": sum(t == p for t, p in zip(truth, preds)) / len(truth) if truth else 0.0,
        "n": len(truth), "precision": precision, "recall": recall, "f1": f1, "support": support,
        "confusion": confusion,
    }


def document_vote(votes: list[tuple[int | None, str]]) -> str:
    """The pipeline's document rule over (page index, encounter type) votes: every
    page votes, the first page twice; Not Available wins only when every vote is Not
    Available. A page with No evidence on page abstains; with no vote left the visit
    is No evidence on page (the pipeline then takes the family default). A tie goes to
    the first page's vote, then to CLASSES order."""
    votes = [v for v in votes if v[1] != NO_EVIDENCE]
    if not votes:
        return NO_EVIDENCE
    ordered = sorted(votes, key=lambda v: (v[0] is None, v[0] or 0))
    tally = Counter()
    for n, (_, label) in enumerate(ordered):
        tally[label] += 2 if n == 0 else 1
    if set(tally) == {NOT_AVAILABLE}:
        return NOT_AVAILABLE
    tally.pop(NOT_AVAILABLE, None)
    first = ordered[0][1]
    return max(tally, key=lambda c: (tally[c], c == first, -CLASSES.index(c)))


def visit_scores(rows: list[dict]) -> dict[str, Any]:
    """Visit-level agreement: the gold visit label is the document vote over the
    pages' visit labels (``encounter_type``), the predicted one over the predicted
    labels. Pages without a visit are left out."""
    by_visit: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        if r["visit"]:
            by_visit[r["visit"]].append(r)
    gold, pred = [], []
    for pages in by_visit.values():
        gold.append(CLASSES.index(document_vote([(p["page_index"], p["visit_label"]) for p in pages])))
        pred.append(CLASSES.index(document_vote([(p["page_index"], p["pred"]) for p in pages])))
    return class_scores(gold, pred)


def report_lines(scores: dict[str, Any], what: str, unit: str = "pages") -> list[str]:
    lines = [f"{what}: {scores['n']} scored, accuracy {scores['accuracy']:.3f}, macro F1 {scores['macro_f1']:.3f}"]
    for c, name in enumerate(CLASSES):
        mark = "  <- small class" if name in SMALL_CLASSES else ""
        lines.append(f"  {name:<24} recall {scores['recall'][c]:.2f}  precision {scores['precision'][c]:.2f}  "
                     f"F1 {scores['f1'][c]:.2f}  ({scores['support'][c]} {unit}){mark}")
    return lines


# --- Tokens ---------------------------------------------------------------------------


def head_tail(ids: list[int], max_length: int, head_fraction: float, cls_id: int | None,
              sep_id: int | None) -> list[int]:
    """Content ids cut to fit ``max_length`` with the special tokens: the first
    ``head_fraction`` of the budget from the start of the page (header, visit type)
    and the rest from the end (signature, attestation)."""
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


# --- Model ------------------------------------------------------------------------------


def build_model(encoder_source: str, dropout: float = DROPOUT, n_classes: int = len(CLASSES)):
    """Encoder plus one linear head on the first token's hidden state."""
    from torch import nn
    from transformers import AutoModel

    class EncounterModel(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.encoder = AutoModel.from_pretrained(encoder_source)
            config = self.encoder.config
            hidden = getattr(config, "hidden_size", None) or getattr(config, "dim")
            self.dropout = nn.Dropout(dropout)
            self.head = nn.Linear(hidden, n_classes)

        def forward(self, input_ids, attention_mask):
            hidden = self.encoder(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state[:, 0]
            return self.head(self.dropout(hidden)).float()

    return EncounterModel()


def save_model(output: Path, model, tokenizer, settings: dict[str, Any]) -> None:
    import torch

    output.mkdir(parents=True, exist_ok=True)
    model.encoder.save_pretrained(output / "encoder")
    tokenizer.save_pretrained(output / "encoder")
    tmp = output / "head.pt.tmp"
    torch.save(model.head.state_dict(), tmp)
    tmp.replace(output / "head.pt")
    write_json(output / "label_space.json", {"label_version": LABEL_VERSION, "classes": CLASSES, **settings})


def load_model(folder: Path, device=None):
    """(model, tokenizer, label space) from a saved encounter folder."""
    import torch
    from transformers import AutoTokenizer

    space = json.loads((folder / "label_space.json").read_text(encoding="utf-8"))
    classes = space.get("classes")
    if classes not in (CLASSES, VISIT_CLASSES):
        raise SystemExit(f"{folder} was trained on classes {classes}, not {CLASSES}")
    model = build_model(str(folder / "encoder"), space.get("dropout", DROPOUT), len(classes))
    model.head.load_state_dict(torch.load(folder / "head.pt", map_location="cpu", weights_only=True))
    model.to(device or "cpu").eval()
    return model, AutoTokenizer.from_pretrained(folder / "encoder"), space


# --- Writing ------------------------------------------------------------------------------


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


# --- Torch helpers --------------------------------------------------------------------------


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


def make_loader(ids: list[list[int]], labels: list[int], weights: list[float], pad_id: int, batch_size: int,
                shuffle: bool, seed: int):
    import torch
    from torch.utils.data import DataLoader

    def collate(batch):
        width = max(len(ids[i]) for i in batch)
        return {
            "index": torch.tensor(batch),
            "input_ids": torch.tensor([ids[i] + [pad_id] * (width - len(ids[i])) for i in batch]),
            "attention_mask": torch.tensor([[1] * len(ids[i]) + [0] * (width - len(ids[i])) for i in batch]),
            "label": torch.tensor([labels[i] for i in batch]),
            "weight": torch.tensor([weights[i] for i in batch], dtype=torch.float),
        }

    return DataLoader(list(range(len(ids))), batch_size=batch_size, shuffle=shuffle, collate_fn=collate,
                      num_workers=0, generator=torch.Generator().manual_seed(seed))


def class_weights(labels: list[int], weights: list[float]):
    """Inverse frequency over the weighted training rows."""
    import torch

    counts = Counter()
    for label, w in zip(labels, weights):
        if w > 0:
            counts[label] += w
    n = sum(counts.values())
    return torch.tensor([n / (len(CLASSES) * max(1e-6, counts[c])) if counts[c] else 0.0
                         for c in range(len(CLASSES))], dtype=torch.float)


def weighted_loss(logits, batch, class_w):
    """Cross-entropy per row, times the row's basis weight; rows of weight 0 add
    nothing. Normalised by the summed weights."""
    import torch

    per_row = torch.nn.functional.cross_entropy(logits, batch["label"], weight=class_w, reduction="none")
    w = batch["weight"]
    return (per_row * w).sum() / w.sum().clamp(min=1e-6)


def predict_rows(model, loader, device) -> list[tuple[int, int, float, list[float]]]:
    """(record index, predicted class, its probability, every probability)."""
    import torch

    model.eval()
    out = []
    with torch.no_grad():
        for batch in loader:
            probs = torch.softmax(model(batch["input_ids"].to(device), batch["attention_mask"].to(device)), -1)
            for i, row in zip(batch["index"].tolist(), probs.tolist()):
                k = max(range(len(row)), key=row.__getitem__)
                out.append((i, k, row[k], row))
    return out


# --- Main ------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    import torch
    import transformers
    from transformers import AutoTokenizer, get_linear_schedule_with_warmup
    from transformers.utils import logging as hf_logging

    hf_logging.set_verbosity_error()
    output = args.output.resolve()
    device = pick_device(args.device)
    environment = {"python": platform.python_version(), "torch": torch.__version__,
                   "transformers": transformers.__version__, "device": str(device)}
    if device.type == "cuda":
        environment["gpu"] = torch.cuda.get_device_name(device)
    print("Environment: " + ", ".join(f"{k} {v}" for k, v in environment.items()), flush=True)

    records, short = load_records(args.data, args.min_chars, target=args.target)
    if args.with_mock is not None:
        mock, mock_short = load_records(args.with_mock, args.min_chars, synthetic=True, target=args.target)
        records += mock
        short += mock_short
    weights = [loss_weight(r, args.inherited_weight) for r in records]
    labels = [CLASSES.index(r["label"]) for r in records]
    print(f"Pages: {len(records)} usable ({sum(r['synthetic'] for r in records)} synthetic, training only), "
          f"{short} left out with fewer than {args.min_chars} characters")
    print("  by class: " + ", ".join(f"{c} {n}" for c, n in Counter(r["label"] for r in records).most_common()))
    print("  by basis: " + ", ".join(f"{b or '<none>'} {n}" for b, n in Counter(r["basis"] for r in records).most_common()))
    print(f"  not learned (train=false): {sum(not r['train'] for r in records)}; inherited labels at weight "
          f"{args.inherited_weight}")

    groups = assign_groups(records)
    sides = group_split(records, groups, args.val_fraction, args.test_fraction, args.seed)
    split = {s: [i for i, side in enumerate(sides) if side == s] for s in ("train", "val", "test")}
    print("Split by chart: " + ", ".join(
        f"{s} {len(ix)} pages / {len({groups[i] for i in ix})} groups" for s, ix in split.items()))
    for s, ix in split.items():
        print(f"  {s}: " + ", ".join(f"{c} {n}" for c, n in Counter(records[i]["label"] for i in ix).most_common()))
    if not split["val"]:
        raise SystemExit("No validation pages: raise --val-fraction")

    seed_everything(args.seed)
    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    ids = encode_texts(tokenizer, [r["text"] for r in records], args.max_length, args.head_fraction)
    model = build_model(args.base_model, args.dropout).to(device)
    pad = tokenizer.pad_token_id or 0

    def loader(ix: list[int], shuffle: bool):
        return make_loader([ids[i] for i in ix], [labels[i] for i in ix], [weights[i] for i in ix], pad,
                           args.batch_size, shuffle, args.seed)

    train_ix = [i for i in split["train"] if weights[i] > 0]
    train_loader = loader(train_ix, True)
    class_w = class_weights([labels[i] for i in train_ix], [weights[i] for i in train_ix]).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    total_steps = max(1, len(train_loader) * args.epochs)
    scheduler = get_linear_schedule_with_warmup(optimizer, max(1, int(WARMUP_FRACTION * total_steps)), total_steps)
    use_amp = device.type == "cuda"
    scaler = torch.amp.GradScaler("cuda") if use_amp else None

    def train_step(batch) -> float:
        model.train()
        batch = {k: v.to(device) for k, v in batch.items()}
        optimizer.zero_grad(set_to_none=True)
        with torch.autocast(device_type="cuda", enabled=use_amp):
            logits = model(batch["input_ids"], batch["attention_mask"])
        loss = weighted_loss(logits, batch, class_w)
        if scaler:
            scaler.scale(loss).backward()
            scale = scaler.get_scale()
            scaler.step(optimizer)
            scaler.update()
            stepped = scaler.get_scale() >= scale
        else:
            loss.backward()
            optimizer.step()
            stepped = True
        if stepped:
            scheduler.step()
        return float(loss.item())

    # Validation scores the learnable rows only (train=true); the default rows are reported apart.
    val_ix = [i for i in split["val"] if records[i]["train"]]
    if args.check:
        batches = iter(train_loader)
        train_step(next(batches))
        start = time.perf_counter()
        train_step(next(batches, None) or next(iter(train_loader)))
        if device.type == "cuda":
            torch.cuda.synchronize()
        per_step = time.perf_counter() - start
        minutes = per_step * (len(train_loader) + len(val_ix) / args.batch_size / 3) / 60
        print(f"Check passed: 2 training steps on {device}, batch size {args.batch_size}. About {per_step:.2f} s "
              f"per step, {len(train_loader)} steps per epoch: roughly {minutes:.1f} minutes per epoch. Nothing saved.")
        return 0

    settings = {k: getattr(args, k) for k in ("base_model", "max_length", "head_fraction", "dropout",
                                              "inherited_weight", "target")}
    settings["train_pages"] = dict(Counter(CLASSES[labels[i]] for i in train_ix))
    config = {"data": str(args.data.resolve()), "output": str(output), "environment": environment,
              "settings": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
              "pages": {"usable": len(records), "left_out_short": short, **{s: len(ix) for s, ix in split.items()}}}
    write_csv(output / "split.csv", ["image", "group", "split"],
              [[r["image"], g, s] for r, g, s in zip(records, groups, sides)])

    val_loader = loader(val_ix, False)
    log, best, best_epoch = [], -1.0, 0
    for epoch in range(1, args.epochs + 1):
        started = time.perf_counter()
        losses = []
        for step, batch in enumerate(train_loader, 1):
            losses.append(train_step(batch))
            if args.log_every > 0 and (step % args.log_every == 0 or step == len(train_loader)):
                recent = losses[-args.log_every:]
                print(f"  epoch {epoch} step {step}/{len(train_loader)}: loss {sum(recent) / len(recent):.4f}, "
                      f"lr {scheduler.get_last_lr()[0]:.2e}, {time.perf_counter() - started:.0f}s", flush=True)
        preds = predict_rows(model, val_loader, device)
        scores = class_scores([labels[val_ix[i]] for i, _, _, _ in preds], [k for _, k, _, _ in preds])
        row = {"epoch": epoch, "train_loss": round(sum(losses) / max(1, len(losses)), 4),
               "val_macro_f1": round(scores["macro_f1"], 4), "val_accuracy": round(scores["accuracy"], 4),
               **{f"val_recall_{c}": round(scores["recall"][k], 4) for k, c in enumerate(CLASSES)},
               "seconds": round(time.perf_counter() - started, 1)}
        log.append(row)
        improved = scores["macro_f1"] > best
        print(f"Epoch {epoch}: loss {row['train_loss']}, val macro F1 {scores['macro_f1']:.3f}, accuracy "
              f"{scores['accuracy']:.3f}" + ("  (best so far, saved)" if improved else ""), flush=True)
        if improved:
            best, best_epoch = scores["macro_f1"], epoch
            save_model(output, model, tokenizer, settings)
        write_csv(output / "training_log.csv", list(row), [list(r.values()) for r in log])
        write_json(output / "run_config.json", {**config, "epochs_run": epoch, "best_epoch": best_epoch,
                                                "best_val_macro_f1": best})

    saved, _, _ = load_model(output, device)
    summary = evaluate(saved, records, labels, weights, groups, sides, ids, pad, args, device, output)
    summary["best_epoch"] = best_epoch
    write_json(output / "metrics_summary.json", summary)
    print(f"Best epoch {best_epoch}. Saved: {output}")
    return 0


def evaluate(model, records, labels, weights, groups, sides, ids, pad, args, device, output: Path) -> dict[str, Any]:
    """Page and visit scores on test (else val), the default rows apart, and the CSVs."""
    side = "test" if any(s == "test" for s in sides) else "val"
    lines, summary = [], {"scored_on": side}
    for s in ("val", "test"):
        ix = [i for i, x in enumerate(sides) if x == s]
        if not ix:
            continue
        loader = make_loader([ids[i] for i in ix], [labels[i] for i in ix], [1.0] * len(ix), pad,
                             args.batch_size, False, args.seed)
        for j, k, p, probs in predict_rows(model, loader, device):
            i = ix[j]
            r = records[i]
            lines.append({"image": r["image"], "split": s, "group": groups[i], "visit": r["visit"],
                          "page_index": r["page_index"], "basis": r["basis"], "train": r["train"],
                          "label": r["label"], "visit_label": r["visit_label"], "pred": CLASSES[k],
                          "confidence": round(p, 4),
                          **{f"p_{c}": round(v, 4) for c, v in zip(CLASSES, probs)}})
    scored = [x for x in lines if x["split"] == side and x["train"]]
    page = class_scores([CLASSES.index(x["label"]) for x in scored], [CLASSES.index(x["pred"]) for x in scored])
    visit = visit_scores([x for x in lines if x["split"] == side])
    defaults = [x for x in lines if not x["train"]]
    for line in report_lines(page, f"Pages ({side}, learnable rows)") + report_lines(visit, f"Visits ({side})", "visits"):
        print(line)
    print(f"Client-default rows (train=false, not learned): {len(defaults)} in val/test; model says "
          + (", ".join(f"{c} {n}" for c, n in Counter(x["pred"] for x in defaults).most_common()) or "—"))
    by_basis = defaultdict(list)
    for x in scored:
        by_basis[x["basis"]].append(x["label"] == x["pred"])
    print("  page accuracy by basis: " + ", ".join(f"{b} {sum(v) / len(v):.2f} ({len(v)})" for b, v in sorted(by_basis.items())))

    header = list(lines[0]) if lines else ["image"]
    write_csv(output / "predictions.csv", header, [list(x.values()) for x in lines])
    write_csv(output / "errors.csv", header, [list(x.values()) for x in lines if x["label"] != x["pred"] and x["train"]])
    for name, scores in (("pages", page), ("visits", visit)):
        write_csv(output / f"confusion_{name}.csv", ["true \\ predicted", *CLASSES],
                  [[c, *row] for c, row in zip(CLASSES, scores["confusion"])])
        write_csv(output / f"class_metrics_{name}.csv", ["class", "precision", "recall", "f1", "support"],
                  [[c, round(scores["precision"][k], 4), round(scores["recall"][k], 4), round(scores["f1"][k], 4),
                    scores["support"][k]] for k, c in enumerate(CLASSES)])
    summary.update({
        "pages": {k: page[k] for k in ("n", "accuracy", "macro_f1")},
        "page_recall": {c: round(page["recall"][k], 4) for k, c in enumerate(CLASSES)},
        "visits": {k: visit[k] for k in ("n", "accuracy", "macro_f1")},
        "visit_recall": {c: round(visit["recall"][k], 4) for k, c in enumerate(CLASSES)},
        "page_accuracy_by_basis": {b: round(sum(v) / len(v), 4) for b, v in by_basis.items()},
        "default_rows_predicted": dict(Counter(x["pred"] for x in defaults)),
    })
    return summary


if __name__ == "__main__":
    sys.exit(main())
