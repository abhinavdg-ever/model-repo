#!/usr/bin/env python3
"""Bootstrap an encounter-type training set from pages already labelled for page type.

Reads training_data.jsonl (page text + page axes) and encounter_canon.json, scores
every page, pools the scores over the document, and writes a candidate-labelled
dataset for a human to confirm or correct.

Nothing here is a final label. The output carries the evidence quote that produced
each suggestion so a reviewer can confirm in seconds rather than reading the page.

    python3 build_encounter_dataset.py training_data.jsonl encounter_canon.json out_prefix
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import defaultdict

SRC = sys.argv[1] if len(sys.argv) > 1 else "training_data.jsonl"
CANON = sys.argv[2] if len(sys.argv) > 2 else "encounter_canon.json"
OUT = sys.argv[3] if len(sys.argv) > 3 else "encounter_training"

canon = json.load(open(CANON))


_PAT: dict[str, "re.Pattern"] = {}


def hit(phrase: str, text: str) -> bool:
    """Whole-phrase match with word boundaries.

    Substring matching is wrong here and silently so: "icu" matches inside
    "difficulty" and "diverticulosis", which on first run put 150 outpatient
    pages into Inpatient.
    """
    pat = _PAT.get(phrase)
    if pat is None:
        pat = _PAT[phrase] = re.compile(
            r"(?<![a-z0-9])" + re.escape(phrase).replace(r"\ ", r"\s+") + r"(?![a-z0-9])")
    return pat.search(text) is not None
LEX = canon["lexicon"]
M = canon["matching"]
PRIOR = {k: v for k, v in canon["document_type_priors"].items() if not k.startswith("_")}
CLASSES = canon["classes"]
DEFAULT_OP = canon["default_when_outpatient_but_modality_unclear"]


def header_band(text: str) -> str:
    lines = [l for l in text.splitlines() if l.strip()]
    n = max(1, int(len(lines) * M["header_band_fraction"]))
    return "\n".join(lines[:n]).lower()


def score_page(text: str) -> tuple[dict, dict]:
    """Return per-class score and the phrase that contributed most."""
    low = text.lower()
    head = header_band(text)
    scores, best = {}, {}
    for cls, bank in LEX.items():
        s = 0.0
        top = (0.0, "")
        for p in bank.get("strong", []):
            if hit(p, low):
                w = M["strong_header_weight"] if hit(p, head) else M["strong_weight"]
                s += w
                if w > top[0]:
                    top = (w, p)
        for p in bank.get("weak", []):
            if hit(p, low):
                s += M["weak_weight"]
                if M["weak_weight"] > top[0]:
                    top = (M["weak_weight"], p)
        for p in bank.get("negative", []):
            if hit(p, low):
                s -= M["strong_weight"]
        scores[cls] = s
        best[cls] = top[1]
    return scores, best


def quote_for(text: str, phrase: str, width: int = 90) -> str:
    if not phrase:
        return ""
    m = _PAT.get(phrase)
    i = m.search(text.lower()).start() if m and m.search(text.lower()) else text.lower().find(phrase)
    if i < 0:
        return ""
    a = max(0, i - width // 2)
    return re.sub(r"\s+", " ", text[a:a + width]).strip()


def decide(scores: dict, l1: str) -> tuple[str, str, float]:
    """Returns (label, how it was decided, margin)."""
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, second = ranked[0], ranked[1]
    margin = top[1] - second[1]

    if top[1] >= M["min_score"] and margin >= M["margin_to_decide"]:
        return top[0], "lexicon", margin
    if l1 in PRIOR:
        return PRIOR[l1], "page type prior", margin
    if top[1] >= M["min_score"]:
        return top[0], "lexicon, thin margin", margin
    return "", "no evidence", margin


# ---- explicit patient-type field (client approved 2026-10-24) ------------
PTF = canon.get("patient_type_field")
_PTF_LABEL = re.compile(PTF["label_pattern"]) if PTF else None


def patient_type_field(text: str) -> str:
    """The setting an explicit Patient Type / Pt Type / Encounter Type field states, or ''."""
    if not PTF:
        return ""
    found = set()
    for m in _PTF_LABEL.finditer(text):
        win = text[m.end():m.end() + PTF["value_window_chars"]].lower()
        best = None
        for word, cls in PTF["values"].items():
            i = re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", win)
            if i and (best is None or i.start() < best[0]):
                best = (i.start(), cls)
        if best:
            found.add(best[1])
    return found.pop() if len(found) == 1 else ""


# ---- per page ------------------------------------------------------------
pages = []
for line in open(SRC):
    r = json.loads(line)
    text = r.get("text") or ""
    scores, best = score_page(text)
    label, how, margin = decide(scores, r.get("L1", ""))
    ptf = patient_type_field(text)
    if ptf and label and label not in ("Not Available", ptf):
        label, how = ptf, "patient type field"
    evid = max(scores, key=lambda c: scores[c])
    pages.append({
        "image": r["image"],
        "chart_id": r.get("chart_id") or "",
        "page_index": r.get("page_index") or "",
        "L1": r.get("L1", ""),
        "L2": r.get("L2", ""),
        "page_encounter_suggestion": label,
        "decided_by": how,
        "margin": round(margin, 1),
        "evidence_phrase": best.get(evid, ""),
        "evidence_quote": quote_for(text, best.get(evid, "")),
        **{f"score_{c}": round(scores[c], 1) for c in CLASSES},
        "patient_type_field": ptf,
        "encounter_type": "",          # the human fills this in
        "reviewed_by": "",
        "reviewed_on": "",
    })

# ---- pooled over the chart (stands in for the document until splits exist) ----
groups = defaultdict(list)
for p in pages:
    groups[p["chart_id"] or "__nochart__"].append(p)

# chart rule: a face-sheet patient-type field decides the chart (canon patient_type_field.chart_rule)
chart_ptf = {}
for cid, ps in groups.items():
    vals = {p["patient_type_field"] for p in ps if p["patient_type_field"]}
    if cid != "__nochart__" and len(vals) == 1 and any(p["patient_type_field"] and p["L1"] == "Demographics" for p in ps):
        chart_ptf[cid] = vals.pop()
        if chart_ptf[cid] == "Outpatient (F2F)":
            for p in ps:
                if p["page_encounter_suggestion"] == "Inpatient":
                    p["page_encounter_suggestion"], p["decided_by"] = "Outpatient (F2F)", "patient type field (chart)"

docs = []
for cid, ps in groups.items():
    if cid == "__nochart__":
        continue                      # loose photographs: no document to pool over
    tot = {c: sum(p[f"score_{c}"] for p in ps) for c in CLASSES}
    ranked = sorted(tot.items(), key=lambda kv: -kv[1])
    label = ranked[0][0] if ranked[0][1] >= M["min_score"] else ""
    if label in ("Outpatient (F2F)", "Outpatient (Evisit)"):
        f2f, ev = tot["Outpatient (F2F)"], tot["Outpatient (Evisit)"]
        if abs(f2f - ev) < M["margin_to_decide"]:
            label = DEFAULT_OP        # the explicit client default
    if cid in chart_ptf:
        label = chart_ptf[cid]
    carriers = sorted(
        (p for p in ps if p["evidence_phrase"]),
        key=lambda p: -max(p[f"score_{c}"] for c in CLASSES))[:3]
    docs.append({
        "chart_id": cid,
        "pages": len(ps),
        "chart_encounter_suggestion": label,
        "margin": round(ranked[0][1] - ranked[1][1], 1),
        "evidence_pages": " ".join(str(p["page_index"]) for p in carriers),
        "evidence_quote": carriers[0]["evidence_quote"] if carriers else "",
        **{f"score_{c}": round(tot[c], 1) for c in CLASSES},
        "encounter_type": "",
        "reviewed_by": "",
        "reviewed_on": "",
    })

with open(f"{OUT}_pages.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(pages[0]))
    w.writeheader(); w.writerows(pages)

with open(f"{OUT}_charts.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(docs[0]))
    w.writeheader(); w.writerows(sorted(docs, key=lambda d: -d["pages"]))

with open(f"{OUT}.jsonl", "w") as f:
    for p in pages:
        f.write(json.dumps(p, ensure_ascii=False) + "\n")

print(f"{len(pages)} pages, {len(docs)} charts")
from collections import Counter
print("page suggestions:", Counter(p["page_encounter_suggestion"] or "(none)" for p in pages).most_common())
print("decided by:", Counter(p["decided_by"] for p in pages).most_common())
print("chart suggestions:", Counter(d["chart_encounter_suggestion"] or "(none)" for d in docs).most_common())
