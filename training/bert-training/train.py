#!/usr/bin/env python3
"""Train the page classifier: a BERT text model that predicts ``model_type``.

Input is ``training_data.jsonl`` from the annotation tool (one page per line:
``text`` is the OCR text, ``model_type`` the label). Output is a Hugging Face
model folder, by default ``output/page-family``, that always holds the best
epoch so far by validation macro F1 — so extra epochs never make it worse.

    python train.py                       # a new run: 4 epochs
    python train.py --epochs 3            # a new run: 3 epochs
    python train.py --resume              # one more epoch, same classes and split
    python train.py --resume --epochs 2   # two more
    python train.py --check               # compatibility check, saves nothing

Runs unchanged on Colab (GPU), a Windows laptop with an NVIDIA GPU, and CPU.
Page text is never printed: only counts, names, shapes and metrics.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import random
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# Inputs and outputs live in this folder; copy the finished model into
# core-pipeline/models/page-family (see README).
DEFAULT_DATA = HERE / "data" / "training_data.jsonl"
DEFAULT_OUTPUT = HERE / "output" / "page-family"
TAXONOMY = HERE / "taxonomy.json"

# --- Settings (every one is also an argument) --------------------------------
# PyTorch only. Colab also has TensorFlow installed; without these,
# transformers imports it and it prints "Could not find cuda drivers" — noise,
# since training runs on PyTorch's GPU.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

MODEL_NAME = "bert-base-uncased"
MAX_LENGTH = 512
BATCH_SIZE = 8
LEARNING_RATE = 2e-5
WARMUP_FRACTION = 0.1  # of the first epoch's steps; constant learning rate after
SEED = 42
MIN_PAGES = 5  # model types with fewer usable pages are left to the keyword model
EPOCHS = 4  # a new run; --resume adds 1 unless --epochs says otherwise
VAL_FRACTION = 0.2
MIN_CHARS = 30  # pages with fewer non-space characters are left out

STATE_FILE = "state.pt"
OUTPUT_FILES = (
    "label_mapping.csv",
    "trained_classes.json",
    "skipped_classes.csv",
    "category_wise_metrics.csv",
    "confusion_matrix.csv",
    "training_log.csv",
    "run_config.json",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", type=Path, default=DEFAULT_DATA, help="training_data.jsonl (default: data/)")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="model folder (best epoch)")
    p.add_argument("--resume-dir", type=Path, default=None,
                   help="where the resume state lives (default: <output>/../page-family-resume)")
    p.add_argument("--resume", action="store_true", help="continue from the last saved epoch")
    p.add_argument("--restart", action="store_true", help="discard an existing resume state and start over")
    p.add_argument("--epochs", type=int, default=None,
                   help=f"epochs to run (default: {EPOCHS} for a new run, 1 with --resume)")
    p.add_argument("--check", action="store_true", help="two training steps and a time estimate; saves nothing")
    p.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    p.add_argument("--model-name", default=MODEL_NAME, help="Hugging Face name or local folder")
    p.add_argument("--max-length", type=int, default=MAX_LENGTH)
    p.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    p.add_argument("--lr", type=float, default=LEARNING_RATE)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--min-pages", type=int, default=MIN_PAGES)
    p.add_argument("--val-fraction", type=float, default=VAL_FRACTION)
    p.add_argument("--min-chars", type=int, default=MIN_CHARS)
    return p.parse_args(argv)


# --- Data ---------------------------------------------------------------------


def load_taxonomy(path: Path = TAXONOMY) -> dict[str, dict[str, str]]:
    """model_type → {page_type, codability}, in taxonomy order."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {m["model_type"]: m for m in data["model_types"]}


def load_records(path: Path, taxonomy: dict[str, Any], min_chars: int) -> tuple[list[dict], int]:
    """Usable pages, and how many were left out for having almost no text."""
    if not path.is_file():
        raise SystemExit(f"No training file at {path}")
    records, short, unknown = [], 0, Counter()
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path.name} line {number} is not JSON ({exc.msg})") from None
            label = str(row.get("model_type") or "").strip()
            if label not in taxonomy:
                unknown[label or "<empty>"] += 1
                continue
            text = str(row.get("text") or "")
            if len("".join(text.split())) < min_chars:
                short += 1
                continue
            chart = row.get("chart_id")
            records.append({
                "image": str(row.get("image") or f"line-{number}"),
                "text": text,
                "label": label,
                "group": str(chart) if chart not in (None, "") else f"image:{row.get('image') or number}",
            })
    if unknown:
        listed = ", ".join(f"{name!r} ({count})" for name, count in sorted(unknown.items()))
        raise SystemExit(f"model_type values not in taxonomy.json: {listed}")
    return records, short


