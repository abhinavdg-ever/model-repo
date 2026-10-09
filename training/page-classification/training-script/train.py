#!/usr/bin/env python3
"""
Train the page classifier. Two heads on one DistilBERT encoder:

    page_family   classes learned from the JSONL (21 families + unknown)
    codeability    4 classes   (codeable | non_codeable | discharge_frequency
                                | not_applicable)

Codeability is a separate head, not derived from the family, because some
families hold page types from more than one bucket.

Input: JSONL from data-prep. Each line has at least
    id            image stem (chart8841_p001 from page_id chart8841_p001.png)
    text          the page's OCR text, uncleaned
    page_family   the family label
    codeability   the bucket label

    python training/page-classification/training-script/train.py \\
        --data training/page-classification/data-prep/training.jsonl \\
        --out training/page-classification/training-script/output
    python training/page-classification/training-script/train.py \\
        --data training/page-classification/data-prep/training.jsonl \\
        --protocol synthetic_only
"""
from __future__ import annotations

import argparse, collections, json, random, re
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from sklearn.metrics import classification_report, f1_score
from sklearn.model_selection import GroupShuffleSplit
from torch.utils.data import DataLoader, Dataset
from transformers import AutoModel, AutoTokenizer, get_linear_schedule_with_warmup

ID_RE = re.compile(r"^(?P<chart>.+?)_p(?P<page>\d+)$")

CODEABILITY = ["codeable", "non_codeable", "discharge_frequency", "not_applicable"]

# Families whose pages a coder must see. Weighted up: a codeable page misfiled
# as non-codeable never reaches a coder and a diagnosis is lost.
CODEABLE_FAMILIES = {
    "progress_note", "discharge", "procedure", "obstetric", "specialty_consult",
    "assessment_screening", "care_plan", "therapy_rehab", "behavioral_health",
    "inpatient_critical", "ophthalmology", "cardiac_diagnostic",
    "neuro_diagnostic", "pulmonary_sleep",
}


# ------------------------------------------------------------------ data ---

def load(path: Path) -> list[dict]:
    """Read the JSONL and derive chart, page order and prev_page_family."""
    rows = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        r = json.loads(line)
        m = ID_RE.match(r["id"])
        if m:
            r["chart_id"] = m["chart"]
            r["page_index"] = int(m["page"])
        else:
            r["chart_id"] = r["id"]
            r["page_index"] = n
        r.setdefault("source", "real")
        rows.append(r)

    rows.sort(key=lambda r: (r["chart_id"], r["page_index"]))
    prev_chart = prev_family = None
    for r in rows:
        r["prev_page_family"] = prev_family if r["chart_id"] == prev_chart else "none"
        prev_chart, prev_family = r["chart_id"], r["page_family"]
    return rows


def split_by_chart(rows, test_size, seed):
    """A chart is on one side of the split or the other, never both: pages from
    one packet share a template, and a page-level split inflates the score."""
    groups = [r["chart_id"] for r in rows]
    if len(set(groups)) < 4:
        raise SystemExit(f"only {len(set(groups))} charts. Collect pages from at "
                         f"least 4 before splitting; a page-level split would "
                         f"put near-identical pages on both sides.")
    a, b = next(GroupShuffleSplit(1, test_size=test_size,
                                  random_state=seed).split(rows, groups=groups))
    return [rows[i] for i in a], [rows[i] for i in b]


class Pages(Dataset):
    """Head+tail truncation: keep the title and the signature block, drop the
    middle. A page's identity is at the top; the sign-off is at the bottom."""

    def __init__(self, rows, tok, fam2id, max_length=512, head_share=0.85):
        self.rows, self.tok, self.fam2id = rows, tok, fam2id
        self.max_length, self.head_share = max_length, head_share
        self.cod2id = {c: i for i, c in enumerate(CODEABILITY)}

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        r = self.rows[i]
        text = f"[PREV:{r['prev_page_family']}] {r['text']}"
        budget = self.max_length - 2
        head = int(budget * self.head_share)
        ids = self.tok(text, add_special_tokens=False, truncation=False)["input_ids"]
        if len(ids) > budget:
            ids = ids[:head] + ids[-(budget - head):]
        ids = [self.tok.cls_token_id, *ids, self.tok.sep_token_id]
        return {"ids": ids,
                "family": self.fam2id[r["page_family"]],
                "cod": self.cod2id[r["codeability"]]}


