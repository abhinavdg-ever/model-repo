"""The page-axes model at inference: encoder + five heads (L1, L2, L3,
specialty, codeability), as ``training/bert-training/train_axes.py`` trains it.

Everything below the imports is copied from ``train_axes.py`` unchanged, so
the pipeline decodes exactly what the trainer scored. ``tests/test_page_axes.py``
fails when a copy drifts: change the trainer, then copy the function here.
The pipeline does not import ``training/`` (the trainer must also run alone on
Colab), hence the copy.

Torch and transformers are imported inside the functions, so importing this
module costs nothing when they are not installed.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

MAX_LENGTH = 512
HEAD_FRACTION = 0.85  # of the token budget kept from the start; the rest from the end
DROPOUT = 0.1
ROUTE_MODEL = 0.50
ROUTE_MARGIN = 0.10
ROUTE_CANON = 0.25
HEADS = ("L1", "L2", "L3", "specialty", "codeability")
MASKED = -1e4  # not -inf: a fully masked row stays finite under mixed precision
SEP = " > "


def mask_rows(space: dict[str, Any]) -> tuple[list[list[bool]], list[list[bool]]]:
    """Allowed L2 per L1 class, allowed L3 per L2 class."""
    l2 = [[p in space["L2_by_L1"].get(a, []) for p in space["L2"]] for a in space["L1"]]
    l3 = [[t in space["L3_by_L2"].get(p, []) for t in space["L3"]] for p in space["L2"]]
    return l2, l3


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
        out.append(row)
    return out


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


def load_model(folder: Path, device=None):
    """(model, tokenizer, label space) from a saved page-axes folder."""
    import torch
    from transformers import AutoTokenizer

    space = json.loads((folder / "label_space.json").read_text(encoding="utf-8"))
    model = build_model(str(folder / "encoder"), space, space.get("dropout", DROPOUT))
    model.heads.load_state_dict(torch.load(folder / "heads.pt", map_location="cpu", weights_only=True))
    model.to(device or "cpu").eval()
    return model, AutoTokenizer.from_pretrained(folder / "encoder"), space