def choose_classes(records: list[dict], taxonomy: dict[str, Any], min_pages: int) -> tuple[list[str], list[dict]]:
    """Trained classes in taxonomy order, and the ones skipped for too few pages."""
    counts = Counter(r["label"] for r in records)
    trained = [m for m in taxonomy if counts.get(m, 0) >= min_pages]
    skipped = [
        {"model_type": m, "pages": counts.get(m, 0),
         "reason": "no pages" if not counts.get(m) else f"fewer than {min_pages} pages"}
        for m in taxonomy if counts.get(m, 0) < min_pages
    ]
    return trained, skipped


def group_split(records: list[dict], classes: list[str], val_fraction: float, seed: int) -> set[str]:
    """Validation images. A chart's pages never sit on both sides; classes are
    balanced as far as whole charts allow."""
    by_group: dict[str, Counter] = {}
    for r in records:
        by_group.setdefault(r["group"], Counter())[r["label"]] += 1
    totals = Counter(r["label"] for r in records)
    target = {c: val_fraction * totals[c] for c in classes}
    have = Counter()
    rng = random.Random(seed)
    groups = list(by_group)
    rng.shuffle(groups)
    # Big charts first: they are the hard ones to place.
    groups.sort(key=lambda g: -sum(by_group[g].values()))
    val_groups: set[str] = set()
    for g in groups:
        counts = by_group[g]
        gain = sum(min(n, max(0.0, target[c] - have[c])) for c, n in counts.items())
        overshoot = sum(max(0.0, have[c] + n - target[c]) for c, n in counts.items())
        if gain > 0 and gain >= overshoot:
            val_groups.add(g)
            have.update(counts)
    # Every class must keep at least one training page.
    for c in classes:
        if all(r["group"] in val_groups for r in records if r["label"] == c):
            val_groups -= {r["group"] for r in records if r["label"] == c}
    return {r["image"] for r in records if r["group"] in val_groups}


# --- Torch helpers --------------------------------------------------------------


def pick_device(choice: str):
    import torch

    if choice == "cuda" and not torch.cuda.is_available():
        raise SystemExit("--device cuda was asked for, but PyTorch sees no CUDA GPU (see README)")
    if choice == "cpu":
        return torch.device("cpu")
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def print_environment(device) -> dict[str, Any]:
    import torch
    import transformers

    info = {
        "python": platform.python_version(),
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "device": str(device),
    }
    if device.type == "cuda":
        props = torch.cuda.get_device_properties(device)
        info["gpu"] = props.name
        info["gpu_memory_gb"] = round(props.total_memory / 1024**3, 1)
    print("Environment: " + ", ".join(f"{k} {v}" for k, v in info.items()), flush=True)
    return info


def seed_everything(seed: int) -> None:
    import numpy as np
    import torch

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def rng_state() -> dict[str, Any]:
    import numpy as np
    import torch

    return {
        "python": random.getstate(),
        "numpy": np.random.get_state(),
        "torch": torch.get_rng_state(),
        "cuda": torch.cuda.get_rng_state_all() if torch.cuda.is_available() else None,
    }


def restore_rng(state: dict[str, Any]) -> None:
    import numpy as np
    import torch

    random.setstate(state["python"])
    np.random.set_state(state["numpy"])
    torch.set_rng_state(state["torch"])
    if state.get("cuda") is not None and torch.cuda.is_available():
        torch.cuda.set_rng_state_all(state["cuda"])


def make_loader(tokenizer, records, label2id, max_length, batch_size, shuffle, seed):
    import torch
    from torch.utils.data import DataLoader

    encoded = tokenizer([r["text"] for r in records], truncation=True, max_length=max_length)
    items = [
        {"input_ids": encoded["input_ids"][i], "attention_mask": encoded["attention_mask"][i],
         "labels": label2id[r["label"]]}
        for i, r in enumerate(records)
    ]

    def collate(batch):
        width = max(len(b["input_ids"]) for b in batch)
        pad = tokenizer.pad_token_id or 0
        ids = [b["input_ids"] + [pad] * (width - len(b["input_ids"])) for b in batch]
        mask = [b["attention_mask"] + [0] * (width - len(b["attention_mask"])) for b in batch]
        return {
            "input_ids": torch.tensor(ids),
            "attention_mask": torch.tensor(mask),
            "labels": torch.tensor([b["labels"] for b in batch]),
        }

    generator = torch.Generator().manual_seed(seed)
    return DataLoader(items, batch_size=batch_size, shuffle=shuffle, collate_fn=collate,
                      num_workers=0, generator=generator)


