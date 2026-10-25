"""Tune and measure the keyword vote (``keyword_vote evaluate``): the model's answers
on the validation and test pages (cached, no text), the keywords' share and full
evidence per level tuned on validation (the minimums stay as set), everything reported
on test beside the model alone and the keywords alone. Prints counts and rates only."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from stages.lib.page_classify import keyword_vote as kv

SHARES = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
FULLS = (1.0, 2.0, 3.0, 4.0, 6.0)
OTHERS = "Others"


def _model_answers(pages: list[dict[str, Any]], folder: Path, cache: Optional[Path]) -> dict[str, Any]:
    if cache and cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    import torch

    from stages.lib.page_classify import axes_model

    model, tokenizer, space = axes_model.load_model(folder)
    out: dict[str, Any] = {}
    for start in range(0, len(pages), 16):
        chunk = pages[start:start + 16]
        ids = axes_model.encode_texts(tokenizer, [p["text"] for p in chunk], int(space.get("max_length", 512)),
                                      float(space.get("head_fraction", 0.85)))
        width = max(len(x) for x in ids)
        input_ids = torch.tensor([x + [0] * (width - len(x)) for x in ids])
        mask = torch.tensor([[1] * len(x) + [0] * (width - len(x)) for x in ids])
        with torch.no_grad():
            rows = axes_model.decode(model(input_ids, mask), model, space)
        for page, row in zip(chunk, rows):
            out[page["image"]] = row["distributions"]
        print(f"  model: {min(start + 16, len(pages))}/{len(pages)} pages", flush=True)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out), encoding="utf-8")
    return out


def _pick(feats: frozenset[str], s: dict[str, Any], level: str, parent: Optional[str],
          model: Optional[dict[str, float]], classes: list[str]) -> Optional[str]:
    """The level's winner as the pipeline picks it: a single strong-title pin, else the
    vote at or above the level's minimum."""
    pins = kv.pinned(feats, level, parent) & set(classes)
    if len(pins) == 1:
        return next(iter(pins))
    ranked = kv.vote(model, kv.scores(feats, level, parent), s["share"][level], s["full"][level], classes)
    return ranked[0][0] if ranked and ranked[0][1] >= s["min"][level] else None


def _model_at(dist: Optional[dict[str, Any]], level: str, parent: Optional[str]) -> Optional[dict[str, float]]:
    if not dist:
        return None
    if level == "L1":
        return dist.get("L1")
    if level == "L2" and parent == "Progress Note":
        return None  # never trained: section tagging owned it
    return (dist.get(level) or {}).get(parent or "")


def decide(feats: frozenset[str], dist: Optional[dict[str, Any]], s: dict[str, Any],
           space: dict[str, Any]) -> tuple[str, Optional[str], Optional[str]]:
    """(L1, L2, L3) down the predicted chain; Others below a level's minimum."""
    l1 = _pick(feats, s, "L1", None, _model_at(dist, "L1", None), space["L1"])
    if not l1:
        return OTHERS, None, None
    if not space["L2"].get(l1):
        return l1, None, None
    l2 = _pick(feats, s, "L2", l1, _model_at(dist, "L2", l1), space["L2"][l1])
    if not l2:
        return l1, OTHERS, None
    pair = f"{l1} > {l2}"
    if not space["L3"].get(pair):
        return l1, l2, None
    return l1, l2, _pick(feats, s, "L3", pair, _model_at(dist, "L3", pair), space["L3"][pair]) or OTHERS


