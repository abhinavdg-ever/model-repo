"""The keyword canon's vote, level by level, beside the page-axes model's.

At each level (L1; L2 under the L1; L3 under the L2) both sides vote and the winner
takes it:

    vote(class) = (1 - k) * p_model(class) + k * keywords(class) / sum(keywords)
    k = share[level] * min(1, sum(keywords) / full[level])

``keywords(class)`` sums, over the canon terms on the page, each term's weight for that
class. A term is a feature twice over: where it sits ("title": a title or strong term
used as a heading in the title zone; "body": anywhere else). Its weight per class is

* **learned** from labelled pages (``fit``): log(P(class | term) / P(class)), smoothed
  toward the prior, never below 0. A rare term that only one kind of page carries scores
  high; a term every kind of page carries scores about 0;
* **the canon's default** for a term the pages rarely or never show: the tier it is
  listed under in the canon entry, for that entry's class -- strong 4.0 (title 4.0 /
  body 3.0), title 2.5 (as a heading in the title zone; 1.0 elsewhere), body 1.0,
  ambiguous 0.3. Learned and default blend by how often the term was seen
  (n / (n + ``shrink``)).

Weights live in ``keyword-canon/page_keyword_weights.json`` (numbers keyed by canon
terms; no page text). Without a model the keywords vote alone; with no keyword on the
page the model votes alone. The winner's vote is the level's confidence; below
``min[level]`` (0.2) the level is Others, and the Final step revisits the page with its
document.

    python -m stages.lib.page_classify.keyword_vote fit --data training_data.jsonl --split split.csv
    python -m stages.lib.page_classify.keyword_vote evaluate --data ... --split ... --model models/page-family/page-axes

``evaluate`` tunes the keywords' share and full evidence on the validation pages (the
minimums stay as set) and reports the test pages; it prints counts only.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from stages.lib.canon_store import CANON_DIR, CanonFile

WEIGHTS_PATH = CANON_DIR / "page_keyword_weights.json"
TITLE, BODY = "title", "body"
SEP = " > "

DEFAULTS = {"strong": {TITLE: 4.0, BODY: 3.0}, "title": {TITLE: 2.5, BODY: 1.0},
            "body": {TITLE: 1.0, BODY: 1.0}, "ambiguous": {TITLE: 0.3, BODY: 0.3}}
PRIOR_STRENGTH = 2.0  # pseudo-pages pulling P(class | term) toward P(class)
SHRINK = 5.0  # pages after which a learned weight counts as much as the default
MAX_WEIGHT = 6.0
MIN_STORED = 0.05


def _empty(_: Any = None) -> dict[str, Any]:
    return {"settings": {}, "strong": {}, "L1": {}, "L2": {}, "L3": {}}


_WEIGHTS: CanonFile[dict[str, Any]] = CanonFile(WEIGHTS_PATH)


def weights() -> dict[str, Any]:
    """The fitted weights, or an empty table when the file is missing."""
    if not WEIGHTS_PATH.is_file():
        return _empty()
    return _WEIGHTS.get()


def settings() -> dict[str, Any]:
    """The vote's settings: the file's over the defaults, level by level."""
    found = weights().get("settings") or {}
    return {key: {**default, **(found.get(key) or {})} for key, default in DEFAULT_SETTINGS.items()}


# --- features ------------------------------------------------------------------------


def features(text: str, headings: Iterable[str] = (), canon: Any = None) -> frozenset[str]:
    """The page's canon terms as features: "term|title" or "term|body"."""
    from stages.lib.page_classify import axes, keywords

    canon = canon or axes._CANON.get().keywords
    hits = keywords.matches(text, tuple(headings), canon=canon)
    if not hits:
        return frozenset()
    return frozenset(f"{term.text.lower()}|{TITLE if where == 'title' else BODY}" for term, where in hits)


def scores(feats: Iterable[str], level: str, parent: Optional[str] = None) -> dict[str, float]:
    """Keyword score per class at ``level`` (under ``parent``: the L1 for L2, "L1 > L2"
    for L3). Classes no term points to are absent."""
    table = weights().get(level) or {}
    if level != "L1":
        table = table.get(parent or "") or {}
    out: dict[str, float] = defaultdict(float)
    for f in feats:
        for cls, w in (table.get(f) or {}).items():
            out[cls] += w
    return dict(out)


def pinned(feats: Iterable[str], level: str, parent: Optional[str] = None) -> frozenset[str]:
    """The classes a **strong** canon term used as a title pins at ``level`` (under
    ``parent``): rare words only one page type carries. One pinned class wins the level
    outright; two or more cancel each other and the vote decides."""
    table = (weights().get("strong") or {}).get(level) or {}
    if level != "L1":
        table = table.get(parent or "") or {}
    return frozenset(cls for f in feats if f.endswith(f"|{TITLE}") for cls in table.get(f, ()))