def class_weights(records, classes):
    import torch

    counts = Counter(r["label"] for r in records)
    n, k = len(records), len(classes)
    return torch.tensor([n / (k * max(1, counts[c])) for c in classes], dtype=torch.float)


def predict(model, loader, device) -> tuple[list[int], list[int]]:
    import torch

    model.eval()
    truth, preds = [], []
    with torch.no_grad():
        for batch in loader:
            batch = {k: v.to(device) for k, v in batch.items()}
            logits = model(input_ids=batch["input_ids"], attention_mask=batch["attention_mask"]).logits
            preds.extend(logits.float().argmax(-1).tolist())
            truth.extend(batch["labels"].tolist())
    return truth, preds


def scores(truth, preds, k) -> dict[str, Any]:
    from sklearn.metrics import confusion_matrix, f1_score, precision_recall_fscore_support

    labels = list(range(k))
    precision, recall, f1, support = precision_recall_fscore_support(
        truth, preds, labels=labels, zero_division=0)
    return {
        "macro_f1": float(f1_score(truth, preds, labels=labels, average="macro", zero_division=0)) if truth else 0.0,
        "accuracy": float(sum(int(a == b) for a, b in zip(truth, preds)) / len(truth)) if truth else 0.0,
        "precision": precision.tolist(), "recall": recall.tolist(), "f1": f1.tolist(),
        "support": support.tolist(),
        "confusion": confusion_matrix(truth, preds, labels=labels).tolist() if truth else [[0] * k] * k,
    }


# --- Writing --------------------------------------------------------------------


def write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    tmp.replace(path)


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def save_best(output: Path, model, tokenizer, ctx: dict[str, Any], result: dict[str, Any]) -> None:
    """The model folder core-pipeline loads, plus the metrics of this epoch."""
    output.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(output)
    tokenizer.save_pretrained(output)
    classes, taxonomy = ctx["classes"], ctx["taxonomy"]
    train_counts, val_counts = ctx["train_counts"], ctx["val_counts"]
    write_csv(output / "label_mapping.csv",
              ["id", "model_type", "page_type", "codability", "train_pages", "val_pages"],
              [[i, c, taxonomy[c]["page_type"], taxonomy[c]["codability"], train_counts[c], val_counts[c]]
               for i, c in enumerate(classes)])
    write_json(output / "trained_classes.json", classes)
    write_csv(output / "skipped_classes.csv", ["model_type", "pages", "reason"],
              [[s["model_type"], s["pages"], s["reason"]] for s in ctx["skipped"]])
    write_csv(output / "category_wise_metrics.csv",
              ["model_type", "precision", "recall", "f1", "val_pages", "train_pages"],
              [[c, round(result["precision"][i], 4), round(result["recall"][i], 4),
                round(result["f1"][i], 4), result["support"][i], train_counts[c]]
               for i, c in enumerate(classes)])
    write_csv(output / "confusion_matrix.csv", ["true \\ predicted", *classes],
              [[c, *row] for c, row in zip(classes, result["confusion"])])


