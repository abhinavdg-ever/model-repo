"""The page-leaf model at inference: encoder + a leaf head (one class per whole path
L1 > L2 > L3) + a specialty head, as ``training/bert-training-leaf/train_leaf.py`` trains it.

The functions below are copied from ``train_leaf.py`` unchanged, so the pipeline decodes
exactly what the trainer scored; ``tests/test_leaf.py`` fails when a copy drifts. The
pipeline does not import ``training/`` (the trainer must also run alone on Colab).
Torch and transformers are imported inside the functions.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

MAX_LENGTH = 512
HEAD_FRACTION = 0.85  # of the token budget kept from the start; the rest from the end
DROPOUT = 0.1
HEADS = ("leaf", "specialty")
TOP_K = 5  # leaves kept per page
PER_L1 = 3  # of which at most this many under one L1
MIN_PROB = 0.01  # a leaf below this is never kept


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


def load_model(folder: Path, device=None):
    """(model, tokenizer, label space) from a saved page-leaf folder."""
    import torch
    from transformers import AutoTokenizer

    space = json.loads((folder / "label_space.json").read_text(encoding="utf-8"))
    model = build_model(str(folder / "encoder"), space, space.get("dropout", DROPOUT))
    model.heads.load_state_dict(torch.load(folder / "heads.pt", map_location="cpu", weights_only=True))
    model.to(device or "cpu").eval()
    return model, AutoTokenizer.from_pretrained(folder / "encoder"), space