def collate(batch, pad_id):
    width = max(len(b["ids"]) for b in batch)
    input_ids = torch.full((len(batch), width), pad_id, dtype=torch.long)
    attn = torch.zeros((len(batch), width), dtype=torch.long)
    for i, b in enumerate(batch):
        input_ids[i, : len(b["ids"])] = torch.tensor(b["ids"])
        attn[i, : len(b["ids"])] = 1
    return (input_ids, attn,
            torch.tensor([b["family"] for b in batch]),
            torch.tensor([b["cod"] for b in batch]))


# ----------------------------------------------------------------- model ---

class TwoHead(nn.Module):
    def __init__(self, base_model: str, n_family: int, dropout: float = 0.1):
        super().__init__()
        self.encoder = AutoModel.from_pretrained(base_model)
        dim = self.encoder.config.hidden_size
        self.drop = nn.Dropout(dropout)
        self.family = nn.Linear(dim, n_family)
        self.codeability = nn.Linear(dim, len(CODEABILITY))

    def forward(self, input_ids, attention_mask):
        h = self.encoder(input_ids=input_ids, attention_mask=attention_mask)
        pooled = h.last_hidden_state[:, 0]          # [CLS]
        pooled = self.drop(pooled)
        return self.family(pooled), self.codeability(pooled)


def weights_for(rows, classes, field, boost: dict[str, float] | None = None,
                cap: float = 8.0):
    """Inverse frequency, capped, with an optional per-class boost."""
    counts = collections.Counter(r[field] for r in rows)
    n, k = len(rows), len(classes)
    w = [min(n / (k * max(counts.get(c, 0), 1)), cap) * (boost or {}).get(c, 1.0)
         for c in classes]
    return torch.tensor(w, dtype=torch.float)


# ------------------------------------------------------------------ loop ---