# --- Main -----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    output = args.output.resolve()
    resume_dir = (args.resume_dir or output.parent / f"{output.name}-resume").resolve()
    state_path = resume_dir / STATE_FILE
    device = pick_device(args.device)
    environment = print_environment(device)

    taxonomy = load_taxonomy()
    records, short = load_records(args.data, taxonomy, args.min_chars)
    print(f"Pages: {len(records)} usable, {short} left out with fewer than {args.min_chars} characters")

    state: dict[str, Any] | None = None
    if args.resume:
        if not state_path.is_file():
            raise SystemExit(f"--resume: no saved state at {state_path}")
        state = torch.load(state_path, map_location="cpu", weights_only=False)
        settings = state["settings"]
        for name in ("model_name", "max_length", "seed", "min_pages", "val_fraction", "min_chars"):
            setattr(args, name, settings[name])
    elif state_path.is_file() and not args.check and not args.restart:
        raise SystemExit(f"A saved run exists at {resume_dir}. Pass --resume to continue it, "
                         "or --restart to start over.")

    if state is None:
        classes, skipped = choose_classes(records, taxonomy, args.min_pages)
        if len(classes) < 2:
            raise SystemExit(f"Only {len(classes)} model type(s) have at least {args.min_pages} pages; "
                             "need two or more to train")
        kept = [r for r in records if r["label"] in classes]
        val_images = group_split(kept, classes, args.val_fraction, args.seed)
    else:
        classes, skipped, val_images = state["classes"], state["skipped"], set(state["val_images"])
        kept = [r for r in records if r["label"] in classes]
        missing = val_images - {r["image"] for r in kept}
        if missing:
            print(f"Warning: {len(missing)} validation page(s) from the saved split are no longer in the data")

    label2id = {c: i for i, c in enumerate(classes)}
    id2label = {i: c for c, i in label2id.items()}
    train = [r for r in kept if r["image"] not in val_images]
    val = [r for r in kept if r["image"] in val_images]
    train_counts = Counter(r["label"] for r in train)
    val_counts = Counter(r["label"] for r in val)
    print(f"Classes: {len(classes)} trained, {len(skipped)} skipped "
          f"(fewer than {args.min_pages} pages; see skipped_classes.csv)")
    print(f"Split: {len(train)} training pages, {len(val)} validation pages, "
          f"{len({r['group'] for r in train})} / {len({r['group'] for r in val})} charts or loose pages")
    no_val = [c for c in classes if not val_counts[c]]
    if no_val:
        print(f"Note: {len(no_val)} class(es) have no validation page (their pages sit in one chart): "
              + ", ".join(no_val))

    seed_everything(args.seed)
    source = str(resume_dir / "model") if state else args.model_name
    tokenizer = AutoTokenizer.from_pretrained(source)
    model = AutoModelForSequenceClassification.from_pretrained(
        source, num_labels=len(classes), id2label=id2label, label2id=label2id,
        ignore_mismatched_sizes=True,
    ).to(device)

    train_loader = make_loader(tokenizer, train, label2id, args.max_length, args.batch_size, True, args.seed)
    val_loader = make_loader(tokenizer, val, label2id, args.max_length, args.batch_size, False, args.seed)
    loss_fn = torch.nn.CrossEntropyLoss(weight=class_weights(train, classes).to(device))
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    from transformers import get_constant_schedule_with_warmup

    warmup = max(1, int(WARMUP_FRACTION * len(train_loader)))
    scheduler = get_constant_schedule_with_warmup(optimizer, warmup)
    use_amp = device.type == "cuda"
    scaler = torch.amp.GradScaler("cuda") if use_amp else None

    def train_step(batch) -> float:
        model.train()
        batch = {k: v.to(device) for k, v in batch.items()}
        optimizer.zero_grad(set_to_none=True)
        with torch.autocast(device_type="cuda", enabled=use_amp):
            logits = model(input_ids=batch["input_ids"], attention_mask=batch["attention_mask"]).logits
            loss = loss_fn(logits.float(), batch["labels"])
        stepped = True
        if scaler:
            scaler.scale(loss).backward()
            scale = scaler.get_scale()
            scaler.step(optimizer)
            scaler.update()
            # The scaler skips the optimizer while it calibrates (the first
            # step or two on a GPU); the schedule waits for a real step.
            stepped = scaler.get_scale() >= scale
        else:
            loss.backward()
            optimizer.step()
        if stepped:
            scheduler.step()
        return float(loss.item())

    if args.check:
        batches = iter(train_loader)
        train_step(next(batches))  # warm-up step, not timed
        start = time.perf_counter()
        steps = 0
        for batch in batches:
            train_step(batch)
            steps += 1
            if steps == 1:
                break
        if device.type == "cuda":
            torch.cuda.synchronize()
        per_step = (time.perf_counter() - start) / max(1, steps)
        minutes = per_step * (len(train_loader) + len(val_loader) / 3) / 60
        print(f"Check passed: 2 training steps on {device}, batch size {args.batch_size}, "
              f"max length {args.max_length}. About {per_step:.2f} s per step, "
              f"{len(train_loader)} steps per epoch: roughly {minutes:.1f} minutes per epoch. Nothing saved.")
        return 0

    log: list[dict[str, Any]] = []
    epoch, best_f1, best_epoch, best_preds = 0, -1.0, 0, None
    if state:
        # The weights came back with from_pretrained(resume_dir / "model") above.
        optimizer.load_state_dict(state["optimizer"])
        scheduler.load_state_dict(state["scheduler"])
        if scaler and state.get("scaler"):
            scaler.load_state_dict(state["scaler"])
        restore_rng(state["rng"])
        epoch, best_f1, best_epoch, log = state["epoch"], state["best_f1"], state["best_epoch"], state["log"]
        best_preds = state.get("best_preds")
        print(f"Resuming after epoch {epoch} (best so far: epoch {best_epoch}, macro F1 {best_f1:.4f})")

    ctx = {"classes": classes, "taxonomy": taxonomy, "skipped": skipped,
           "train_counts": train_counts, "val_counts": val_counts}
    config = {
        "data": str(args.data.resolve()), "output": str(output), "resume_dir": str(resume_dir),
        "settings": {k: getattr(args, k) for k in ("model_name", "max_length", "batch_size", "lr", "seed",
                                                    "min_pages", "val_fraction", "min_chars")},
        "environment": environment,
        "pages": {"usable": len(records), "left_out_short": short, "trained": len(kept),
                  "train": len(train), "val": len(val)},
        "classes": len(classes), "skipped_classes": len(skipped),
    }

    epochs = args.epochs if args.epochs is not None else (1 if args.resume else EPOCHS)
    for _ in range(epochs):
        epoch += 1
        started = time.perf_counter()
        losses = [train_step(batch) for batch in train_loader]
        truth, preds = predict(model, val_loader, device)
        result = scores(truth, preds, len(classes))
        seconds = time.perf_counter() - started
        row = {"epoch": epoch, "train_loss": round(sum(losses) / max(1, len(losses)), 4),
               "val_macro_f1": round(result["macro_f1"], 4), "val_accuracy": round(result["accuracy"], 4),
               "seconds": round(seconds, 1)}
        log.append(row)
        improved = result["macro_f1"] > best_f1
        print(f"Epoch {epoch}: loss {row['train_loss']}, val macro F1 {row['val_macro_f1']}, "
              f"accuracy {row['val_accuracy']}, {row['seconds']} s" + ("  (best so far, saved)" if improved else ""))
        for i, c in enumerate(classes):
            print(f"    {c:<40} recall {result['recall'][i]:.2f}  f1 {result['f1'][i]:.2f}  "
                  f"val pages {result['support'][i]}")
        if improved:
            best_f1, best_epoch, best_preds = result["macro_f1"], epoch, preds
            save_best(output, model, tokenizer, ctx, result)

        # Resume state: outside the model folder, refreshed every epoch.
        resume_dir.mkdir(parents=True, exist_ok=True)
        model.save_pretrained(resume_dir / "model")
        tokenizer.save_pretrained(resume_dir / "model")
        state = {
            "settings": config["settings"], "classes": classes, "skipped": skipped,
            "val_images": sorted(val_images), "epoch": epoch, "best_f1": best_f1, "best_epoch": best_epoch,
            "best_preds": best_preds,
            "log": log, "optimizer": optimizer.state_dict(), "scheduler": scheduler.state_dict(),
            "scaler": scaler.state_dict() if scaler else None, "rng": rng_state(),
        }
        tmp = state_path.with_suffix(".tmp")
        torch.save(state, tmp)
        tmp.replace(state_path)
        output.mkdir(parents=True, exist_ok=True)
        write_csv(output / "training_log.csv", list(row), [list(r.values()) for r in log])
        write_json(output / "run_config.json",
                   {**config, "epochs_run": epoch, "best_epoch": best_epoch, "best_val_macro_f1": best_f1})

    # Prove the saved folder works: load it from disk and score validation again.
    saved = AutoModelForSequenceClassification.from_pretrained(output).to(device)
    if saved.config.id2label != id2label:
        raise SystemExit("Reload check failed: the saved labels differ from the trained classes")
    truth, preds = predict(saved, val_loader, device)
    reloaded = scores(truth, preds, len(classes))["macro_f1"]
    agree = (sum(int(a == b) for a, b in zip(preds, best_preds or [])) / len(preds)) if preds else 1.0
    if abs(reloaded - best_f1) > 1e-4 and agree < 0.99:
        raise SystemExit(f"Reload check failed: saved model scores {reloaded:.4f}, best epoch scored {best_f1:.4f}")
    print(f"Reload check passed: {output} scores macro F1 {reloaded:.4f} on validation "
          f"(best epoch {best_epoch}). Resume state: {resume_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
