#!/usr/bin/env python3
"""The page-pair text model (briefs 1 and 2, steps E3 / Q3; 2026-11-02).

One encoder (Bio_ClinicalBERT by default) reads the END of page A and the START of page B as one
input ("tail [SEP] head") and has two heads:

* ``break``   -- P(the document ends after page A), on every meeting point of a chart, whatever the
                 document type (``document_pairs.jsonl``, label ``doc_breaks``; 2026-11-02, user:
                 every document can run on; the same target as the continuity clue model,
                 continuity/ml/build_data.py). Unsure points count at half weight.
* ``follows`` -- P(page B comes directly after page A), for pages at most 5 places apart in one DOS block
                 (``order_pairs.jsonl`` = Training/boundary/local/order_pairs_local.jsonl, label ``follows``;
                 ``doc_id`` holds the DOS block id; the same target as the local order clue model, 2026-11-02).

Both files are split by chart already (``split``: train / val / test). The best epoch by mean
validation AUC of the two heads is kept; test is scored once at the end, never tuned on.

Writes ``--output`` (default ``output/boundary-text``):

* ``model.onnx``      -- input_ids, attention_mask -> probs [batch, 2] = (p_break, p_follows), for CPU
* ``tokenizer/``, ``label_space.json`` (heads, max_length, words per side), ``metrics.json``
* ``encoder/``, ``heads.pt``  -- the PyTorch weights (to fine-tune again)
* ``scores_break.csv`` (pair_id, p_break_text, source) and ``scores_follows.csv`` (doc_id, image_a,
  image_b, p_follows_text, source): the text score of every pair, to feed the clue models as one
  more clue. With ``--folds K`` the train / val scores are out-of-fold (K models, grouped by chart),
  so the clue model never sees a score the text model learned that pair from; test scores come
  from the final model.

    python train_pairs.py --check                  # label counts, two steps, a time estimate
    python train_pairs.py                          # train, export, score (no folds)
    python train_pairs.py --folds 5                # + out-of-fold scores for the clue models

Page text is never printed or written: only counts, ids, scores and metrics.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import time
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("USE_TF", "0")

HERE = Path(__file__).resolve().parent
BASE_MODEL = "emilyalsentzer/Bio_ClinicalBERT"
HEADS = ("break", "follows")
WORDS = 150  # words kept from the end of page A and from the start of page B
MAX_LENGTH = 384
SEED = 42


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--document-pairs", type=Path, default=HERE / "data" / "document_pairs.jsonl")
    p.add_argument("--order-pairs", type=Path, default=HERE / "data" / "order_pairs.jsonl")
    p.add_argument("--output", type=Path, default=HERE / "output" / "boundary-text")
    p.add_argument("--base-model", default=BASE_MODEL)
    p.add_argument("--epochs", type=int, default=4)
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--max-length", type=int, default=MAX_LENGTH)
    p.add_argument("--words", type=int, default=WORDS)
    p.add_argument("--folds", type=int, default=0, help="K > 1: out-of-fold text scores for train / val pairs")
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--check", action="store_true", help="counts, two training steps, a time estimate; saves nothing")
    p.add_argument("--no-onnx", action="store_true")
    return p.parse_args(argv)


# --- data -----------------------------------------------------------------------------------------


def _tail(text: str, words: int) -> str:
    return " ".join((text or "").split()[-words:])


def _head(text: str, words: int) -> str:
    return " ".join((text or "").split()[:words])


def load_examples(doc_path: Path, order_path: Path, words: int) -> list[dict]:
    out = []
    with doc_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            out.append({"head": 0, "id": r["pair_id"], "chart": r["pair_id"].split("|")[0], "split": r["split"],
                        "label": float(r["doc_breaks"]), "weight": 0.5 if int(r.get("unsure") or 0) else 1.0,
                        "a": _tail(r.get("tail_n"), words), "b": _head(r.get("head_n1"), words)})
    with order_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            out.append({"head": 1, "id": f"{r['doc_id']}|{r['image_a']}|{r['image_b']}", "chart": r["doc_id"].split("#")[0],
                        "doc_id": r["doc_id"], "image_a": r["image_a"], "image_b": r["image_b"], "split": r["split"],
                        "label": float(r["follows"]), "weight": 1.0,
                        "a": _tail(r.get("tail_a"), words), "b": _head(r.get("head_b"), words)})
    return out


def counts(examples: list[dict]) -> dict:
    c: dict = {}
    for e in examples:
        key = f"{HEADS[e['head']]}/{e['split']}"
        c.setdefault(key, [0, 0])[int(e["label"])] += 1
    return {k: {"neg": v[0], "pos": v[1]} for k, v in sorted(c.items())}


# --- model ----------------------------------------------------------------------------------------


def build_model(base: str):
    import torch
    from torch import nn
    from transformers import AutoModel

    class PairModel(nn.Module):
        def __init__(self):
            super().__init__()
            self.encoder = AutoModel.from_pretrained(base)
            self.dropout = nn.Dropout(0.1)
            self.heads = nn.Linear(self.encoder.config.hidden_size, len(HEADS))

        def forward(self, input_ids, attention_mask):
            out = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
            cls = out.last_hidden_state[:, 0]
            return self.heads(self.dropout(cls))  # logits [batch, 2]

    return PairModel()


class Probs:  # wrapper for ONNX: probabilities instead of logits, no dropout
    @staticmethod
    def wrap(model):
        import torch
        from torch import nn

        class _P(nn.Module):
            def __init__(self, m):
                super().__init__()
                self.m = m

            def forward(self, input_ids, attention_mask):
                return torch.sigmoid(self.m(input_ids, attention_mask))

        return _P(model).eval()


def encode(tokenizer, examples, max_length):
    enc = tokenizer([e["a"] for e in examples], [e["b"] for e in examples], truncation="longest_first",
                    max_length=max_length, padding=False)
    return enc["input_ids"]


def batches(ids, examples, order, batch_size, pad_id):
    import torch

    for start in range(0, len(order), batch_size):
        chunk = order[start:start + batch_size]
        width = max(len(ids[i]) for i in chunk)
        x = torch.full((len(chunk), width), pad_id, dtype=torch.long)
        m = torch.zeros((len(chunk), width), dtype=torch.long)
        for row, i in enumerate(chunk):
            x[row, :len(ids[i])] = torch.tensor(ids[i])
            m[row, :len(ids[i])] = 1
        yield (x, m, torch.tensor([examples[i]["head"] for i in chunk]),
               torch.tensor([examples[i]["label"] for i in chunk]), torch.tensor([examples[i]["weight"] for i in chunk]), chunk)


def predict(model, ids, examples, idx, batch_size, pad_id, device):
    import torch

    model.eval()
    out = {}
    with torch.no_grad():
        for x, m, head, _y, _w, chunk in batches(ids, examples, idx, batch_size * 2, pad_id):
            p = torch.sigmoid(model(x.to(device), m.to(device))).cpu()
            for row, i in enumerate(chunk):
                out[i] = float(p[row, examples[i]["head"]])
    return out


def scores(examples, idx, probs) -> dict:
    from sklearn.metrics import precision_recall_fscore_support, roc_auc_score

    res = {}
    for h, name in enumerate(HEADS):
        rows = [i for i in idx if examples[i]["head"] == h]
        y = [int(examples[i]["label"]) for i in rows]
        p = [probs[i] for i in rows]
        if len(set(y)) < 2:
            continue
        pr, rc, f1, _ = precision_recall_fscore_support(y, [v >= 0.5 for v in p], average="binary", zero_division=0)
        res[name] = {"pairs": len(rows), "pos": sum(y), "auc": round(roc_auc_score(y, p), 4),
                     "P@0.5": round(pr, 3), "R@0.5": round(rc, 3), "F1@0.5": round(f1, 3)}
    return res


def train_one(args, examples, ids, train_idx, val_idx, pad_id, device, log_prefix=""):
    """Train on train_idx, keep the best epoch by mean val AUC; returns (model, best val scores)."""
    import copy

    import torch
    from transformers import get_linear_schedule_with_warmup

    torch.manual_seed(args.seed)
    model = build_model(args.base_model).to(device)
    steps = math.ceil(len(train_idx) / args.batch_size) * (1 if args.check else args.epochs)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    sched = get_linear_schedule_with_warmup(opt, max(1, int(0.1 * steps)), steps)
    rng = random.Random(args.seed)
    best, best_state, best_val = -1.0, None, {}
    for epoch in range(1, (1 if args.check else args.epochs) + 1):
        model.train()
        order = list(train_idx)
        rng.shuffle(order)
        started, done, run = time.time(), 0, []
        for x, m, head, y, w, _ in batches(ids, examples, order, args.batch_size, pad_id):
            logits = model(x.to(device), m.to(device))
            chosen = logits.gather(1, head.to(device).unsqueeze(1)).squeeze(1)
            loss = (torch.nn.functional.binary_cross_entropy_with_logits(chosen, y.to(device), reduction="none")
                    * w.to(device)).sum() / w.sum().to(device)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            done += 1
            run.append(float(loss))
            if args.check and done == 2:
                per = (time.time() - started) / 2
                total = len(train_idx) / args.batch_size * args.epochs * per / 60
                print(f"check: 2 steps on {device}, ~{per:.2f}s a step, ~{total:.0f} min for {args.epochs} epochs"
                      f"{f' x {args.folds + 1} models' if args.folds > 1 else ''}; nothing saved")
                return model, {}
        val = scores(examples, val_idx, predict(model, ids, examples, val_idx, args.batch_size, pad_id, device))
        mean = sum(v["auc"] for v in val.values()) / max(1, len(val))
        flag = mean > best
        print(f"{log_prefix}epoch {epoch}: loss {sum(run) / len(run):.4f}, val "
              + ", ".join(f"{k} AUC {v['auc']:.4f} F1 {v['F1@0.5']:.3f}" for k, v in val.items())
              + ("  (best)" if flag else ""), flush=True)
        if flag:
            best, best_val = mean, val
            best_state = copy.deepcopy({k: v.detach().cpu() for k, v in model.state_dict().items()})
    model.load_state_dict(best_state)
    return model, best_val


def export_onnx(model, tokenizer, path: Path, max_length: int) -> None:
    import torch

    wrapped = Probs.wrap(model.cpu())
    enc = tokenizer(["end of page a"], ["start of page b"], return_tensors="pt", truncation=True, max_length=max_length)
    kwargs = dict(
        input_names=["input_ids", "attention_mask"], output_names=["probs"],
        dynamic_axes={"input_ids": {0: "batch", 1: "seq"}, "attention_mask": {0: "batch", 1: "seq"}, "probs": {0: "batch"}},
        opset_version=17, do_constant_folding=True,
    )
    try:  # torch >= 2.9 defaults to the dynamo exporter; the classic one keeps dynamic_axes simple
        torch.onnx.export(wrapped, (enc["input_ids"], enc["attention_mask"]), str(path), dynamo=False, **kwargs)
    except TypeError:
        torch.onnx.export(wrapped, (enc["input_ids"], enc["attention_mask"]), str(path), **kwargs)


def main(argv=None) -> int:
    args = parse_args(argv)
    import torch
    from transformers import AutoTokenizer

    random.seed(args.seed)
    examples = load_examples(args.document_pairs, args.order_pairs, args.words)
    print(f"{len(examples)} pairs:", json.dumps(counts(examples)))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    tokenizer = AutoTokenizer.from_pretrained(args.base_model)
    ids = encode(tokenizer, examples, args.max_length)
    pad_id = tokenizer.pad_token_id or 0
    split = {s: [i for i, e in enumerate(examples) if e["split"] == s] for s in ("train", "val", "test")}

    model, best_val = train_one(args, examples, ids, split["train"], split["val"], pad_id, device)
    if args.check:
        return 0
    test_probs = predict(model, ids, examples, split["test"], args.batch_size, pad_id, device)
    metrics = {"base_model": args.base_model, "val_best": best_val, "test": scores(examples, split["test"], test_probs)}
    print("test:", json.dumps(metrics["test"]))

    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    model.encoder.save_pretrained(out / "encoder")
    torch.save(model.heads.state_dict(), out / "heads.pt")
    tokenizer.save_pretrained(out / "tokenizer")
    (out / "label_space.json").write_text(json.dumps({
        "kind": "page_pair_text", "heads": list(HEADS), "output": "probs [batch, 2] = (p_break, p_follows)",
        "input": "tokenizer(tail of page A, head of page B), truncation longest_first",
        "words_per_side": args.words, "max_length": args.max_length, "base_model": args.base_model}, indent=1), encoding="utf-8")

    # Text scores for every pair: out-of-fold for train / val when --folds, else the final model's.
    all_probs = dict(test_probs)
    source = {i: "test" for i in split["test"]}
    trval = split["train"] + split["val"]
    if args.folds > 1:
        charts = sorted({examples[i]["chart"] for i in trval})
        random.Random(args.seed).shuffle(charts)
        fold_of = {c: k % args.folds for k, c in enumerate(charts)}
        for k in range(args.folds):
            held = [i for i in trval if fold_of[examples[i]["chart"]] == k]
            fit = [i for i in trval if fold_of[examples[i]["chart"]] != k]
            fit_val = [i for i in fit if examples[i]["split"] == "val"] or held
            fold_model, _ = train_one(args, examples, ids, [i for i in fit if examples[i]["split"] == "train"] or fit,
                                      fit_val, pad_id, device, log_prefix=f"fold {k + 1}/{args.folds} ")
            all_probs.update(predict(fold_model, ids, examples, held, args.batch_size, pad_id, device))
            source.update({i: "oof" for i in held})
            del fold_model
            if device == "cuda":
                torch.cuda.empty_cache()
    else:
        all_probs.update(predict(model, ids, examples, trval, args.batch_size, pad_id, device))
        source.update({i: "in_sample" for i in trval})
    with (out / "scores_break.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["pair_id", "p_break_text", "source"])
        for i, e in enumerate(examples):
            if e["head"] == 0:
                w.writerow([e["id"], round(all_probs[i], 5), source[i]])
    with (out / "scores_follows.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["doc_id", "image_a", "image_b", "p_follows_text", "source"])
        for i, e in enumerate(examples):
            if e["head"] == 1:
                w.writerow([e["doc_id"], e["image_a"], e["image_b"], round(all_probs[i], 5), source[i]])
    metrics["scores"] = "out_of_fold" if args.folds > 1 else "in_sample (train / val scores are optimistic)"
    (out / "metrics.json").write_text(json.dumps(metrics, indent=1), encoding="utf-8")

    if not args.no_onnx:
        export_onnx(model, tokenizer, out / "model.onnx", args.max_length)
        print("wrote", out / "model.onnx")
    print("done:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