@torch.no_grad()
def evaluate(model, loader, device, families):
    model.eval()
    yf, pf, yc, pc = [], [], [], []
    for input_ids, attn, fam, cod in loader:
        lf, lc = model(input_ids.to(device), attn.to(device))
        pf += lf.argmax(-1).cpu().tolist(); yf += fam.tolist()
        pc += lc.argmax(-1).cpu().tolist(); yc += cod.tolist()

    cod_ids = [i for i, f in enumerate(families) if f in CODEABLE_FAMILIES]
    lost = sum(1 for t, p in zip(yf, pf) if t in cod_ids and p not in cod_ids)
    total = sum(1 for t in yf if t in cod_ids) or 1
    return {
        "family_macro_f1": f1_score(yf, pf, average="macro", zero_division=0),
        "cod_macro_f1": f1_score(yc, pc, average="macro", zero_division=0),
        "codeable_lost_rate": lost / total,
    }, (yf, pf, yc, pc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("training/page-classification/training-script/output"))
    ap.add_argument("--base-model", default="distilbert-base-uncased")
    ap.add_argument("--max-length", type=int, default=512)
    ap.add_argument("--head-share", type=float, default=0.85)
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--lr", type=float, default=3e-5)
    ap.add_argument("--cod-loss-weight", type=float, default=0.5,
                    help="how much the codeability head contributes to the loss")
    ap.add_argument("--codeable-boost", type=float, default=2.0)
    ap.add_argument("--real-copies", type=int, default=3)
    ap.add_argument("--protocol", choices=["normal", "synthetic_only"],
                    default="normal")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()

    random.seed(a.seed); np.random.seed(a.seed); torch.manual_seed(a.seed)
    device = ("cuda" if torch.cuda.is_available()
              else "mps" if torch.backends.mps.is_available() else "cpu")

    rows = load(a.data)
    families = sorted({r["page_family"] for r in rows})
    fam2id = {f: i for i, f in enumerate(families)}
    real = [r for r in rows if r["source"] == "real"]
    synth = [r for r in rows if r["source"] != "real"]
    print(f"{len(rows)} pages | real {len(real)} synthetic {len(synth)} | "
          f"{len(families)} families | device {device}")

    if a.protocol == "synthetic_only":
        # Does the synthetic data generalise, or did the model learn the
        # generator's habits? Run this before spending real labels.
        train, val, test = synth, real, real
    else:
        trainval, test = split_by_chart(real, 0.25, a.seed)
        train_real, val = split_by_chart(trainval, 0.2, a.seed)
        train = synth + train_real * a.real_copies
    random.shuffle(train)
    print(f"train {len(train)} | val {len(val)} | test {len(test)}")

    tok = AutoTokenizer.from_pretrained(a.base_model)
    mk = lambda rs, shuffle: DataLoader(
        Pages(rs, tok, fam2id, a.max_length, a.head_share),
        batch_size=a.batch_size, shuffle=shuffle,
        collate_fn=lambda b: collate(b, tok.pad_token_id))

    model = TwoHead(a.base_model, len(families)).to(device)
    w_fam = weights_for(train, families, "page_family",
                        {f: a.codeable_boost for f in CODEABLE_FAMILIES}).to(device)
    w_cod = weights_for(train, CODEABILITY, "codeability",
                        {"codeable": a.codeable_boost}).to(device)
    loss_fam = nn.CrossEntropyLoss(weight=w_fam)
    loss_cod = nn.CrossEntropyLoss(weight=w_cod)

    train_loader, val_loader, test_loader = mk(train, True), mk(val, False), mk(test, False)
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=0.01)
    steps = len(train_loader) * a.epochs
    sched = get_linear_schedule_with_warmup(opt, int(steps * 0.1), steps)

    best, best_state = -1.0, None
    for epoch in range(1, a.epochs + 1):
        model.train()
        running = 0.0
        for input_ids, attn, fam, cod in train_loader:
            opt.zero_grad()
            lf, lc = model(input_ids.to(device), attn.to(device))
            loss = (loss_fam(lf, fam.to(device))
                    + a.cod_loss_weight * loss_cod(lc, cod.to(device)))
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step(); sched.step()
            running += loss.item()

        m, _ = evaluate(model, val_loader, device, families)
        print(f"epoch {epoch}  loss {running/max(len(train_loader),1):.4f}  "
              f"family_f1 {m['family_macro_f1']:.4f}  "
              f"cod_f1 {m['cod_macro_f1']:.4f}  "
              f"codeable_lost {m['codeable_lost_rate']:.4f}")
        # keep the best epoch by family macro F1, not the last one
        if m["family_macro_f1"] > best:
            best = m["family_macro_f1"]
            best_state = {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}

    if best_state:
        model.load_state_dict(best_state)

    print("\n=== held-out test ===")
    m, (yf, pf, yc, pc) = evaluate(model, test_loader, device, families)
    print(classification_report(yf, pf, labels=range(len(families)),
                                target_names=families, zero_division=0))
    print(classification_report(yc, pc, labels=range(len(CODEABILITY)),
                                target_names=CODEABILITY, zero_division=0))
    for k, v in m.items():
        print(f"{k:24s} {v:.4f}")

    a.out.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), a.out / "model.pt")
    tok.save_pretrained(a.out)
    (a.out / "meta.json").write_text(json.dumps({
        "base_model": a.base_model, "families": families,
        "codeability": CODEABILITY, "max_length": a.max_length,
        "head_share": a.head_share, "prev_family_token": True,
        "protocol": a.protocol, "test_metrics": m,
    }, indent=2))
    print(f"\nsaved -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