def vote(model: Optional[dict[str, float]], keyword: dict[str, float], share: float, full: float = 3.0,
         classes: Optional[Iterable[str]] = None) -> list[tuple[str, float, str]]:
    """(class, vote, who) best first: both sides vote and the winner takes it.

        vote(class) = (1 - k) * p_model(class) + k * keyword(class) / sum(keyword)
        k = share * min(1, sum(keyword) / full)

    A unique strong term (weight 4) carries the keywords' whole ``share``; one ambiguous
    word (0.3) a tenth of it, so thin evidence cannot swing a page. Without a model the
    keywords vote alone (k = 1); with no keyword on the page the model votes alone.
    ``who`` is "model" when the model's own favourite won, "canon" when the keywords
    changed the answer or there is no model. Empty when neither side has anything."""
    total = sum(v for v in keyword.values() if v > 0)
    if not model and not total:
        return []
    names = list(dict.fromkeys([*(classes or ()), *(model or {}), *keyword]))
    k = 1.0 if not model else share * min(1.0, total / full) if total else 0.0
    votes = {c: (1 - k) * (model or {}).get(c, 0.0) + (k * max(0.0, keyword.get(c, 0.0)) / total if total else 0.0)
             for c in names}
    model_best = max(model, key=model.get) if model else None
    ranked = sorted(votes.items(), key=lambda x: -x[1])
    return [(c, round(v, 6), "model" if c == model_best else "canon") for c, v in ranked]


# --- fitting ------------------------------------------------------------------------


def _canon_terms() -> dict[str, list[tuple[str, dict[str, str]]]]:
    """term text (lower) -> [(tier, path)] over the canon entries listing it."""
    data = json.loads((CANON_DIR / "page_canon.json").read_text(encoding="utf-8"))
    out: dict[str, list[tuple[str, dict[str, str]]]] = defaultdict(list)
    for entry in data["page_types"]:
        lexicon = entry.get("lexicon") or {}
        for tier in ("strong", "title", "body", "ambiguous"):
            for term in lexicon.get(f"{tier}_terms") or []:
                if str(term).strip():
                    out[str(term).strip().lower()].append((tier, entry["path"]))
    return out


def _defaults(terms: dict[str, list[tuple[str, dict[str, str]]]], level: str) -> dict[str, dict[str, dict[str, float]]]:
    """parent -> feature -> class -> default weight, from the canon tiers."""
    out: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for term, listed in terms.items():
        for tier, path in listed:
            cls = path.get(level) or ""
            if not cls:
                continue
            parent = "" if level == "L1" else path["L1"] if level == "L2" else f"{path['L1']}{SEP}{path.get('L2', '')}"
            for where in (TITLE, BODY):
                f = f"{term}|{where}"
                out[parent][f][cls] = max(out[parent][f].get(cls, 0.0), DEFAULTS[tier][where])
    return out


def _learn(pages: list[tuple[frozenset[str], str]], defaults: dict[str, dict[str, float]],
           classes: list[str]) -> dict[str, dict[str, float]]:
    """feature -> class -> weight over ``pages`` (features, class)."""
    n = len(pages)
    per_class = Counter(c for _, c in pages)
    prior = {c: (per_class.get(c, 0) + 0.5) / (n + 0.5 * len(classes)) for c in classes}
    seen: dict[str, Counter] = defaultdict(Counter)
    for feats, cls in pages:
        for f in feats:
            seen[f][cls] += 1
    out: dict[str, dict[str, float]] = {}
    for f in set(seen) | set(defaults):
        counts = seen.get(f, Counter())
        total = sum(counts.values())
        blend = total / (total + SHRINK)
        row = {}
        for c in classes:
            learned = 0.0
            if total:
                p = (counts.get(c, 0) + PRIOR_STRENGTH * prior[c]) / (total + PRIOR_STRENGTH)
                learned = max(0.0, math.log(p / prior[c]))
            w = min(MAX_WEIGHT, blend * learned + (1 - blend) * defaults.get(f, {}).get(c, 0.0))
            if w >= MIN_STORED:
                row[c] = round(w, 3)
        if row:
            out[f] = row
    return out


