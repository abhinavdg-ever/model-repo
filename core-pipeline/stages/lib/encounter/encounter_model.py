"""The encounter model at inference: encoder + one head over five classes, as
``training/bert-training-encounter/train_encounter.py`` trains it.

Everything below the imports is copied from ``train_encounter.py`` unchanged, so the
pipeline reads pages exactly as the trainer scored them. ``tests/test_encounter_classify.py``
fails when a copy drifts: change the trainer, then copy the definition here. The
pipeline does not import ``training/`` (the trainer must also run alone on Colab).

Torch and transformers are imported inside the functions, so importing this module
costs nothing when they are not installed.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

LABEL_VERSION = "encounter-v1"
CLASSES = ["Inpatient", "Outpatient (F2F)", "Outpatient (Evisit)", "Outpatient (Home Visit)", "Not Available"]
NOT_AVAILABLE = "Not Available"
MAX_LENGTH = 512
HEAD_FRACTION = 0.85  # of the token budget kept from the start; the rest from the end
DROPOUT = 0.1


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


def build_model(encoder_source: str, dropout: float = DROPOUT):
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
            self.head = nn.Linear(hidden, len(CLASSES))

        def forward(self, input_ids, attention_mask):
            hidden = self.encoder(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state[:, 0]
            return self.head(self.dropout(hidden)).float()

    return EncounterModel()


def load_model(folder: Path, device=None):
    """(model, tokenizer, label space) from a saved encounter folder."""
    import torch
    from transformers import AutoTokenizer

    space = json.loads((folder / "label_space.json").read_text(encoding="utf-8"))
    if space.get("classes") != CLASSES:
        raise SystemExit(f"{folder} was trained on classes {space.get('classes')}, not {CLASSES}")
    model = build_model(str(folder / "encoder"), space.get("dropout", DROPOUT))
    model.head.load_state_dict(torch.load(folder / "head.pt", map_location="cpu", weights_only=True))
    model.to(device or "cpu").eval()
    return model, AutoTokenizer.from_pretrained(folder / "encoder"), space