def score(pages: list[dict[str, Any]], answers: dict[str, Any], s: dict[str, Any], space: dict[str, Any],
          use_model: bool = True, use_keywords: bool = True) -> dict[str, Any]:
    """L1 accuracy; L2 given the true L1 and L3 given the true L1 > L2 (the same pages for
    every method; Progress Note's L2 apart); the path down the predicted chain."""
    c = dict.fromkeys(("n", "l1", "l2n", "l2", "pnn", "pn", "l3n", "l3", "path", "path_note", "others",
                       "labs", "notes", "lab_as_note", "note_as_lab"), 0)
    for p in pages:
        feats = p["features"] if use_keywords else frozenset()
        dist = answers.get(p["image"]) if use_model else None
        l1, l2, l3 = decide(feats, dist, s, space)
        c["n"] += 1
        c["others"] += l1 == OTHERS
        c["l1"] += l1 == p["L1"]
        c["labs"] += p["L1"] == "Lab Report"
        c["notes"] += p["L1"] == "Progress Note"
        c["lab_as_note"] += p["L1"] == "Lab Report" and l1 == "Progress Note"
        c["note_as_lab"] += p["L1"] == "Progress Note" and l1 == "Lab Report"
        if p["L2"] in (space["L2"].get(p["L1"]) or ()):
            got = _pick(feats, s, "L2", p["L1"], _model_at(dist, "L2", p["L1"]), space["L2"][p["L1"]]) or OTHERS
            key = "pn" if p["L1"] == "Progress Note" else "l2"
            c[key + "n" if key == "pn" else "l2n"] += 1
            c[key] += got == p["L2"]
            pair = f"{p['L1']} > {p['L2']}"
            if p["L3"] in (space["L3"].get(pair) or ()):
                got3 = _pick(feats, s, "L3", pair, _model_at(dist, "L3", pair), space["L3"][pair]) or OTHERS
                c["l3n"] += 1
                c["l3"] += got3 == p["L3"]
        gold = (p["L1"], p["L2"] if space["L2"].get(p["L1"]) else "",
                p["L3"] if space["L3"].get(f"{p['L1']} > {p['L2']}") else "")
        right = (l1, l2 or "", l3 or "") == gold
        c["path"] += right
        c["path_note"] += (l1 == p["L1"]) if p["L1"] == "Progress Note" else right
    rate = lambda a, b: round(a / b, 3) if b else None  # noqa: E731
    return {"pages": c["n"], "L1": rate(c["l1"], c["n"]), "L2_true_L1": rate(c["l2"], c["l2n"]),
            "PN_L2": rate(c["pn"], c["pnn"]), "L3_true_L2": rate(c["l3"], c["l3n"]),
            "path": rate(c["path"], c["n"]), "path_note_on_L1": rate(c["path_note"], c["n"]),
            "others": rate(c["others"], c["n"]),
            "lab_as_note": f"{c['lab_as_note']}/{c['labs']}", "note_as_lab": f"{c['note_as_lab']}/{c['notes']}"}


def tune(pages: list[dict[str, Any]], answers: dict[str, Any], space: dict[str, Any]) -> dict[str, Any]:
    """The keywords' share and full evidence per level, each by that level's validation
    accuracy (L2 given the right L1, L3 given the right L2). The minimums are the user's
    (settings), not tuned."""
    s = kv.settings()
    for level, key in (("L1", "L1"), ("L2", "L2_true_L1"), ("L3", "L3_true_L2")):
        grid = [(sh, fu) for sh in SHARES for fu in FULLS]

        def result(choice):
            sh, fu = choice
            trial = {**s, "share": {**s["share"], level: sh}, "full": {**s["full"], level: fu}}
            return (score(pages, answers, trial, space)[key] or 0, -sh, fu)

        s["share"][level], s["full"][level] = max(grid, key=result)
    return s


def run(pages: dict[str, list[dict[str, Any]]], space: dict[str, Any], model: Optional[Path],
        cache: Optional[Path], save: bool = False) -> int:
    if not kv.weights().get("L1"):
        print("no keyword weights yet: run keyword_vote fit first")
        return 1
    answers = _model_answers(pages["val"] + pages["test"], model, cache) if model else {}
    s = tune(pages["val"], answers, space) if answers else kv.settings()
    print("share and full tuned on validation; minimums as set:", json.dumps(s))
    if save and answers:
        table = kv.weights()
        table["settings"] = s
        kv.WEIGHTS_PATH.write_text(json.dumps(table, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"saved the settings into {kv.WEIGHTS_PATH}")
    for side in ("val", "test"):
        print(f"{side}:")
        if answers:
            print("  model alone   ", score(pages[side], answers, s, space, use_keywords=False))
        print("  keywords alone", score(pages[side], answers, s, space, use_model=False))
        if answers:
            print("  the vote      ", score(pages[side], answers, s, space))
    return 0