def strong_table(terms: dict[str, list[tuple[str, dict[str, str]]]]) -> dict[str, Any]:
    """level -> [parent ->] "term|title" -> the classes the canon's strong terms pin."""
    out: dict[str, Any] = {"L1": defaultdict(list), "L2": defaultdict(lambda: defaultdict(list)),
                           "L3": defaultdict(lambda: defaultdict(list))}
    for term, listed in terms.items():
        f = f"{term}|{TITLE}"
        for tier, path in listed:
            if tier != "strong":
                continue
            if path["L1"] not in out["L1"][f]:
                out["L1"][f].append(path["L1"])
            if path.get("L2") and path["L2"] not in out["L2"][path["L1"]][f]:
                out["L2"][path["L1"]][f].append(path["L2"])
            pair = f"{path['L1']}{SEP}{path.get('L2', '')}"
            if path.get("L3") and path["L3"] not in out["L3"][pair][f]:
                out["L3"][pair][f].append(path["L3"])
    return json.loads(json.dumps(out))


def fit(pages: list[dict[str, Any]], label_space: dict[str, Any]) -> dict[str, Any]:
    """The weight tables from labelled pages (each: ``features``, L1, L2, L3)."""
    terms = _canon_terms()
    out = _empty()
    out["strong"] = strong_table(terms)
    d1 = _defaults(terms, "L1")[""]
    out["L1"] = _learn([(p["features"], p["L1"]) for p in pages if p["L1"] in label_space["L1"]],
                       d1, list(label_space["L1"]))
    d2 = _defaults(terms, "L2")
    for l1, l2s in label_space["L2"].items():
        rows = [(p["features"], p["L2"]) for p in pages if p["L1"] == l1 and p.get("L2") in l2s]
        out["L2"][l1] = _learn(rows, d2.get(l1, {}), list(l2s))
    d3 = _defaults(terms, "L3")
    for pair, l3s in label_space["L3"].items():
        l1, l2 = pair.split(SEP, 1)
        rows = [(p["features"], p["L3"]) for p in pages if p["L1"] == l1 and p.get("L2") == l2 and p.get("L3") in l3s]
        out["L3"][pair] = _learn(rows, d3.get(pair, {}), list(l3s))
    return out


# --- the command line ---------------------------------------------------------------


def _pages(data: Path, split: Path) -> dict[str, list[dict[str, Any]]]:
    """Labelled pages with their features, by split (train / val / test)."""
    sides = {row["image"]: row["split"] for row in csv.DictReader(split.open(encoding="utf-8"))}
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with data.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            side = sides.get(row.get("image"))
            if not side:
                continue
            out[side].append({"image": row["image"], "text": row.get("text") or "", "L1": row.get("L1"),
                              "L2": row.get("L2") or "", "L3": row.get("L3") or "",
                              "features": features(row.get("text") or "")})
    return out


def _write(table: dict[str, Any], settings_: dict[str, Any], counts: dict[str, int]) -> None:
    table["settings"] = settings_
    table["version"] = 1
    table["comment"] = ("Keyword weights per level for the keyword vote (page_classify/keyword_vote.py): "
                        "feature 'term|title' or 'term|body' -> class -> weight. Fitted on the labelled "
                        f"training pages (train split: {counts}); defaults from the canon tiers for terms "
                        "the pages rarely show. Regenerate after a canon change: keyword_vote fit.")
    WEIGHTS_PATH.write_text(json.dumps(table, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


# min: below it a level is Others (2026-10-25, user: 0.2 for the current model).
# share: the keywords' part of the vote at full evidence; full: the keyword score that
# counts as full evidence. Both tuned on validation pages (evaluate).
DEFAULT_SETTINGS = {"share": {"L1": 0.5, "L2": 0.5, "L3": 0.5}, "full": {"L1": 3.0, "L2": 3.0, "L3": 3.0},
                    "min": {"L1": 0.2, "L2": 0.2, "L3": 0.2}}


def main(argv: Optional[list[str]] = None) -> int:
    from stages.lib.page_classify import taxonomy

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("fit", "evaluate"))
    ap.add_argument("--data", type=Path, required=True, help="training_data.jsonl (labelled pages)")
    ap.add_argument("--split", type=Path, required=True, help="the page-axes run's split.csv")
    ap.add_argument("--model", type=Path, help="evaluate: a page-axes model folder (else keywords alone)")
    ap.add_argument("--cache", type=Path, help="evaluate: cache the model's answers here (no text)")
    ap.add_argument("--save", action="store_true", help="evaluate: write the tuned share and full into the weights file")
    args = ap.parse_args(argv)

    pages = _pages(args.data, args.split)
    space = taxonomy.label_space()
    if args.command == "fit":
        table = fit(pages["train"], space)
        _write(table, settings(), {k: len(v) for k, v in pages.items()})
        print(f"fitted on {len(pages['train'])} pages: {len(table['L1'])} L1 features; wrote {WEIGHTS_PATH}")
        return 0
    from stages.lib.page_classify import keyword_eval

    return keyword_eval.run(pages, space, args.model, args.cache, args.save)


if __name__ == "__main__":
    sys.exit(main())
