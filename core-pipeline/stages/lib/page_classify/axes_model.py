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
TOP_PATHS = 5  # whole paths kept per page (``paths``)
PATHS_PER_L1 = 3  # of which at most this many under one L1


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
        row["distributions"] = distributions(l1_prob[i].tolist(), None if l2_given is None else l2_given[i].tolist(),
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


def distributions(l1_prob: list[float], l2_given: list[list[float]] | None, l3_given: list[list[float]] | None,
                  space: dict[str, Any]) -> dict[str, Any]:
    """The model's whole answer per level, for the keyword vote: p(L1) over every L1,
    p(L2 | L1) under every L1 with L2s, p(L3 | L2) under every L2 with L3s."""
    l2_index = {pair: i for i, pair in enumerate(space["L2"])}
    l3_index = {name: i for i, name in enumerate(space["L3"])}
    out: dict[str, Any] = {"L1": {l1: round(float(p), 6) for l1, p in zip(space["L1"], l1_prob)}, "L2": {}, "L3": {}}
    for a, l1 in enumerate(space["L1"]):
        pairs = space["L2_by_L1"].get(l1) if l2_given is not None else None
        if pairs:
            out["L2"][l1] = {pair.split(SEP, 1)[1]: round(float(l2_given[a][l2_index[pair]]), 6) for pair in pairs}
    for pair, names in (space.get("L3_by_L2") or {}).items():
        if l3_given is not None and names and pair in l2_index:
            out["L3"][pair] = {name.rsplit(SEP, 1)[1]: round(float(l3_given[l2_index[pair]][l3_index[name]]), 6)
                               for name in names}
    return out


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
